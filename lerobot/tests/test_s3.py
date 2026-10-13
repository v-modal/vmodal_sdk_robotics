"""Fault tests and optional actual MinIO/S3 round trip."""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass, replace
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning
from src.utils.util_base import os_makedirs, os_path_cleanup

import asyncio
from vmodal_robot.transports.s3 import S3Transport
from vmodal_robot.adapters.lerobot import LeRobotAdapter
from vmodal_robot.spool import Spool, SpoolConfig
from vmodal_robot.runner import Runner, RunnerConfig
from vmodal_robot.contracts import ArtifactInput, TransportBlocked, TransportUnknown
from tests.utils_datafake import pd_fake_data, FakeS3, os_save_bytes, str_sha
from tests.utils_temporal_fake import ContractRaises

dirtest="ztmp/ztests"


async def roundtrip(client, bucket, prefix):
    data = pd_fake_data("s3_roundtrip")
    spool = Spool(SpoolConfig(os.path.join(data["root"], "spool")))
    item = LeRobotAdapter(data["ready"]).discover()[0]
    path = os.path.join(data["dataset"], "audio.wav")
    os_save_bytes(path, b"opaque-original")
    extra = ArtifactInput("audio.wav", "opaque", "audio/wav", path, 15, str_sha(b"opaque-original"))
    item = replace(item, artifacts=item.artifacts + (extra,))
    revision = spool.accept(item)
    transport = S3Transport(client, bucket, prefix)
    try:
        for identity in revision.artifact_ids:
            artifact = spool.get_artifact(identity)
            assert await transport.reconcile(artifact, revision.destination) is None
            first = await transport.deliver(artifact, revision.destination)
            assert first.checksum_verified
            assert first == await transport.deliver(artifact, revision.destination)
            assert first == await transport.reconcile(artifact, revision.destination)
            spool.mark_acknowledged(first)
        assert await transport.reconcile_revision(revision) is None
        first = await transport.publish_revision(revision)
        assert first == await transport.publish_revision(revision)
        assert first == await transport.reconcile_revision(revision)
        stored = await transport._json(transport._key(revision.destination, "revisions", revision.revision_id))
        assert len(stored["artifacts"]) == len(item.artifacts)
        assert {a["artifact"]["rel_path"] for a in stored["artifacts"]} == {a.rel_path for a in item.artifacts}
        assert not any("local_ref" in a["artifact"] for a in stored["artifacts"])
        spool.mark_revision_acknowledged(revision.revision_id, first)
        assert spool.cleanup(0) == len(item.artifacts)
        assert all(os.path.isfile(a.source_path) for a in item.artifacts)
    finally:
        spool.close()


def test1():
    client = FakeS3()
    asyncio.run(roundtrip(client, "test", "roundtrip"))
    assert client.effects == 13


def test2():
    async def check():
        data = pd_fake_data("s3_faults")
        spool = Spool(SpoolConfig(os.path.join(data["root"], "spool")))
        revision = spool.accept(LeRobotAdapter(data["ready"]).discover()[0])
        artifact = spool.get_artifact(revision.artifact_ids[0])
        client = FakeS3()
        transport = S3Transport(client, "test")
        try:
            client.lost = True
            with ContractRaises(ConnectionError, "response lost"):
                await transport.deliver(artifact, revision.destination)
            assert await transport.reconcile(artifact, revision.destination) is None
            receipt = await transport.deliver(artifact, revision.destination)
            assert client.effects == 2
            assert receipt.checksum_verified
            client.unavailable = True
            with ContractRaises(TransportUnknown, "ServiceUnavailable"):
                await transport.reconcile(artifact, revision.destination)
            client.unavailable = False
            with ContractRaises(TransportBlocked, "identity conflict"):
                await transport.reconcile(replace(artifact, rel_path="other.mp4"), revision.destination)
            key = transport._key(revision.destination, "objects", artifact.artifact_id)
            client.objects[("test", key)] = b"corrupt"
            with ContractRaises(TransportBlocked, "checksum conflict"):
                await transport.reconcile(artifact, revision.destination)
            with ContractRaises(TransportBlocked, "checksum conflict"):
                await transport.deliver(artifact, revision.destination)
        finally:
            spool.close()
    asyncio.run(check())


def test3():
    async def check():
        data = pd_fake_data("s3_runner_lost")
        spool = Spool(SpoolConfig(os.path.join(data["root"], "spool")))
        client = FakeS3()
        transport = S3Transport(client, "test")
        runner = Runner(LeRobotAdapter(data["ready"]), spool, transport,
                        RunnerConfig(poll_seconds=.01, retry_base_seconds=0, retry_max_seconds=0))
        try:
            runner.admit()
            client.lost = True
            assert await runner.flush(10)
            assert spool.status()["spool_bytes"] == 0
            assert client.effects == 11
        finally:
            spool.close()
    asyncio.run(check())


def test_live(endpoint: str, bucket: str = "vmodal-test"):
    """Use only a disposable destination; test writes are left for inspection."""
    from aiobotocore.session import get_session
    from botocore.config import Config
    import uuid
    async def check():
        async with get_session().create_client("s3", endpoint_url=endpoint,
                region_name="us-east-1", aws_access_key_id="minioadmin",
                aws_secret_access_key="minioadmin", config=Config(s3={"addressing_style": "path"})) as client:
            await client.create_bucket(Bucket=bucket)
            await roundtrip(client, bucket, "test/" + uuid.uuid4().hex)
    asyncio.run(check())
    log_info("actual S3/MinIO roundtrip passed")


def test4():
    async def check():
        data = pd_fake_data("s3_lost_revision")
        spool = Spool(SpoolConfig(os.path.join(data["root"], "spool")))
        revision = spool.accept(LeRobotAdapter(data["ready"]).discover()[0])
        client = FakeS3()
        transport = S3Transport(client, "test")
        try:
            with ContractRaises(TransportBlocked, "descriptor is missing"):
                await transport.publish_revision(revision)
            for identity in revision.artifact_ids:
                await transport.deliver(spool.get_artifact(identity), revision.destination)
            client.lost = True
            with ContractRaises(ConnectionError, "response lost"):
                await transport.publish_revision(revision)
            receipt = await transport.reconcile_revision(revision)
            assert receipt is not None
            assert receipt == await transport.publish_revision(revision)
            assert client.effects == 11
            with ContractRaises(TransportBlocked, "conflict"):
                await transport.publish_revision(replace(revision, source_revision="conflict"))
            assert client.effects == 11
        finally:
            spool.close()
    asyncio.run(check())


def test_all():
    test1()
    test2()
    test3()
    test4()
    log_info("test_s3: all passed")


if __name__ == "__main__":
    fire.Fire({"test_all": test_all, "test_live": test_live})
