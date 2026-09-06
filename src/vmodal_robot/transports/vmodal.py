from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

import inspect

from ..contracts import Artifact, DatasetRevision, Receipt, TransportBlocked


def str_destination_parts(destination: str) -> Tuple[str, str]:
    values = [value.strip() for value in destination.split("/") if value.strip()]
    if len(values) != 2:
        raise ValueError("destination must be collection/stream")
    return values[0], values[1]


async def _await(value):
    return await value if inspect.isawaitable(value) else value


def _receipt(value: Any, artifact_id: str, checksum: str = "") -> Receipt:
    if isinstance(value, Receipt):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        value = {"remote_ref": str(value)}
    remote_ref = str(value.get("remote_ref") or value.get("dest_path") or value.get("key") or "")
    return Receipt(
        artifact_id=artifact_id,
        remote_ref=remote_ref,
        status=str(value.get("status") or "ACKNOWLEDGED"),
        checksum=str(value.get("checksum") or checksum),
        detail=str(value.get("detail") or ""),
    )


class VmodalTransport:
    """Wrap the reference SDK while keeping unqualified robot APIs explicit."""

    def __init__(self, client: Any, artifact_api: Any = None, multipart_max_concurrency: int = 1):
        self.client = client
        self.artifact_api = artifact_api
        self.multipart_max_concurrency = max(1, int(multipart_max_concurrency))

    @classmethod
    def from_env(cls):
        try:
            from vmodal import Client
        except ImportError as exc:
            raise RuntimeError("install the 'vmodal-robotics[vmodal]' extra for cloud delivery") from exc
        return cls(Client.from_env())

    async def deliver(self, artifact: Artifact, destination: str) -> Receipt:
        if self.artifact_api is not None:
            value = await _await(self.artifact_api.deliver_artifact(artifact, destination))
            return _receipt(value, artifact.artifact_id, artifact.checksum)
        if artifact.kind != "video":
            raise TransportBlocked(
                "Vmodal generic artifact storage is not qualified; original telemetry/metadata cannot be uploaded"
            )
        collection, stream = str_destination_parts(destination)
        value = await self.client.collections.video_upload(
            filepath_local=artifact.local_ref,
            collection_name=collection,
            sub_collection_name=stream,
            reduce_size=False,
            max_concurrency=self.multipart_max_concurrency,
            video_filename=os.path.basename(artifact.rel_path),
        )
        return _receipt(value, artifact.artifact_id, artifact.checksum)

    async def reconcile(self, artifact: Artifact, destination: str) -> Optional[Receipt]:
        if self.artifact_api is None:
            return None
        value = await _await(self.artifact_api.reconcile_artifact(artifact, destination))
        return None if value is None else _receipt(value, artifact.artifact_id, artifact.checksum)

    async def publish_revision(self, revision: DatasetRevision) -> Receipt:
        if self.artifact_api is None:
            raise TransportBlocked(
                "Vmodal revision manifest storage is not qualified; dataset completion cannot be published"
            )
        value = await _await(self.artifact_api.publish_revision(revision))
        return _receipt(value, revision.revision_id)

    async def close(self):
        close = getattr(self.client, "aclose", None)
        if close is not None:
            await _await(close())
