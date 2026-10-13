"""Worked S3/MinIO transport; immutable objects and remotely verified receipts.

This example needs conditional PutObject support and exclusive ownership of its
prefix. It publishes datasets, not search indexes. Qualify each deployed backend.
"""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass, asdict
import os,sys
import fire
from vmodal_robot._support.util_log import log_info, log_error, log_trace, log_warning

import asyncio
import hashlib
import json
import math
from botocore.exceptions import ClientError

from ..contracts import ARTIFACT_KINDS, Artifact, DatasetRevision, Receipt, TransportCapabilities, TransportBlocked, TransportUnknown
from ..utils import str_json_canonical, str_sha256_bytes, str_sha256_file


class S3Transport:
    capabilities = TransportCapabilities(
        artifact_kinds=ARTIFACT_KINDS, max_video_bytes=1024 ** 3,
        durable_receipts=True, checksum_verified=True, artifact_idempotency=True,
        artifact_reconciliation=True, revision_publication=True,
        revision_idempotency=True, revision_reconciliation=True,
    )

    def __init__(self, client, bucket: str, prefix: str = "vmodal",
                 video_bytes_per_second: float = 0, pause_file: str = ""):
        if not bucket or not prefix.strip("/"):
            raise ValueError("bucket and dedicated prefix are required")
        self.client, self.bucket, self.prefix = client, bucket, prefix.strip("/")
        if not math.isfinite(video_bytes_per_second) or video_bytes_per_second < 0:
            raise ValueError("video_bytes_per_second must be finite and nonnegative")
        if video_bytes_per_second and not math.isfinite(self.capabilities.max_video_bytes / video_bytes_per_second + 60):
            raise ValueError("video_bytes_per_second is too small for a finite video wait")
        self.video_bytes_per_second = video_bytes_per_second
        self.pause_file = os.path.abspath(pause_file) if pause_file else ""
        self._video_lock = asyncio.Lock()

    async def _wait_enabled(self):
        # Cancelable by the runner's request timeout and shared shutdown deadline.
        while self.pause_file and os.path.lexists(self.pause_file):
            await asyncio.sleep(.25)

    async def _pace_video(self, artifact: Artifact):
        """Charge each video attempt before PUT; other lanes keep progressing.

        This limits average video admission to the network, not packet bursts or
        verification download bandwidth. Use OS traffic shaping for a wire cap.
        """
        if artifact.kind == "video" and self.video_bytes_per_second:
            async with self._video_lock:
                await asyncio.sleep(artifact.byte_length / self.video_bytes_per_second)

    def validate_capabilities(self, max_video_bytes: int):
        if max_video_bytes > self.capabilities.max_video_bytes:
            raise TransportBlocked("S3 example video limit is 1 GiB; segment larger videos")

    def _key(self, destination: str, kind: str, identity: str) -> str:
        scope = str_sha256_bytes(destination.encode())
        return f"{self.prefix}/{scope}/{kind}/{identity}"

    async def _get(self, key: str):
        await self._wait_enabled()
        try:
            return await self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "NoSuchKey":
                return None
            if code in ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
                raise TransportBlocked(str(exc)) from exc
            raise TransportUnknown(str(exc)) from exc

    async def _verify(self, key: str, checksum: str, size: int) -> bool:
        obj = await self._get(key)
        if obj is None:
            return False
        digest, length = hashlib.sha256(), 0
        body = obj["Body"]
        async with body:
            while True:
                block = await body.read(1024 * 1024)
                if not block:
                    break
                length += len(block)
                digest.update(block)
                if length > size:
                    raise TransportBlocked(f"remote length conflict: {key}")
        if length != size or digest.hexdigest() != checksum:
            raise TransportBlocked(f"remote checksum conflict: {key}")
        return True

    async def _json(self, key: str):
        obj = await self._get(key)
        if obj is None:
            return None
        chunks, length = [], 0
        body = obj["Body"]
        async with body:
            while True:
                block = await body.read(1024 * 1024)
                if not block:
                    break
                length += len(block)
                if length > 16 * 1024 * 1024:
                    raise TransportBlocked("descriptor/revision exceeds 16 MiB; segment revisions")
                chunks.append(block)
        return json.loads(b"".join(chunks))

    async def _put(self, key: str, body, content_type: str):
        await self._wait_enabled()
        try:
            await self.client.put_object(Bucket=self.bucket, Key=key, Body=body,
                                         ContentType=content_type, IfNoneMatch="*")
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("PreconditionFailed", "412"):
                return
            if code in ("AccessDenied", "InvalidRequest", "NotImplemented"):
                raise TransportBlocked(str(exc)) from exc
            raise TransportUnknown(str(exc)) from exc

    def _descriptor(self, artifact: Artifact, destination: str):
        value = asdict(artifact)
        value.pop("local_ref")
        return dict(artifact=value, destination=destination,
                    object_key=self._key(destination, "objects", artifact.artifact_id))

    async def _store_json(self, key: str, value):
        data = str_json_canonical(value)
        if len(data) > 16 * 1024 * 1024:
            raise TransportBlocked("descriptor/revision exceeds 16 MiB; segment revisions")
        await self._put(key, data, "application/json")
        if not await self._verify(key, str_sha256_bytes(data), len(data)):
            raise TransportUnknown("object missing after conditional write")

    async def deliver(self, artifact: Artifact, destination: str) -> Receipt:
        if artifact.kind not in ARTIFACT_KINDS:
            raise TransportBlocked("unsupported artifact kind")
        if artifact.kind == "video" and artifact.byte_length > self.capabilities.max_video_bytes:
            raise TransportBlocked("video exceeds S3 example limit")
        await self._wait_enabled()
        await self._pace_video(artifact)
        if os.path.getsize(artifact.local_ref) != artifact.byte_length or await asyncio.to_thread(str_sha256_file, artifact.local_ref) != artifact.checksum:
            raise TransportBlocked("local artifact checksum/length conflict")
        descriptor = self._descriptor(artifact, destination)
        with open(artifact.local_ref, "rb") as handle:
            await self._put(descriptor["object_key"], handle, artifact.content_type)
        if not await self._verify(descriptor["object_key"], artifact.checksum, artifact.byte_length):
            raise TransportUnknown("payload missing after write")
        await self._store_json(self._key(destination, "artifacts", artifact.artifact_id), descriptor)
        return self._receipt(artifact.artifact_id, descriptor["object_key"], artifact.checksum)

    def _receipt(self, identity: str, key: str, checksum: str = ""):
        return Receipt(identity, f"s3://{self.bucket}/{key}", "ACKNOWLEDGED",
                       checksum, checksum_verified=True)

    async def reconcile(self, artifact: Artifact, destination: str) -> Optional[Receipt]:
        descriptor = self._descriptor(artifact, destination)
        stored = await self._json(self._key(destination, "artifacts", artifact.artifact_id))
        if stored is None:
            # Payload-only interruption is safe to replay via conditional creation.
            return None
        if stored != descriptor:
            raise TransportBlocked("artifact descriptor identity conflict")
        if not await self._verify(descriptor["object_key"], artifact.checksum, artifact.byte_length):
            raise TransportBlocked("acknowledged payload is missing")
        return self._receipt(artifact.artifact_id, descriptor["object_key"], artifact.checksum)

    async def _revision_value(self, revision: DatasetRevision):
        descriptors = []
        for identity in revision.artifact_ids:
            value = await self._json(self._key(revision.destination, "artifacts", identity))
            if value is None:
                raise TransportBlocked("revision artifact descriptor is missing")
            artifact = value["artifact"]
            if (artifact["artifact_id"] != identity or artifact["dataset_id"] != revision.dataset_id
                    or value["destination"] != revision.destination
                    or value["object_key"] != self._key(revision.destination, "objects", identity)):
                raise TransportBlocked("revision descriptor identity conflict")
            if not await self._verify(value["object_key"], artifact["checksum"], artifact["byte_length"]):
                raise TransportBlocked("revision payload is missing")
            descriptors.append(value)
        return dict(revision=asdict(revision), artifacts=descriptors)

    async def publish_revision(self, revision: DatasetRevision) -> Receipt:
        value = await self._revision_value(revision)
        key = self._key(revision.destination, "revisions", revision.revision_id)
        await self._store_json(key, value)
        return self._receipt(revision.revision_id, key)

    async def reconcile_revision(self, revision: DatasetRevision) -> Optional[Receipt]:
        key = self._key(revision.destination, "revisions", revision.revision_id)
        stored = await self._json(key)
        if stored is None:
            return None
        if str_json_canonical(stored) != str_json_canonical(await self._revision_value(revision)):
            raise TransportBlocked("revision identity conflict")
        return self._receipt(revision.revision_id, key)

    async def close(self):
        await self.client.close()
