from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning
from src.utils.util_base import os_makedirs, os_path_cleanup

import asyncio
import hashlib
import json
import time
import threading
from unittest.mock import patch

from vmodal_robot.adapters.lerobot import LeRobotAdapter
from vmodal_robot.contracts import ArtifactInput, Receipt, RevisionInput, TransportRetry, TransportBlocked, TransportUnknown, TransportCapabilities
from vmodal_robot.runner import Runner, RunnerConfig
from vmodal_robot.spool import Spool, SpoolConfig
from vmodal_robot.utils import os_atomic_copy
from tests.utils_datafake import FakeTransport, pd_fake_data


dirtest="ztmp/ztests"


async def _drain(runner: Runner):
    while await runner.cycle(admit=False):
        pass


def test1():
    data = pd_fake_data("runner_full")
    spool = Spool(SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000))
    transport = FakeTransport()
    runner = Runner(LeRobotAdapter(data["ready"]), spool, transport, RunnerConfig(retry_base_seconds=0))
    revisions = runner.admit()
    asyncio.run(_drain(runner))
    assert len(transport.remote) == 5
    assert len(transport.payloads) == 5
    assert len(transport.revisions) == 1
    assert spool.pending_count() == 0
    assert spool.revision_state(revisions[0].revision_id) == "ACKNOWLEDGED"
    for artifact_id in revisions[0].artifact_ids:
        artifact = spool.get_artifact(artifact_id)
        source = [value for value in data["data"]["artifacts"] if value["path"] == artifact.rel_path][0]
        assert transport.payloads[artifact_id] == open(os.path.join(data["dataset"], source["path"]), "rb").read()
    spool.close()


def _daemon_fault(phase):
    data = pd_fake_data("runner_lanes_" + phase)
    spool = Spool(SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000))

    async def check():
        gate, started, aux_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        copy_finished = threading.Event()
        loop = asyncio.get_running_loop()

        class Transport(FakeTransport):
            async def deliver(self, artifact, destination):
                if phase == "copy" and artifact.kind != "video":
                    await started.wait()
                if artifact.kind == "video" and phase in ("video", "retry"):
                    started.set()
                    if phase == "video":
                        await gate.wait()
                    else:
                        raise TransportRetry("deliberate video outage")
                receipt = await super().deliver(artifact, destination)
                count = sum(spool.get_artifact(key).kind != "video" for key in self.remote)
                if count >= (6 if phase == "publish" else 3):
                    aux_done.set()
                return receipt

            async def publish_revision(self, revision):
                if phase == "publish":
                    started.set()
                    await gate.wait()
                return await super().publish_revision(revision)

        transport = Transport()
        runner = Runner(LeRobotAdapter(data["ready"]), spool, transport,
                        RunnerConfig(poll_seconds=0.05, retry_base_seconds=60, retry_max_seconds=60))
        second = None
        if phase in ("copy", "publish"):
            second = pd_fake_data("runner_lanes_second_" + phase)

        def add_second():
            value = dict(second["data"], dataset_root=second["dataset"], dataset_key="second/dataset")
            with open(os.path.join(data["ready"], "second.ready.json"), "w", encoding="utf-8") as handle:
                json.dump(value, handle)
            runner._notify()

        def copy(source, target, chunk_bytes=1024 * 1024, stop_event=None):
            if phase == "copy" and source.startswith(second["dataset"] + os.sep):
                loop.call_soon_threadsafe(started.set)
                stop_event.wait()
                copy_finished.set()
            return os_atomic_copy(source, target, chunk_bytes, stop_event)

        if phase == "copy":
            runner.admit()
            add_second()
        with patch("vmodal_robot.spool.os_atomic_copy", copy):
            task = asyncio.create_task(runner.run())
            try:
                await asyncio.wait_for(started.wait(), 2)
                if phase == "publish":
                    add_second()
                done = asyncio.create_task(aux_done.wait())
                await asyncio.wait({done}, timeout=2)
                done.cancel()
                await asyncio.gather(done, return_exceptions=True)
                assert aux_done.is_set(), (phase, spool.status(), len(transport.remote))
                assert not gate.is_set()
                if phase == "video":
                    assert len(transport.remote) == 3
                    assert len(spool.sending("video")) == 1
                if phase == "copy":
                    assert spool._preparing
                    assert not copy_finished.is_set()
                    assert spool.db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 1
                if phase == "publish":
                    assert len(transport.revisions) == 0
                    assert len(spool.sending_revisions()) == 1
                if phase == "retry":
                    assert len(spool.sending("video")) == 2
                    assert all(not spool.uncertain_due(artifact.artifact_id) for artifact, _ in spool.sending("video"))
                runner.stop(0)
                assert await asyncio.wait_for(task, 1) is False
                assert not [value for value in asyncio.all_tasks() if value is not asyncio.current_task()]
                if phase == "copy":
                    assert copy_finished.is_set()
                    assert not spool._preparing
                    assert spool.status()["discovery"]["pending"] == 0, (spool.status()["discovery"], runner.adapter.discovery_status())
                    assert spool.status()["discovery"]["deferred"] == 1
            finally:
                runner.stop(0)
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())
    spool.close()


