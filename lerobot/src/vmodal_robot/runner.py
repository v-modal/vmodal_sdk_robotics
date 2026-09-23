from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

import asyncio
import random
import time

from .contracts import Adapter, Artifact, DatasetRevision, Receipt, Transport, TransportBlocked
from .spool import Spool


@dataclass(frozen=True)
class RunnerConfig:
    discovery_batch: int = 100
    poll_seconds: float = 1.0
    request_timeout_seconds: float = 120.0
    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 300.0
    max_attempts: int = 10


class Runner:
    def __init__(self, adapter: Adapter, spool: Spool, transport: Transport, cfg: RunnerConfig = RunnerConfig()):
        self.adapter = adapter
        self.spool = spool
        self.transport = transport
        self.cfg = cfg
        self._stop = False
        self._recovered = False

    def admit(self) -> List[DatasetRevision]:
        revisions = []
        for item in self.adapter.discover(self.cfg.discovery_batch):
            try:
                revisions.append(self.spool.accept(item))
            except Exception as exc:
                log_error("handoff rejected", manifest=item.manifest_path, reason=str(exc))
        drain = getattr(self.adapter, "drain_errors", None)
        if drain is not None:
            for path, reason in drain():
                self.spool.reject(path, reason)
                log_error("manifest rejected", manifest=path, reason=reason)
        return revisions

    def _delay(self, attempts: int) -> float:
        base = min(self.cfg.retry_max_seconds, self.cfg.retry_base_seconds * (2 ** max(0, attempts - 1)))
        return base * random.uniform(0.8, 1.2)

    def _validate_receipt(self, receipt: Receipt, item_id: str, checksum: str = ""):
        if receipt.artifact_id != item_id:
            raise ValueError("transport receipt identity mismatch")
        if receipt.status != "ACKNOWLEDGED":
            raise ValueError(f"transport has not durably acknowledged the item: {receipt.status}")
        if not receipt.remote_ref:
            raise ValueError("transport receipt requires a stable remote reference")
        if checksum and receipt.checksum and receipt.checksum != checksum:
            raise TransportBlocked("transport receipt checksum conflicts with the local artifact")

    async def recover(self):
        for lane in ("video", "aux"):
            for artifact, destination in self.spool.sending(lane):
                try:
                    receipt = await asyncio.wait_for(
                        self.transport.reconcile(artifact, destination),
                        timeout=self.cfg.request_timeout_seconds,
                    )
                    if receipt is None:
                        self.spool.mark_ready(artifact.artifact_id, "remote outcome not found during recovery")
                    else:
                        self._validate_receipt(receipt, artifact.artifact_id, artifact.checksum)
                        self.spool.mark_acknowledged(receipt)
                except TransportBlocked as exc:
                    self.spool.mark_blocked(artifact.artifact_id, str(exc))
                except Exception as exc:
                    self._retry_artifact(artifact.artifact_id, exc)
        for revision in self.spool.sending_revisions():
            self.spool.mark_revision_ready(revision.revision_id, "revision publication interrupted")
        self._recovered = True

    def _retry_artifact(self, artifact_id: str, exc: Exception):
        attempts = self.spool.artifact_attempts(artifact_id)
        if attempts >= self.cfg.max_attempts:
            self.spool.mark_blocked(artifact_id, str(exc))
        else:
            self.spool.mark_retry(artifact_id, str(exc), self._delay(attempts))

    def _retry_revision(self, revision_id: str, exc: Exception):
        attempts = self.spool.revision_attempts(revision_id)
        if attempts >= self.cfg.max_attempts:
            self.spool.mark_revision_blocked(revision_id, str(exc))
        else:
            self.spool.mark_revision_retry(revision_id, str(exc), self._delay(attempts))

    async def run_once(self, lane: str) -> bool:
        if not self._recovered:
            await self.recover()
        claimed = self.spool.claim(lane)
        if claimed is None:
            return False
        artifact, destination = claimed
        try:
            receipt = await asyncio.wait_for(
                self.transport.deliver(artifact, destination),
                timeout=self.cfg.request_timeout_seconds,
            )
            self._validate_receipt(receipt, artifact.artifact_id, artifact.checksum)
            self.spool.mark_acknowledged(receipt)
        except TransportBlocked as exc:
            self.spool.mark_blocked(artifact.artifact_id, str(exc))
        except Exception as exc:
            self._retry_artifact(artifact.artifact_id, exc)
        return True

    async def publish_once(self) -> bool:
        revision = self.spool.claim_revision()
        if revision is None:
            return False
        try:
            receipt = await asyncio.wait_for(
                self.transport.publish_revision(revision),
                timeout=self.cfg.request_timeout_seconds,
            )
            self._validate_receipt(receipt, revision.revision_id)
            self.spool.mark_revision_acknowledged(revision.revision_id, receipt)
        except TransportBlocked as exc:
            self.spool.mark_revision_blocked(revision.revision_id, str(exc))
        except Exception as exc:
            self._retry_revision(revision.revision_id, exc)
        return True

    async def cycle(self, admit: bool = True) -> bool:
        changed = bool(self.admit()) if admit else False
        video, aux = await asyncio.gather(self.run_once("video"), self.run_once("aux"))
        published = await self.publish_once()
        return changed or video or aux or published

    async def run(self):
        await self.recover()
        while not self._stop:
            changed = await self.cycle(admit=True)
            if not changed:
                await asyncio.sleep(self.cfg.poll_seconds)

    async def flush(self, deadline_seconds: float = 60.0) -> bool:
        await self.recover()
        deadline = time.monotonic() + max(0.0, deadline_seconds)
        while time.monotonic() < deadline:
            changed = await self.cycle(admit=False)
            if self.spool.pending_count() == 0:
                while await self.publish_once():
                    pass
                return self.spool.blocked_count() == 0
            if not changed:
                await asyncio.sleep(min(self.cfg.poll_seconds, max(0.0, deadline - time.monotonic())))
        return self.spool.pending_count() == 0 and self.spool.blocked_count() == 0

    def stop(self):
        self._stop = True
