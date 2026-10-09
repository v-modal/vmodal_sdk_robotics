from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

from typing import Protocol


CONTRACT_VERSION = 1
ARTIFACT_KINDS = ("video", "telemetry", "metadata", "opaque")
ARTIFACT_STATES = ("READY", "SENDING", "RETRY_WAIT", "ACKNOWLEDGED", "BLOCKED")


class ContractError(ValueError):
    pass


class SpoolFull(ContractError):
    pass


class TransportRetry(RuntimeError):
    pass


class TransportUnknown(TransportRetry):
    """Remote absence could not be established; retain the uncertain claim."""


class TransportBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class TransportCapabilities:
    """Declared qualification evidence; stable names alone do not establish idempotency."""

    artifact_kinds: Tuple[str, ...] = ()
    max_video_bytes: int = 100 * 1024 * 1024
    durable_receipts: bool = False
    checksum_verified: bool = False
    artifact_idempotency: bool = False
    artifact_reconciliation: bool = False
    revision_publication: bool = False
    revision_idempotency: bool = False
    revision_reconciliation: bool = False


@dataclass(frozen=True)
class ArtifactInput:
    rel_path: str
    kind: str
    content_type: str
    source_path: str
    byte_length: int
    checksum: str = ""
    source_clock: str = ""
    time_start: Optional[float] = None
    time_end: Optional[float] = None
    source_refs: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class RevisionInput:
    source_id: str
    dataset_key: str
    source_format: str
    source_version: str
    destination: str
    source_revision: str
    complete: bool
    artifacts: Tuple[ArtifactInput, ...]
    manifest_path: str = ""


@dataclass(frozen=True)
class Artifact:
    contract_version: int
    artifact_id: str
    dataset_id: str
    source_id: str
    kind: str
    rel_path: str
    content_type: str
    byte_length: int
    checksum: str
    local_ref: str
    source_clock: str = ""
    time_start: Optional[float] = None
    time_end: Optional[float] = None
    source_refs: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class DatasetRevision:
    contract_version: int
    revision_id: str
    dataset_id: str
    source_id: str
    source_format: str
    source_version: str
    destination: str
    source_revision: str
    complete: bool
    artifact_ids: Tuple[str, ...]


@dataclass(frozen=True)
class Receipt:
    artifact_id: str
    remote_ref: str
    status: str
    checksum: str = ""
    detail: str = ""
    checksum_verified: bool = False


class Adapter(Protocol):
    """Custom adapters need only discover; admission_feedback(item, outcome) is optional.

    The built-in adapter confirms accepted handoffs after the durable commit,
    retries deferred handoffs, and caches permanent rejection until content changes.
    """

    def discover(self, limit: int = 100) -> List[RevisionInput]: ...


class Transport(Protocol):
    async def deliver(self, artifact: Artifact, destination: str) -> Receipt: ...

    async def reconcile(self, artifact: Artifact, destination: str) -> Optional[Receipt]:
        """None means confirmed absence; unknown/unavailable must raise TransportUnknown."""
        ...

    async def publish_revision(self, revision: DatasetRevision) -> Receipt: ...
