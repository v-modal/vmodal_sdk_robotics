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


class TransportBlocked(RuntimeError):
    pass


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


class Adapter(Protocol):
    def discover(self, limit: int = 100) -> List[RevisionInput]: ...


class Transport(Protocol):
    async def deliver(self, artifact: Artifact, destination: str) -> Receipt: ...

    async def reconcile(self, artifact: Artifact, destination: str) -> Optional[Receipt]: ...

    async def publish_revision(self, revision: DatasetRevision) -> Receipt: ...
