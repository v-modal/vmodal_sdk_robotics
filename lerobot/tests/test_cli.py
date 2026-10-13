from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass, replace
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning
from src.utils.util_base import os_makedirs, os_path_cleanup

import asyncio
import json
import select
import signal
import subprocess
import time
from unittest.mock import patch

from vmodal_robot.cli import RobotCli, _run, _spool
from vmodal_robot.runner import Runner, RunnerConfig
from vmodal_robot.spool import Spool, SpoolConfig
from vmodal_robot.contracts import SpoolFull, TransportBlocked
from vmodal_robot.transports.vmodal import VmodalTransport
from tests.utils_datafake import FakeTransport, pd_fake_data, os_save_bytes, str_sha
from tests.test_vmodal import FakeArtifactApi


dirtest="ztmp/ztests"


class _EmptyAdapter:
    def discover(self, limit):
        return []


class _SignalTransport(FakeTransport):
    def __init__(self, phase):
        super().__init__()
        self.phase = phase

    async def _gate(self):
        print("READY:" + self.phase, flush=True)
        await asyncio.Event().wait()

    async def deliver(self, artifact, destination):
        lane = "video" if artifact.kind == "video" else "aux"
        if lane == self.phase:
            await self._gate()
        return await super().deliver(artifact, destination)

    async def reconcile(self, artifact, destination):
        if self.phase == "recovery":
            await self._gate()
        return await super().reconcile(artifact, destination)

    async def publish_revision(self, revision):
        if self.phase == "publish":
            await self._gate()
        return await super().publish_revision(revision)

    async def close(self):
        if self.phase == "close":
            await self._gate()


def signal_child(phase: str, budget: float = 1.0):
    """Run a real signal-handling lifecycle with a gated transport phase."""
    data = pd_fake_data("cli_signal_" + phase)
    spool = Spool(SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000))
    transport = _SignalTransport(phase)
    adapter = _EmptyAdapter()
    if phase == "copy":
        from vmodal_robot.adapters.lerobot import LeRobotAdapter
        adapter = LeRobotAdapter(data["ready"])
    elif phase not in ("idle", "close"):
        from vmodal_robot.adapters.lerobot import LeRobotAdapter
        spool.accept(LeRobotAdapter(data["ready"]).discover()[0])
    if phase == "recovery":
        spool.claim("video")
    runner = Runner(adapter, spool, transport, RunnerConfig(poll_seconds=120, request_timeout_seconds=120))

    async def run():
        if phase == "idle":
            asyncio.get_running_loop().call_later(0.05, lambda: print("READY:idle", flush=True))
        return await _run(runner, budget, flush=phase == "close")

    def copy_gate(source, destination, stop_event=None):
        assert stop_event is not None, "admission copy must have cooperative cancellation"
        print("READY:copy", flush=True)
        stop_event.wait()
        raise InterruptedError("copy stopped before durable admission")

    try:
        if phase == "copy":
            with patch("vmodal_robot.spool.os_atomic_copy", copy_gate):
                completed = asyncio.run(run())
            assert spool.status()["historical_record_count"] == 0
            assert not spool._preparing
            for artifact in data["data"]["artifacts"]:
                assert os.path.isfile(os.path.join(data["dataset"], artifact["path"]))
        else:
            completed = asyncio.run(run())
    finally:
        spool.close()
    raise SystemExit(0 if completed else 1)