def test2():
    _discovery_stop_check()
    for phase in ("video", "publish", "copy", "retry"):
        _daemon_fault(phase)


def _discovery_stop_check():
    data = pd_fake_data("runner_discovery_stop")
    spool = Spool(SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000))
    adapter = LeRobotAdapter(data["ready"])
    runner = Runner(adapter, spool, FakeTransport())
    discover = adapter.discover

    async def check():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def delayed(limit):
            loop.call_soon_threadsafe(started.set)
            runner._copy_stop.wait()
            return discover(limit)

        with patch.object(adapter, "discover", delayed):
            task = asyncio.create_task(runner.run())
            await asyncio.wait_for(started.wait(), 1)
            runner.stop(0)
            assert await asyncio.wait_for(task, 1) is False
        status = spool.status()["discovery"]
        assert status["pending"] == 0
        assert status["deferred"] == 1
        assert spool.status()["record_count"] == 0

    asyncio.run(check())
    spool.close()


def test3():
    data = pd_fake_data("runner_lost")
    spool = Spool(SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000))
    transport = FakeTransport()
    runner = Runner(
        LeRobotAdapter(data["ready"]), spool, transport,
        RunnerConfig(retry_base_seconds=0, retry_max_seconds=0),
    )
    revision = runner.admit()[0]
    aux_id = [value for value in revision.artifact_ids if spool.get_artifact(value).kind != "video"][0]
    transport.lost_once.add(aux_id)
    asyncio.run(runner.run_once("aux"))
    assert spool.artifact_state(aux_id) == "SENDING"
    for _ in range(3):
        asyncio.run(runner.run_once("aux"))
    assert spool.artifact_state(aux_id) == "ACKNOWLEDGED"
    assert list(transport.remote).count(aux_id) == 1
    spool.close()


def test4():
    data = pd_fake_data("runner_restart")
    cfg = SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000)
    spool = Spool(cfg)
    revision = spool.accept(LeRobotAdapter(data["ready"]).discover()[0])
    artifact, destination = spool.claim("video")
    transport = FakeTransport()
    transport.remote[artifact.artifact_id] = Receipt(
        artifact.artifact_id, f"fake://{destination}/{artifact.artifact_id}", "ACKNOWLEDGED", artifact.checksum
    )
    spool.close()
    spool = Spool(cfg)
    runner = Runner(LeRobotAdapter(data["ready"]), spool, transport)
    asyncio.run(runner.recover())
    assert spool.artifact_state(artifact.artifact_id) == "ACKNOWLEDGED"
    assert len(transport.remote) == 1
    spool.close()


