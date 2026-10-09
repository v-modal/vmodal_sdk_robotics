from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

import asyncio
import random
import threading
import time

from .contracts import Adapter, Artifact, DatasetRevision, Receipt, Transport, TransportBlocked, TransportUnknown, SpoolFull
from .spool import Spool


class _DrainExpired(TimeoutError):
    pass


@dataclass(frozen=True)
class RunnerConfig:
    discovery_batch: int = 100
    poll_seconds: float = 1.0
    request_timeout_seconds: float = 120.0
    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 300.0
    max_attempts: int = 0
    retention_seconds: float = 0.0

    def __post_init__(self):
        if self.poll_seconds <= 0 or self.request_timeout_seconds <= 0:
            raise ValueError("poll and request timeouts must be positive")
        if min(self.retry_base_seconds, self.retry_max_seconds, self.retention_seconds, self.max_attempts) < 0:
            raise ValueError("retry, retention and attempt limits must be nonnegative")


class Runner:
    def __init__(self, adapter: Adapter, spool: Spool, transport: Transport, cfg: RunnerConfig = RunnerConfig()):
        self.adapter = adapter
        self.spool = spool
        self.transport = transport
        self.cfg = cfg
        self._stop = False
        self._recovered = False
        self._recover_lock = asyncio.Lock()
        self._deadline = None
        self._force = False
        self._wake = asyncio.Event()
        self._lane_wake = {name: asyncio.Event() for name in ("video", "aux", "publish", "admission")}
        self._copy_stop = threading.Event()
        self._admission_incomplete = False
        self._active_task = None
        self._deadline_handle = None

    def _arm_deadline(self):
        if self._deadline_handle is not None:
            self._deadline_handle.cancel()
        if self._deadline is not None and self._active_task is not None:
            self._deadline_handle = asyncio.get_running_loop().call_later(
                max(0.0, self._deadline - time.monotonic()), self._active_task.cancel,
            )

    async def _bounded(self, operation):
        task = asyncio.create_task(operation)
        self._active_task = task
        if self._deadline is not None and time.monotonic() >= self._deadline:
            task.cancel()
        else:
            self._arm_deadline()
        try:
            return await task
        except asyncio.CancelledError:
            if self._force or (self._deadline is not None and time.monotonic() >= self._deadline):
                raise _DrainExpired("shutdown deadline expired") from None
            raise
        finally:
            self._active_task = None
            if self._deadline_handle is not None:
                self._deadline_handle.cancel()

    async def _request(self, operation):
        timeout = self.cfg.request_timeout_seconds
        if self._deadline is not None:
            timeout = min(timeout, max(0.0, self._deadline - time.monotonic()))
        return await asyncio.wait_for(operation, timeout)

    async def _pause(self, wake=None):
        wake = wake if wake is not None else self._wake
        task = asyncio.create_task(wake.wait())
        try:
            await asyncio.wait({task}, timeout=self.cfg.poll_seconds)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        wake.clear()

    def _notify(self):
        for wake in self._lane_wake.values():
            wake.set()

    def complete(self) -> bool:
        return (self.spool.pending_count() == 0 and self.spool.blocked_count() == 0
                and self.spool.deferred_count() == 0 and not self._admission_incomplete)

    def validate_transport(self):
        validate = getattr(self.transport, "validate_capabilities", None)
        if validate is not None:
            validate(self.spool.cfg.max_video_bytes)
        if getattr(getattr(self.transport, "capabilities", None), "checksum_verified", False):
            self.spool.recover_unverified()

    def admit(self) -> List[DatasetRevision]:
        self.validate_transport()
        self.spool.cleanup(self.cfg.retention_seconds)
        revisions = []
        feedback = getattr(self.adapter, "admission_feedback", None)
        for item in self.adapter.discover(self.cfg.discovery_batch):
            if self._stop:
                break
            try:
                revisions.append(self.spool.accept(item))
                outcome = "accepted"
            except (SpoolFull, OSError) as exc:
                outcome = "deferred"
                log_warning("handoff deferred", manifest=item.manifest_path, reason=str(exc))
            except Exception as exc:
                outcome = "rejected"
                log_error("handoff rejected", manifest=item.manifest_path, reason=str(exc))
            if feedback is not None:
                feedback(item, outcome)
        drain = getattr(self.adapter, "drain_errors", None)
        if drain is not None:
            for path, reason in drain():
                self.spool.reject(path, reason)
                log_error("manifest rejected", manifest=path, reason=reason)
        return revisions

    async def _filesystem(self, operation, *args):
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._copy_stop.set()
            # Join before SQLite closes; a stuck kernel IO call needs a supervisor hard stop.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise

    async def _admit_async(self) -> List[DatasetRevision]:
        self.validate_transport()
        self.spool.cleanup(self.cfg.retention_seconds)
        revisions = []
        feedback = getattr(self.adapter, "admission_feedback", None)
        items = await self._filesystem(self.adapter.discover, self.cfg.discovery_batch)
        try:
            for item in items:
                if self._stop:
                    break
                plan = None
                outcome = "deferred"
                try:
                    plan = self.spool.prepare_accept(item)
                    await self._filesystem(self.spool.copy_accept, plan, self._copy_stop)
                    if self._stop:
                        raise InterruptedError("spool admission stopped")
                    revisions.append(self.spool.commit_accept(plan))
                    plan = None
                    outcome = "accepted"
                    self._notify()
                except (SpoolFull, OSError) as exc:
                    self.spool.reject_accept(item, exc)
                    log_warning("handoff deferred", manifest=item.manifest_path, reason=str(exc))
                except Exception as exc:
                    outcome = "rejected"
                    self.spool.reject_accept(item, exc)
                    log_error("handoff rejected", manifest=item.manifest_path, reason=str(exc))
                except asyncio.CancelledError:
                    self._admission_incomplete = True
                    self.spool.reject_accept(item, InterruptedError("spool admission stopped"))
                    raise
                finally:
                    if plan is not None:
                        self.spool.abandon_accept(plan)
                    if feedback is not None:
                        feedback(item, outcome)
        finally:
            if feedback is not None:
                for item in items:
                    if self._stop:
                        feedback(item, "deferred")
        drain = getattr(self.adapter, "drain_errors", None)
        if drain is not None:
            for path, reason in drain():
                self.spool.reject(path, reason)
                log_error("manifest rejected", manifest=path, reason=reason)
        return revisions

    def _delay(self, attempts: int) -> float:
        base = min(self.cfg.retry_max_seconds, self.cfg.retry_base_seconds * (2 ** min(30, max(0, attempts - 1))))
        return min(self.cfg.retry_max_seconds, base * random.uniform(0.8, 1.2))

    def _validate_receipt(self, receipt: Receipt, item_id: str, checksum: str = ""):
        if not isinstance(receipt, Receipt):
            raise TransportBlocked("transport must return a Receipt")
        if receipt.artifact_id != item_id:
            raise ValueError("transport receipt identity mismatch")
        if receipt.status != "ACKNOWLEDGED":
            raise ValueError(f"transport has not durably acknowledged the item: {receipt.status}")
        if not isinstance(receipt.remote_ref, str) or not receipt.remote_ref.strip():
            raise ValueError("transport receipt requires a stable remote reference")
        if checksum and receipt.checksum != checksum:
            raise TransportBlocked("transport receipt checksum conflicts with the local artifact")
        if checksum and getattr(getattr(self.transport, "capabilities", None), "checksum_verified", False) and receipt.checksum_verified is not True:
            raise TransportBlocked("transport receipt lacks remote checksum verification")

    async def recover(self):
        async with self._recover_lock:
            if not self._recovered:
                if getattr(getattr(self.transport, "capabilities", None), "checksum_verified", False):
                    self.spool.recover_unverified()
                await self._recover()

    async def _recover(self):
        for lane in ("video", "aux"):
            for artifact, destination in self.spool.sending(lane):
                if not self.spool.uncertain_due(artifact.artifact_id):
                    continue
                try:
                    receipt = await self._request(self.transport.reconcile(artifact, destination))
                    if receipt is None:
                        self.spool.mark_ready(artifact.artifact_id, "remote outcome not found during recovery")
                    else:
                        self._validate_receipt(receipt, artifact.artifact_id, artifact.checksum)
                        self.spool.mark_acknowledged(receipt)
                except TransportUnknown as exc:
                    if getattr(getattr(self.transport, "capabilities", None), "artifact_idempotency", False):
                        self._retry_artifact(artifact.artifact_id, exc)
                    else:
                        self.spool.mark_uncertain(artifact.artifact_id, str(exc), self._delay(self.spool.artifact_attempts(artifact.artifact_id)))
                except TransportBlocked as exc:
                    self.spool.mark_blocked(artifact.artifact_id, str(exc))
                except (ValueError, PermissionError) as exc:
                    self.spool.mark_blocked(artifact.artifact_id, str(exc))
                except Exception as exc:
                    self.spool.mark_uncertain(artifact.artifact_id, str(exc), self._delay(self.spool.artifact_attempts(artifact.artifact_id)))
                    log_warning("remote outcome uncertain", artifact=artifact.artifact_id, reason=str(exc))
        for revision in self.spool.sending_revisions():
            if not self.spool.uncertain_due(revision.revision_id, revision=True):
                continue
            reconcile = getattr(self.transport, "reconcile_revision", None)
            caps = getattr(self.transport, "capabilities", None)
            if reconcile is None:
                if getattr(caps, "revision_idempotency", False):
                    self.spool.mark_revision_ready(revision.revision_id, "idempotent publication interrupted")
                else:
                    self.spool.mark_revision_blocked(revision.revision_id, "uncertain publication: revision lookup or idempotent publish required")
                continue
            try:
                receipt = await self._request(reconcile(revision))
                if receipt is None:
                    self.spool.mark_revision_ready(revision.revision_id, "revision confirmed absent")
                else:
                    self._validate_receipt(receipt, revision.revision_id)
                    self.spool.mark_revision_acknowledged(revision.revision_id, receipt)
                    self.spool.cleanup(self.cfg.retention_seconds)
            except TransportUnknown as exc:
                if getattr(caps, "revision_idempotency", False):
                    self._retry_revision(revision.revision_id, exc)
                else:
                    self.spool.mark_uncertain(revision.revision_id, str(exc), self._delay(self.spool.revision_attempts(revision.revision_id)), revision=True)
            except TransportBlocked as exc:
                self.spool.mark_revision_blocked(revision.revision_id, str(exc))
            except (ValueError, PermissionError) as exc:
                self.spool.mark_revision_blocked(revision.revision_id, str(exc))
            except Exception as exc:
                self.spool.mark_uncertain(revision.revision_id, str(exc), self._delay(self.spool.revision_attempts(revision.revision_id)), revision=True)
                log_warning("publication outcome uncertain", revision=revision.revision_id, reason=str(exc))
        self._recovered = True

    def _retry_artifact(self, artifact_id: str, exc: Exception):
        attempts = self.spool.artifact_attempts(artifact_id)
        if isinstance(exc, (ValueError, PermissionError)) or (self.cfg.max_attempts > 0 and attempts >= self.cfg.max_attempts):
            self.spool.mark_blocked(artifact_id, str(exc))
        else:
            self.spool.mark_retry(artifact_id, str(exc), self._delay(attempts))

    def _retry_revision(self, revision_id: str, exc: Exception):
        attempts = self.spool.revision_attempts(revision_id)
        if isinstance(exc, (ValueError, PermissionError)) or (self.cfg.max_attempts > 0 and attempts >= self.cfg.max_attempts):
            self.spool.mark_revision_blocked(revision_id, str(exc))
        else:
            self.spool.mark_revision_retry(revision_id, str(exc), self._delay(attempts))

    async def run_once(self, lane: str) -> bool:
        if not self._recovered:
            await self.recover()
        for artifact, destination in self.spool.sending(lane):
            if not self.spool.uncertain_due(artifact.artifact_id):
                continue
            try:
                receipt = await self._request(self.transport.reconcile(artifact, destination))
                if receipt is None:
                    self._retry_artifact(artifact.artifact_id, RuntimeError("remote artifact confirmed absent"))
                else:
                    self._validate_receipt(receipt, artifact.artifact_id, artifact.checksum)
                    self.spool.mark_acknowledged(receipt)
            except TransportUnknown as exc:
                if getattr(getattr(self.transport, "capabilities", None), "artifact_idempotency", False):
                    self._retry_artifact(artifact.artifact_id, exc)
                else:
                    self.spool.mark_uncertain(artifact.artifact_id, str(exc), self._delay(self.spool.artifact_attempts(artifact.artifact_id)))
            except TransportBlocked as exc:
                self.spool.mark_blocked(artifact.artifact_id, str(exc))
            except (ValueError, PermissionError) as exc:
                self.spool.mark_blocked(artifact.artifact_id, str(exc))
            except Exception as exc:
                self.spool.mark_uncertain(artifact.artifact_id, str(exc), self._delay(self.spool.artifact_attempts(artifact.artifact_id)))
                log_warning("remote outcome uncertain", artifact=artifact.artifact_id, reason=str(exc))
            return True
        claimed = self.spool.claim(lane)
        if claimed is None:
            return False
        artifact, destination = claimed
        try:
            receipt = await self._request(self.transport.deliver(artifact, destination))
            self._validate_receipt(receipt, artifact.artifact_id, artifact.checksum)
            self.spool.mark_acknowledged(receipt)
        except TransportBlocked as exc:
            self.spool.mark_blocked(artifact.artifact_id, str(exc))
        except Exception as exc:
            if isinstance(exc, (ValueError, PermissionError)):
                self._retry_artifact(artifact.artifact_id, exc)
            else:
                self.spool.mark_uncertain(artifact.artifact_id, str(exc), self._delay(self.spool.artifact_attempts(artifact.artifact_id)))
                log_warning("delivery outcome uncertain", artifact=artifact.artifact_id, reason=str(exc))
        return True

    async def publish_once(self) -> bool:
        for revision in self.spool.sending_revisions():
            if not self.spool.uncertain_due(revision.revision_id, revision=True):
                continue
            reconcile = getattr(self.transport, "reconcile_revision", None)
            caps = getattr(self.transport, "capabilities", None)
            try:
                if reconcile is not None:
                    receipt = await self._request(reconcile(revision))
                    if receipt is not None:
                        self._validate_receipt(receipt, revision.revision_id)
                        self.spool.mark_revision_acknowledged(revision.revision_id, receipt)
                        self.spool.cleanup(self.cfg.retention_seconds)
                    else:
                        self._retry_revision(revision.revision_id, RuntimeError("revision confirmed absent"))
                elif getattr(caps, "revision_idempotency", False):
                    self._retry_revision(revision.revision_id, RuntimeError("idempotent publication retry"))
                else:
                    raise TransportBlocked("uncertain publication: revision lookup or idempotent publish required")
            except TransportUnknown as exc:
                if getattr(caps, "revision_idempotency", False):
                    self._retry_revision(revision.revision_id, exc)
                else:
                    self.spool.mark_uncertain(revision.revision_id, str(exc), self._delay(self.spool.revision_attempts(revision.revision_id)), revision=True)
            except TransportBlocked as exc:
                self.spool.mark_revision_blocked(revision.revision_id, str(exc))
            except (ValueError, PermissionError) as exc:
                self.spool.mark_revision_blocked(revision.revision_id, str(exc))
            except Exception as exc:
                self.spool.mark_uncertain(revision.revision_id, str(exc), self._delay(self.spool.revision_attempts(revision.revision_id)), revision=True)
                log_warning("publication outcome uncertain", revision=revision.revision_id, reason=str(exc))
            return True
        revision = self.spool.claim_revision()
        if revision is None:
            return False
        try:
            receipt = await self._request(self.transport.publish_revision(revision))
            self._validate_receipt(receipt, revision.revision_id)
            self.spool.mark_revision_acknowledged(revision.revision_id, receipt)
            self.spool.cleanup(self.cfg.retention_seconds)
        except TransportBlocked as exc:
            self.spool.mark_revision_blocked(revision.revision_id, str(exc))
        except Exception as exc:
            if isinstance(exc, (ValueError, PermissionError)):
                self._retry_revision(revision.revision_id, exc)
            else:
                self.spool.mark_uncertain(revision.revision_id, str(exc), self._delay(self.spool.revision_attempts(revision.revision_id)), revision=True)
                log_warning("publication outcome uncertain", revision=revision.revision_id, reason=str(exc))
        return True

    async def cycle(self, admit: bool = True) -> bool:
        changed = bool(await self._admit_async()) if admit else False
        video, aux = await asyncio.gather(self.run_once("video"), self.run_once("aux"))
        published = await self.publish_once()
        return changed or video or aux or published

    async def _admission_worker(self):
        while not self._stop:
            await self._admit_async()
            if not self._stop:
                await self._pause(self._lane_wake["admission"])

    async def _lane_worker(self, lane: str, admit: bool):
        while True:
            if (self._stop or not admit) and self.spool.pending_count() == 0:
                return
            changed = await (self.publish_once() if lane == "publish" else self.run_once(lane))
            if changed:
                self._notify()
            else:
                await self._pause(self._lane_wake[lane])

    async def _serve(self, admit: bool) -> bool:
        self.validate_transport()
        await self.recover()
        workers = [asyncio.create_task(self._lane_worker(lane, admit)) for lane in ("video", "aux", "publish")]
        if admit:
            workers.append(asyncio.create_task(self._admission_worker()))
        try:
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        return self.complete()

    async def _run(self):
        return await self._serve(admit=True)

    async def run(self) -> bool:
        try:
            return await self._bounded(self._run())
        except _DrainExpired:
            return self.complete()

    async def _flush(self) -> bool:
        return await self._serve(admit=False)

    async def flush(self, deadline_seconds: float = 60.0) -> bool:
        deadline = time.monotonic() + max(0.0, deadline_seconds)
        self._deadline = min(self._deadline, deadline) if self._deadline is not None else deadline
        try:
            return await self._bounded(self._flush())
        except _DrainExpired:
            return self.complete()

    async def close(self) -> bool:
        close = getattr(self.transport, "close", None)
        if close is None:
            return True
        if self._deadline is None:
            self._deadline = time.monotonic() + self.cfg.request_timeout_seconds
        try:
            await self._bounded(close())
            return True
        except _DrainExpired:
            return False

    def stop(self, deadline_seconds: float = 30.0):
        if self._stop:
            self._force = True
            self._deadline = time.monotonic()
        else:
            deadline = time.monotonic() + max(0.0, deadline_seconds)
            self._deadline = min(self._deadline, deadline) if self._deadline is not None else deadline
        self._stop = True
        if self.spool._preparing:
            self._admission_incomplete = True
        self._copy_stop.set()
        self._wake.set()
        self._notify()
        self._arm_deadline()