def test1():
    if os.name != "posix":
        return
    for phase in ("video", "aux", "recovery", "publish", "idle", "close", "copy"):
        proc = subprocess.Popen(
            [sys.executable, __file__, "signal_child", "--phase=" + phase],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            assert select.select([proc.stdout], [], [], 5)[0], phase
            line = proc.stdout.readline()
            assert line.strip() == "READY:" + phase, (phase, line)
            start = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            output, _ = proc.communicate(timeout=3)
            assert time.monotonic() - start < 2.0, (phase, output)
            assert proc.returncode == (0 if phase == "idle" else 1), (phase, output)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def test2():
    if os.name != "posix":
        return
    for budget, twice in ((0, False), (30, True)):
        proc = subprocess.Popen(
            [sys.executable, __file__, "signal_child", "--phase=video", "--budget=" + str(budget)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            assert select.select([proc.stdout], [], [], 5)[0]
            assert proc.stdout.readline().strip() == "READY:video"
            start = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            if twice:
                time.sleep(0.05)
                proc.send_signal(signal.SIGTERM)
            output, _ = proc.communicate(timeout=2)
            assert proc.returncode == 1, output
            assert time.monotonic() - start < 1
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def test3():
    root = os.path.abspath(os.path.join(dirtest, "cli_invalid_transport"))
    os_path_cleanup(root)

    class InvalidTransport(FakeTransport):
        def validate_capabilities(self, limit):
            raise TransportBlocked("generic durable storage required")

        async def close(self):
            self.closed_loop = asyncio.get_running_loop()

    transport = InvalidTransport()
    with patch("vmodal_robot.cli.VmodalTransport.from_env", return_value=transport):
        try:
            RobotCli().flush(spool_dir=root)
        except TransportBlocked as exc:
            assert "generic durable storage" in str(exc)
        else:
            assert False, "unsupported stock transport must fail before spool creation"
    assert not os.path.exists(root)
    assert transport.closed_loop.is_closed()


def test4():
    data = pd_fake_data("cli_flush_exit")

    class QualifiedTransport(FakeTransport):
        def validate_capabilities(self, limit):
            self.loop = asyncio.get_running_loop()

        async def close(self):
            assert asyncio.get_running_loop() is self.loop

    cfg = SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000)
    spool = Spool(cfg)
    from vmodal_robot.adapters.lerobot import LeRobotAdapter
    revision = spool.accept(LeRobotAdapter(data["ready"]).discover()[0])
    spool.mark_revision_blocked(revision.revision_id, "operator action required")
    spool.close()
    with patch("vmodal_robot.cli.VmodalTransport.from_env", return_value=QualifiedTransport()):
        try:
            RobotCli().flush(spool_dir=cfg.root, deadline_seconds=0)
        except SystemExit as exc:
            assert exc.code == 1
        else:
            assert False, "incomplete CLI flush must exit nonzero"


def test5():
    api = FakeArtifactApi()
    api.capabilities = replace(api.capabilities, max_video_bytes=256 * 1024 * 1024)
    observed = []

    async def inspect(runner, deadline, flush=False):
        observed.append((runner.spool.cfg.max_video_bytes, runner.cfg.retention_seconds, flush))
        return True

    for env, limit, retention, expected in (
        ({}, None, None, (100 * 1024 * 1024, 0.0)),
        ({"VMODAL_ROBOT_MAX_VIDEO_BYTES": "64", "VMODAL_ROBOT_RETENTION_SECONDS": "120"}, None, None, (64, 120.0)),
        ({"VMODAL_ROBOT_MAX_VIDEO_BYTES": "bad", "VMODAL_ROBOT_RETENTION_SECONDS": "bad"}, 80, 0, (80, 0.0)),
    ):
        data = pd_fake_data("cli_limits_" + str(expected[0]))
        root = os.path.join(data["root"], "spool")
        with patch.dict(os.environ, env, clear=True), \
             patch("vmodal_robot.cli.VmodalTransport.from_env", return_value=VmodalTransport(None, api)), \
             patch("vmodal_robot.cli._run", inspect):
            RobotCli().run(data["ready"], root, 100000, 1000, 100, 1, 120, 30, limit, retention)
            RobotCli().flush(root, 60, 100000, 1000, 100, limit, retention)
            values = json.loads(RobotCli().status(root, 100000, 1000, 100, limit, retention))
        assert observed[-2:] == [(*expected, False), (*expected, True)]
        assert (values["max_video_bytes"], values["retention_seconds"]) == expected
        assert values["transport_qualification"]["qualified"] is False
        assert values["transport_qualification"]["max_video_bytes"] == 100 * 1024 * 1024
        for name in ("storage:telemetry", "storage:metadata", "storage:opaque", "checksum_verified", "artifact_idempotency", "revision_publication"):
            assert name in values["transport_qualification"]["reason"]


def test6():
    root = os.path.abspath(os.path.join(dirtest, "cli_invalid_limits"))
    os_path_cleanup(root)
    cli = RobotCli()
    for value in (0, -1, 1.5, True, "bad", "1.5"):
        for call in (cli.run, cli.flush, cli.status):
            try:
                call(spool_dir=root, max_video_bytes=value)
            except ValueError as exc:
                assert "positive integer" in str(exc)
            else:
                assert False, value
        with patch.dict(os.environ, {"VMODAL_ROBOT_MAX_VIDEO_BYTES": str(value)}):
            try:
                cli.status(spool_dir=root)
            except ValueError:
                pass
            else:
                assert False, value
    for value in (-1, "nan", "inf"):
        for call in (cli.run, cli.flush, cli.status):
            try:
                call(spool_dir=root, retention_seconds=value)
            except ValueError as exc:
                assert "finite and nonnegative" in str(exc)
            else:
                assert False, value
    assert not os.path.exists(root)
    proc = subprocess.run(
        [sys.executable, "-m", "vmodal_robot.cli", "status", "--spool_dir=" + root, "--max_video_bytes=0"],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "positive integer" in proc.stderr
    assert not os.path.exists(root)


def test7():
    from vmodal_robot.adapters.lerobot import LeRobotAdapter
    data = pd_fake_data("cli_transport_limit")
    root = os.path.join(data["root"], "spool")
    api = FakeArtifactApi()
    api.capabilities = replace(api.capabilities, max_video_bytes=64)
    for call in (RobotCli().run, RobotCli().flush):
        with patch("vmodal_robot.cli.VmodalTransport.from_env", return_value=VmodalTransport(None, api)):
            try:
                call(spool_dir=root, max_bytes=100000, aux_reserve_bytes=1000, max_video_bytes=65)
            except TransportBlocked as exc:
                assert "exceeds qualified transport limit 64" in str(exc)
            else:
                assert False, "incompatible limit must fail before creating spool"
    assert not os.path.exists(root)
    item = LeRobotAdapter(data["ready"]).discover()[0]
    artifacts = []
    for artifact in item.artifacts:
        if artifact.kind == "video":
            os_save_bytes(artifact.source_path, b"v" * 64)
            artifact = replace(artifact, byte_length=64, checksum=str_sha(b"v" * 64))
        artifacts.append(artifact)
    item = replace(item, artifacts=tuple(artifacts))
    spool = _spool(root, 100000, 1000, 100, 64)
    revision = spool.accept(item)
    assert spool.cfg.max_video_bytes == 64
    spool.close()
    with patch("vmodal_robot.cli.VmodalTransport.from_env", return_value=VmodalTransport(None, api)):
        assert RobotCli().flush(root, max_bytes=100000, aux_reserve_bytes=1000,
                               max_video_bytes=64, retention_seconds=3600)
    values = json.loads(RobotCli().status(root, 100000, 1000, 100, 64, 3600))
    assert values["cleanup_eligible_count"] == 0
    assert values["revision_counts"]["ACKNOWLEDGED"] == 1
    assert json.loads(RobotCli().status(root, 100000, 1000, 100, 64, 0))["cleanup_eligible_count"] == len(item.artifacts)
    assert sum(payload == b"v" * 64 for payload in api.payloads.values()) == 2
    larger = artifacts[0]
    os_save_bytes(larger.source_path, b"v" * 65)
    larger = replace(larger, byte_length=65, checksum=str_sha(b"v" * 65))
    spool = _spool(root, 100000, 1000, 100, 64)
    before = spool.status()["historical_record_count"]
    try:
        spool.accept(replace(item, source_revision="oversized", artifacts=(larger,) + tuple(artifacts[1:])))
    except SpoolFull:
        pass
    else:
        assert False, "limit plus one must reject the whole revision"
    assert spool.status()["historical_record_count"] == before
    assert os.path.getsize(larger.source_path) == 65
    assert spool.revision_state(revision.revision_id) == "ACKNOWLEDGED"
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
    log_info("test_cli: all passed")


if __name__ == '__main__':
    import fire
    fire.Fire()