def test5():
    data = pd_fake_data("runner_quota")
    spool = Spool(SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100, aux_reserve_bytes=95))
    runner = Runner(LeRobotAdapter(data["ready"]), spool, FakeTransport())
    assert runner.admit() == []
    status = spool.status()
    assert status["deferred_handoffs"] == 1
    assert status["spool_bytes"] == 0
    assert status["discovery"]["deferred"] == 1
    assert status["admission_pressure"]["pause_recording"]
    assert "unresolved_capacity" in status["admission_pressure"]["reasons"]
    assert os.path.isfile(os.path.join(data["dataset"], "videos/chunk-000/cam-front.mp4"))
    spool.close()

    data = pd_fake_data("runner_capacity_returns")
    total = sum(item["size_bytes"] for item in data["data"]["artifacts"])
    cfg = SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=total, aux_reserve_bytes=0)
    spool = Spool(cfg)
    second = dict(data["data"], dataset_key="different/dataset", source_revision="revision-002")
    with open(os.path.join(data["ready"], "revision-002.ready.json"), "w", encoding="utf-8") as handle:
        json.dump(second, handle)
    runner = Runner(LeRobotAdapter(data["ready"]), spool, FakeTransport(), RunnerConfig(discovery_batch=1))
    assert len(runner.admit()) == 1
    assert spool.status()["discovery"]["unseen"] == 1
    assert spool.status()["discovery"]["admitted"] == 1
    assert runner.admit() == []
    assert spool.status()["deferred_handoffs"] == 1
    assert spool.status()["discovery"]["deferred"] == 1
    asyncio.run(_drain(runner))
    assert spool.status()["spool_bytes"] == 0
    assert len(runner.admit()) == 1
    assert spool.status()["deferred_handoffs"] == 0
    assert spool.status()["discovery"]["admitted"] == 2
    asyncio.run(_drain(runner))
    assert len(runner.transport.revisions) == 2
    spool.close()


def test6():
    root = os.path.abspath(os.path.join(dirtest, "runner_opaque"))
    os_path_cleanup(root)
    os_makedirs(root)
    path = os.path.join(root, "sample.mcap")
    value = b"fake opaque MCAP"
    with open(path, "wb") as handle:
        handle.write(value)
    source = ArtifactInput(
        rel_path="sample.mcap", kind="opaque", content_type="application/octet-stream",
        source_path=path, byte_length=len(value), checksum=hashlib.sha256(value).hexdigest(),
        source_refs={"format": "mcap"},
    )
    item = RevisionInput(
        source_id="fake-adapter", dataset_key="opaque-set", source_format="fake",
        source_version="v1", destination="robot-data/opaque", source_revision="one",
        complete=True, artifacts=(source,),
    )
    spool = Spool(SpoolConfig(os.path.join(root, "spool"), max_bytes=10000, aux_reserve_bytes=1000))
    revision = spool.accept(item)
    transport = FakeTransport()
    runner = Runner(LeRobotAdapter(os.path.join(root, "none")), spool, transport)
    asyncio.run(_drain(runner))
    assert spool.artifact_state(revision.artifact_ids[0]) == "ACKNOWLEDGED"
    assert spool.revision_state(revision.revision_id) == "ACKNOWLEDGED"
    spool.close()


def test7():
    data = pd_fake_data("runner_oversized")
    spool = Spool(
        SpoolConfig(
            os.path.join(data["root"], "spool"), max_bytes=100000,
            aux_reserve_bytes=1000, max_video_bytes=5,
        )
    )
    runner = Runner(LeRobotAdapter(data["ready"]), spool, FakeTransport())
    assert runner.admit() == []
    assert spool.status()["rejected_handoffs"] == 1
    assert spool.status()["record_count"] == 0
    assert spool.status()["admission_pressure"]["recommended_action"] == "segment_video"
    assert spool.status()["discovery"]["deferred"] == 1
    spool.close()

    for malformed in ("dict", "empty_ref"):
        data = pd_fake_data("runner_bad_receipt_" + malformed)
        spool = Spool(SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000))

        class BadTransport(FakeTransport):
            async def deliver(self, artifact, destination):
                return {} if malformed == "dict" else Receipt(artifact.artifact_id, " ", "ACKNOWLEDGED", artifact.checksum)

        runner = Runner(LeRobotAdapter(data["ready"]), spool, BadTransport())
        revision = runner.admit()[0]
        asyncio.run(runner.run_once("video"))
        video = next(value for value in revision.artifact_ids if spool.get_artifact(value).kind == "video")
        assert spool.artifact_state(video) == "BLOCKED"
        assert os.path.isfile(spool.get_artifact(video).local_ref)
        assert spool.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        spool.close()


