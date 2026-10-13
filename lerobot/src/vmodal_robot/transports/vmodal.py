from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass, asdict
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

import inspect

from ..contracts import (
    ARTIFACT_KINDS, Artifact, DatasetRevision, Receipt, TransportBlocked,
    TransportCapabilities, TransportRetry, TransportUnknown,
)


def str_destination_parts(destination: str) -> Tuple[str, str]:
    values = [value.strip() for value in destination.split("/") if value.strip()]
    if len(values) != 2:
        raise ValueError("destination must be collection/stream")
    return values[0], values[1]


async def _await(value):
    return await value if inspect.isawaitable(value) else value


def _receipt(value: Any, artifact_id: str, checksum: str = "") -> Receipt:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, dict):
        value = Receipt(
            artifact_id=value.get("artifact_id", ""),
            remote_ref=value.get("remote_ref", ""),
            status=value.get("status", ""),
            checksum=value.get("checksum", ""),
            detail=value.get("detail", ""),
            checksum_verified=value.get("checksum_verified", False) is True,
        )
    if not isinstance(value, Receipt) or value.artifact_id != artifact_id:
        raise TransportBlocked("transport receipt identity mismatch or missing identity")
    if value.status != "ACKNOWLEDGED":
        raise TransportBlocked("transport receipt does not establish durable storage")
    if not isinstance(value.remote_ref, str) or not value.remote_ref.strip():
        raise TransportBlocked("transport receipt requires a stable remote reference")
    if checksum and (value.checksum != checksum or value.checksum_verified is not True):
        raise TransportBlocked("transport receipt requires a remotely verified matching checksum")
    return value


def _failure(exc: Exception) -> Exception:
    if isinstance(exc, (TransportBlocked, TransportRetry)):
        return exc
    status = getattr(exc, "status_code", 0)
    if isinstance(exc, (ValueError, PermissionError, NotImplementedError)):
        return TransportBlocked(str(exc))
    if status and status not in (408, 425, 429) and status < 500:
        return TransportBlocked(str(exc))
    if exc.__class__.__name__ in ("AuthError", "ValidationFailed", "FeatureDisabled"):
        return TransportBlocked(str(exc))
    return TransportRetry(str(exc))


class VmodalTransport:
    """Wrap the reference SDK while keeping unqualified robot APIs explicit."""

    def __init__(self, client: Any, artifact_api: Any = None, multipart_max_concurrency: int = 1):
        self.client = client
        self.artifact_api = artifact_api
        self.multipart_max_concurrency = max(1, int(multipart_max_concurrency))
        self.capabilities = getattr(artifact_api, "capabilities", TransportCapabilities(artifact_kinds=("video",)))

    @classmethod
    def from_env(cls):
        """Fail before admission until a generic backend is qualified and wired here."""
        cls(None).validate_capabilities()

    def validate_capabilities(self, max_video_bytes: Optional[int] = None):
        caps = self.capabilities
        if not isinstance(caps, TransportCapabilities):
            raise TransportBlocked("artifact_api must declare TransportCapabilities qualification evidence")
        missing = [f"storage:{kind}" for kind in ARTIFACT_KINDS if kind not in caps.artifact_kinds]
        for name in ("durable_receipts", "checksum_verified", "artifact_idempotency", "revision_publication", "revision_idempotency"):
            if getattr(caps, name) is not True:
                missing.append(name)
        for name in ("deliver_artifact", "publish_revision"):
            if not callable(getattr(self.artifact_api, name, None)):
                missing.append(name)
        if caps.artifact_reconciliation is True and not callable(getattr(self.artifact_api, "reconcile_artifact", None)):
            missing.append("reconcile_artifact")
        if caps.revision_reconciliation is True and not callable(getattr(self.artifact_api, "reconcile_revision", None)):
            missing.append("reconcile_revision")
        if missing:
            raise TransportBlocked("Vmodal LeRobot transport is not qualified; missing capabilities: " + ", ".join(missing))
        if type(caps.max_video_bytes) is not int or caps.max_video_bytes <= 0:
            raise TransportBlocked("qualified max_video_bytes must be a positive integer")
        if max_video_bytes is not None:
            if type(max_video_bytes) is not int or max_video_bytes <= 0:
                raise TransportBlocked("configured max_video_bytes must be a positive integer")
            if max_video_bytes > caps.max_video_bytes:
                raise TransportBlocked(f"max_video_bytes={max_video_bytes} exceeds qualified transport limit {caps.max_video_bytes}")
        return {"qualified": True, **asdict(caps)}

    async def deliver(self, artifact: Artifact, destination: str) -> Receipt:
        self.validate_capabilities()
        try:
            str_destination_parts(destination)
        except ValueError as exc:
            raise TransportBlocked(str(exc)) from exc
        if not artifact.checksum:
            raise TransportBlocked("artifact delivery requires an expected checksum")
        if artifact.kind not in self.capabilities.artifact_kinds:
            raise TransportBlocked(f"unsupported artifact kind: {artifact.kind}")
        if artifact.kind == "video":
            if artifact.byte_length > self.capabilities.max_video_bytes:
                raise TransportBlocked("video exceeds qualified transport byte limit")
            if os.path.basename(artifact.local_ref) != artifact.artifact_id + ".mp4":
                raise TransportBlocked("video payload name must be full artifact_id.mp4; complete spool naming migration first")
        try:
            value = await _await(self.artifact_api.deliver_artifact(artifact, destination))
            return _receipt(value, artifact.artifact_id, artifact.checksum)
        except Exception as exc:
            raise _failure(exc) from exc

    async def reconcile(self, artifact: Artifact, destination: str) -> Optional[Receipt]:
        if not isinstance(self.capabilities, TransportCapabilities) or self.capabilities.artifact_reconciliation is not True:
            raise TransportUnknown("artifact reconciliation is unsupported; remote outcome is unknown")
        try:
            value = await _await(self.artifact_api.reconcile_artifact(artifact, destination))
            return None if value is None else _receipt(value, artifact.artifact_id, artifact.checksum)
        except Exception as exc:
            failure = _failure(exc)
            if isinstance(failure, TransportBlocked):
                raise failure from exc
            raise TransportUnknown(str(failure)) from exc

    async def publish_revision(self, revision: DatasetRevision) -> Receipt:
        self.validate_capabilities()
        try:
            value = await _await(self.artifact_api.publish_revision(revision))
            return _receipt(value, revision.revision_id)
        except Exception as exc:
            raise _failure(exc) from exc

    async def reconcile_revision(self, revision: DatasetRevision) -> Optional[Receipt]:
        if not isinstance(self.capabilities, TransportCapabilities) or self.capabilities.revision_reconciliation is not True:
            raise TransportUnknown("revision reconciliation is unsupported; require qualified idempotent publication")
        try:
            value = await _await(self.artifact_api.reconcile_revision(revision))
            return None if value is None else _receipt(value, revision.revision_id)
        except Exception as exc:
            failure = _failure(exc)
            if isinstance(failure, TransportBlocked):
                raise failure from exc
            raise TransportUnknown(str(failure)) from exc

    async def close(self):
        close = getattr(self.client, "aclose", None)
        if close is not None:
            await _await(close())