def test8():
    data = pd_fake_data("runner_cancel_restart")
    cfg = SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000)
    spool = Spool(cfg)
    transport = FakeTransport()
    transport.video_gate = asyncio.Event()
    transport.video_started = asyncio.Event()
    runner = Runner(LeRobotAdapter(data["ready"]), spool, transport)
    revision = runner.admit()[0]

    async def check():
        task = asyncio.create_task(runner.flush(0.05))
        await asyncio.wait_for(transport.video_started.wait(), 1)
        assert await task is False
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(check())
    artifact = spool.sending("video")[0][0]
    value = open(artifact.local_ref, "rb").read()
    assert hashlib.sha256(value).hexdigest() == artifact.checksum
    spool.close()
    spool = Spool(cfg)
    transport.remote[artifact.artifact_id] = Receipt(artifact.artifact_id, "fake://stored", "ACKNOWLEDGED", artifact.checksum)
    runner = Runner(LeRobotAdapter(data["ready"]), spool, transport)
    asyncio.run(runner.recover())
    assert spool.get_artifact(artifact.artifact_id).checksum == artifact.checksum
    assert spool.artifact_state(artifact.artifact_id) == "ACKNOWLEDGED"
    assert open(artifact.local_ref, "rb").read() == value
    spool.mark_revision_blocked(revision.revision_id, "publication unsupported")
    transport.video_gate = None
    start = time.monotonic()
    assert asyncio.run(runner.flush(0)) is False
    assert time.monotonic() - start < 0.1
    assert os.path.exists(artifact.local_ref)
    spool.close()


def test9():
    data = pd_fake_data("runner_long_outage")
    spool = Spool(SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000))
    transport = FakeTransport()
    cfg = RunnerConfig(retry_base_seconds=0, retry_max_seconds=0)
    runner = Runner(LeRobotAdapter(data["ready"]), spool, transport, cfg)
    revision = runner.admit()[0]
    video = next(value for value in revision.artifact_ids if spool.get_artifact(value).kind == "video")
    for value in revision.artifact_ids:
        artifact = spool.get_artifact(value)
        if artifact.kind == "video" and value != video:
            spool.mark_acknowledged(asyncio.run(transport.deliver(artifact, revision.destination)))
    for count in range(12):
        transport.fail_once.add(video)
        assert asyncio.run(runner.run_once("video"))
        assert spool.artifact_state(video) == "SENDING"
        assert asyncio.run(runner.run_once("video"))
        assert spool.artifact_state(video) == "RETRY_WAIT"
        assert spool.artifact_attempts(video) == count + 1
    assert spool.blocked_count() == 0
    asyncio.run(_drain(runner))
    assert runner.complete()
    assert transport.artifact_effects == 5
    assert spool.status()["spool_bytes"] == 0
    spool.close()

    data = pd_fake_data("runner_idempotent_lookup_unavailable")
    spool = Spool(SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000))

    class IdempotentTransport(FakeTransport):
        capabilities = TransportCapabilities(artifact_idempotency=True, revision_idempotency=True)

        async def reconcile(self, artifact, destination):
            raise TransportUnknown("lookup unavailable")

        async def reconcile_revision(self, revision):
            raise TransportUnknown("lookup unavailable")

        async def publish_revision(self, revision):
            receipt = await super().publish_revision(revision)
            if self.revision_effects == 1 and not getattr(self, "lost_revision", False):
                self.lost_revision = True
                raise TransportRetry("publication ACK lost")
            return receipt

    transport = IdempotentTransport()
    runner = Runner(LeRobotAdapter(data["ready"]), spool, transport, cfg)
    revision = runner.admit()[0]
    transport.lost_once.update(revision.artifact_ids)
    asyncio.run(_drain(runner))
    assert runner.complete()
    assert transport.artifact_effects == 5
    assert transport.revision_effects == 1
    spool.close()


def test_all():
    os_makedirs(dirtest)
    test1()
    test2()
    test3()
    test4()
    test5()
    test6()
    test7()
    test8()
    test9()
    log_info("test_runner: all passed")


if __name__ == '__main__':
    import fire
    fire.Fire()
