from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning
from src.utils.util_base import os_makedirs, os_path_cleanup

import asyncio
import json
import errno
import sqlite3
import select
import signal
import subprocess
from dataclasses import replace
from unittest.mock import patch

from vmodal_robot.adapters.lerobot import LeRobotAdapter
from vmodal_robot.runner import Runner, RunnerConfig
from vmodal_robot.spool import Spool, SpoolConfig
from vmodal_robot.contracts import ContractError, Receipt, SpoolFull, TransportCapabilities, TransportUnknown
from vmodal_robot.utils import str_sha256_file
from tests.utils_datafake import FakeTransport, pd_fake_data
from tests.utils_temporal_fake import ContractRaises


dirtest="ztmp/ztests"


def test1():
    data = pd_fake_data("spool_accept")
    item = LeRobotAdapter(data["ready"]).discover()[0]
    spool = Spool(SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000))
    revision = spool.accept(item)
    assert len(revision.artifact_ids) == 5
    source = item.artifacts[0].source_path
    artifact = spool.get_artifact(revision.artifact_ids[0])
    assert os.path.basename(artifact.local_ref) == artifact.artifact_id + ".mp4"
    before = open(artifact.local_ref, "rb").read()
    with open(source, "wb") as handle:
        handle.write(b"producer changed after handoff")
    assert open(artifact.local_ref, "rb").read() == before
    assert os.path.isfile(source)
    spool.close()


def test2():
    data = pd_fake_data("spool_restart")
    item = LeRobotAdapter(data["ready"]).discover()[0]
    cfg = SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000)
    spool = Spool(cfg)
    first = spool.accept(item)
    dataset_id = first.dataset_id
    spool.close()
    spool = Spool(cfg)
    second = spool.accept(item)
    assert second.revision_id == first.revision_id
    assert second.dataset_id == dataset_id
    spool.close()


def test3():
    data = pd_fake_data("spool_changed_metadata")
    adapter = LeRobotAdapter(data["ready"])
    item = adapter.discover()[0]
    spool = Spool(SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000))
    first = spool.accept(item)
    path = os.path.join(data["dataset"], "meta", "info.json")
    value = b'{"codebase_version":"v3.1"}'
    with open(path, "wb") as handle:
        handle.write(value)
    manifest = data["data"]
    row = [entry for entry in manifest["artifacts"] if entry["path"] == "meta/info.json"][0]
    import hashlib
    row["size_bytes"] = len(value)
    row["sha256"] = hashlib.sha256(value).hexdigest()
    with open(data["manifest"], "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    second = spool.accept(LeRobotAdapter(data["ready"]).discover()[0])
    assert second.revision_id != first.revision_id
    assert second.dataset_id == first.dataset_id
    assert len(set(first.artifact_ids).intersection(second.artifact_ids)) == 4
    spool.close()


def test4():
    data = pd_fake_data("spool_orphan")
    root = os.path.join(data["root"], "spool")
    path = os.path.join(root, "objects", "aa", "a" * 64, "payload")
    os_makedirs(os.path.dirname(path))
    with open(path, "wb") as handle:
        handle.write(b"orphan")
    spool = Spool(SpoolConfig(root, max_bytes=100000, aux_reserve_bytes=1000))
    assert not os.path.exists(path)
    spool.close()

    data = pd_fake_data("spool_postcommit_error")
    item = LeRobotAdapter(data["ready"]).discover()[0]
    cfg = SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000)
    spool = Spool(cfg)
    with patch.object(spool, "get_revision", side_effect=OSError("postcommit read failed")):
        try:
            spool.accept(item)
        except OSError as exc:
            assert str(exc) == "postcommit read failed"
        else:
            assert False, "injected postcommit failure must propagate"
    assert not spool._preparing
    rows = spool.db.execute("SELECT local_ref, checksum FROM artifacts").fetchall()
    assert len(rows) == len(item.artifacts)
    assert all(os.path.isfile(row["local_ref"]) and str_sha256_file(row["local_ref"]) == row["checksum"] for row in rows)
    revision = spool.accept(item)
    spool.close()
    spool = Spool(cfg)
    assert spool.accept(item).revision_id == revision.revision_id
    assert spool.status()["record_count"] == len(item.artifacts)
    spool.close()


def test6():
    data = pd_fake_data("spool_legacy_names")
    cfg = SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000)
    spool = Spool(cfg)
    revision = spool.accept(LeRobotAdapter(data["ready"]).discover()[0])
    video = spool.get_artifact(revision.artifact_ids[0])
    old = os.path.join(os.path.dirname(video.local_ref), "payload")
    os.rename(video.local_ref, old)
    inode = os.stat(old).st_ino
    spool.db.execute("UPDATE artifacts SET local_ref=?, attempts=3, state='RETRY_WAIT' WHERE artifact_id=?", (old, video.artifact_id))
    spool.db.execute("ALTER TABLE rejections RENAME TO rejections_new")
    spool.db.execute("CREATE TABLE rejections (id INTEGER PRIMARY KEY AUTOINCREMENT, manifest_path TEXT NOT NULL, reason TEXT NOT NULL, rejected_at REAL NOT NULL)")
    spool.db.execute("INSERT INTO rejections VALUES (1, 'legacy.ready.json', 'invalid legacy manifest', 1)")
    spool.db.execute("DROP TABLE rejections_new")
    spool.db.execute("DROP TABLE spool_meta")
    aux = spool.get_artifact(revision.artifact_ids[2])
    spool.mark_acknowledged(Receipt(aux.artifact_id, "remote://legacy", "ACKNOWLEDGED", aux.checksum))
    spool.db.execute("ALTER TABLE receipts RENAME TO receipts_new")
    spool.db.execute("CREATE TABLE receipts (artifact_id TEXT PRIMARY KEY, remote_ref TEXT NOT NULL, status TEXT NOT NULL, checksum TEXT NOT NULL, detail TEXT NOT NULL, acknowledged_at REAL NOT NULL)")
    spool.db.execute("INSERT INTO receipts SELECT artifact_id, remote_ref, status, checksum, detail, acknowledged_at FROM receipts_new")
    spool.db.execute("DROP TABLE receipts_new")
    spool.db.commit()
    spool.close()
    spool = Spool(cfg)
    moved = spool.get_artifact(video.artifact_id)
    assert moved.local_ref == video.local_ref
    assert str_sha256_file(moved.local_ref) == video.checksum
    assert os.stat(moved.local_ref).st_ino == inode
    assert not os.path.exists(old)
    assert spool.get_revision(revision.revision_id) == revision
    assert spool.artifact_state(video.artifact_id) == "RETRY_WAIT"
    assert spool.artifact_attempts(video.artifact_id) == 3
    assert spool.status()["rejection_reasons"][0]["category"] == "invalid"
    backup = sqlite3.connect(os.path.join(spool.root, "state.before-fixes-v1.sqlite3"))
    assert backup.execute("SELECT local_ref FROM artifacts WHERE artifact_id=?", (video.artifact_id,)).fetchone()[0] == old
    assert "category" not in {row[1] for row in backup.execute("PRAGMA table_info(rejections)")}
    assert "checksum_verified" not in {row[1] for row in backup.execute("PRAGMA table_info(receipts)")}
    backup.close()
    assert spool.db.execute("SELECT checksum_verified FROM receipts WHERE artifact_id=?", (aux.artifact_id,)).fetchone()[0] == 0
    spool.mark_acknowledged(Receipt(aux.artifact_id, "remote://verified", "ACKNOWLEDGED", aux.checksum, checksum_verified=True))
    spool.close()
    spool = Spool(cfg)
    assert spool.db.execute("SELECT checksum_verified FROM receipts WHERE artifact_id=?", (aux.artifact_id,)).fetchone()[0] == 1
    spool.close()


def test7():
    for phase in ("before_commit", "after_commit", "no_space", "copy", "conflict"):
        data = pd_fake_data("spool_migrate_" + phase)
        cfg = SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000)
        spool = Spool(cfg)
        revision = spool.accept(LeRobotAdapter(data["ready"]).discover()[0])
        video = spool.get_artifact(revision.artifact_ids[0])
        old = os.path.join(os.path.dirname(video.local_ref), "payload")
        os.rename(video.local_ref, old)
        if phase != "after_commit":
            spool.db.execute("UPDATE artifacts SET local_ref=? WHERE artifact_id=?", (old, video.artifact_id))
            spool.db.commit()
        if phase in ("before_commit", "after_commit"):
            os.link(old, video.local_ref)
        elif phase == "conflict":
            with open(video.local_ref, "wb") as handle:
                handle.write(b"conflicting interrupted migration")
        spool.close()
        if phase == "no_space":
            usage = type("Usage", (), {"free": 0})()
            with patch("vmodal_robot.spool.os.link", side_effect=OSError(errno.EOPNOTSUPP, "links unsupported")), patch("vmodal_robot.spool.shutil.disk_usage", return_value=usage):
                spool = Spool(cfg)
            assert spool.get_artifact(video.artifact_id).local_ref == old
            assert spool.artifact_state(video.artifact_id) == "READY"
            assert spool.status()["video_naming_deferred"] == 1
            assert str_sha256_file(old) == video.checksum
            spool.os_recover()
        elif phase == "copy":
            with patch("vmodal_robot.spool.os.link", side_effect=OSError(errno.EOPNOTSUPP, "links unsupported")):
                spool = Spool(cfg)
        else:
            spool = Spool(cfg)
        if phase == "conflict":
            assert spool.artifact_state(video.artifact_id) == "BLOCKED"
            assert os.path.isfile(old) and os.path.isfile(video.local_ref)
            assert str_sha256_file(old) == video.checksum
        else:
            assert spool.get_artifact(video.artifact_id).local_ref == video.local_ref
            assert not os.path.exists(old)
            assert str_sha256_file(video.local_ref) == video.checksum
        spool.close()


def test8():
    _ack_rollback_check()
    _legacy_ack_check()
    data = pd_fake_data("spool_shared_references")
    cfg = SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000)
    spool = Spool(cfg)
    item = LeRobotAdapter(data["ready"]).discover()[0]
    first = spool.accept(item)
    second = spool.accept(replace(item, source_revision="second"))
    for artifact_id in first.artifact_ids:
        artifact = spool.get_artifact(artifact_id)
        spool.mark_acknowledged(Receipt(artifact_id, "remote://" + artifact_id, "ACKNOWLEDGED", artifact.checksum))
    spool.mark_revision_acknowledged(first.revision_id, Receipt(first.revision_id, "remote://first", "ACKNOWLEDGED"))
    assert spool.cleanup(0) == 0
    assert spool.pending_count() == 1
    spool.mark_revision_acknowledged(second.revision_id, Receipt(second.revision_id, "remote://second", "ACKNOWLEDGED"))
    assert spool.status()["cleanup_eligible_count"] == 5
    assert spool.cleanup(0) == 5
    assert spool.status()["historical_record_count"] == 5
    assert spool.status()["record_count"] == 0
    assert spool.accept(item).revision_id == first.revision_id
    for _ in range(3):
        spool.reject(item.manifest_path, "capacity deferred", "capacity")
    assert spool.status()["deferred_handoffs"] == 1
    assert spool.status()["rejected_handoffs"] == 1
    spool.accept(item)
    assert spool.status()["deferred_handoffs"] == 0
    spool.mark_revision_blocked(second.revision_id, "operator action")
    assert spool.blocked_count() == 1
    assert spool.status()["revision_counts"]["BLOCKED"] == 1
    last_cleanup = spool.status()["last_cleanup"]
    spool.close()
    spool = Spool(cfg)
    assert spool.status()["last_cleanup"] == last_cleanup
    spool.mark_revision_ready(second.revision_id)
    assert spool.claim_revision().revision_id == second.revision_id
    spool.mark_uncertain(second.revision_id, "lost publication ACK", 300, revision=True)
    assert not spool.uncertain_due(second.revision_id, revision=True)
    spool.close()
    spool = Spool(cfg)
    assert not spool.uncertain_due(second.revision_id, revision=True)
    spool.mark_uncertain(second.revision_id, "ready to reconcile", 0, revision=True)
    assert spool.uncertain_due(second.revision_id, revision=True)
    spool.close()


def _ack_rollback_check():
    data = pd_fake_data("spool_ack_rollback")
    spool = Spool(SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000))
    revision = spool.accept(LeRobotAdapter(data["ready"]).discover()[0])
    artifact, _ = spool.claim("video")
    receipt = Receipt(artifact.artifact_id, "remote://artifact", "ACKNOWLEDGED", artifact.checksum)
    spool.db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT, 'receipt failure'); END")
    try:
        spool.mark_acknowledged(receipt)
    except sqlite3.IntegrityError as exc:
        assert str(exc) == "receipt failure"
    else:
        assert False, "receipt write must fail"
    assert spool.artifact_state(artifact.artifact_id) == "SENDING"
    assert spool.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    spool.db.execute("DROP TRIGGER fail_receipt")
    for artifact_id in revision.artifact_ids:
        value = spool.get_artifact(artifact_id)
        spool.mark_acknowledged(Receipt(artifact_id, "remote://" + artifact_id, "ACKNOWLEDGED", value.checksum))
    assert spool.claim_revision().revision_id == revision.revision_id
    spool.db.execute("CREATE TRIGGER fail_revision_receipt BEFORE INSERT ON revision_receipts BEGIN SELECT RAISE(ABORT, 'revision receipt failure'); END")
    try:
        spool.mark_revision_acknowledged(revision.revision_id, Receipt(revision.revision_id, "remote://revision", "ACKNOWLEDGED"))
    except sqlite3.IntegrityError as exc:
        assert str(exc) == "revision receipt failure"
    else:
        assert False, "revision receipt write must fail"
    assert spool.revision_state(revision.revision_id) == "SENDING"
    assert spool.db.execute("SELECT COUNT(*) FROM revision_receipts").fetchone()[0] == 0
    assert spool.cleanup(0) == 0
    spool.close()


def _legacy_ack_check():
    for outcome in ("absent", "unknown", "verified"):
        data = pd_fake_data("spool_legacy_ack_" + outcome)
        spool = Spool(SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000))
        revision = spool.accept(LeRobotAdapter(data["ready"]).discover()[0])
        paths = []
        for artifact_id in revision.artifact_ids:
            artifact = spool.get_artifact(artifact_id)
            paths.append(artifact.local_ref)
            spool.mark_acknowledged(Receipt(artifact_id, "remote://legacy", "ACKNOWLEDGED", artifact.checksum))
        spool.mark_revision_acknowledged(revision.revision_id, Receipt(revision.revision_id, "remote://revision", "ACKNOWLEDGED"))

        class QualifiedTransport(FakeTransport):
            capabilities = TransportCapabilities(checksum_verified=True)

            async def reconcile(self, artifact, destination):
                if outcome == "unknown":
                    raise TransportUnknown("lookup unavailable")
                if outcome == "verified":
                    return Receipt(artifact.artifact_id, "remote://verified", "ACKNOWLEDGED", artifact.checksum, checksum_verified=True)
                return None

        runner = Runner(LeRobotAdapter(data["ready"]), spool, QualifiedTransport())
        asyncio.run(runner.recover())
        assert spool.cleanup(0) == (len(paths) if outcome == "verified" else 0)
        assert all(os.path.isfile(path) for path in paths) == (outcome != "verified")
        if outcome != "verified":
            expected = "SENDING" if outcome == "unknown" else "READY"
            assert all(spool.artifact_state(value) == expected for value in revision.artifact_ids)
        spool.close()


def test9():
    _pressure_snapshot_check()
    _immutable_contract_check()
    _ownership_check()
    data = pd_fake_data("spool_four_names")
    item = LeRobotAdapter(data["ready"]).discover()[0]
    first_video = item.artifacts[0]
    videos = tuple(replace(first_video, rel_path=f"videos/chunk-{chunk:03d}/cam-{camera}/file-000.mp4")
                   for chunk in range(2) for camera in range(2))
    item = replace(item, artifacts=videos + item.artifacts[2:])
    spool = Spool(SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000))
    revision = spool.accept(item)
    paths = [spool.get_artifact(value).local_ref for value in revision.artifact_ids[:4]]
    assert len(set(paths)) == 4
    assert all(os.path.basename(path).endswith(".mp4") and len(os.path.basename(path)) == 68 for path in paths)
    assert all(str_sha256_file(path) == first_video.checksum for path in paths)
    orphan_id = "f" * 64
    orphan = spool._object_path(orphan_id, "video")
    os_makedirs(os.path.dirname(orphan))
    with open(orphan, "wb") as handle:
        handle.write(b"orphan-video")
    unknown = os.path.join(os.path.dirname(orphan), "keep.me")
    with open(unknown, "wb") as handle:
        handle.write(b"unknown file")
    spool.os_recover()
    assert not os.path.exists(orphan)
    assert os.path.isfile(unknown)
    spool.close()


def _immutable_contract_check():
    data = pd_fake_data("spool_immutable_contract_audit")
    item = LeRobotAdapter(data["ready"]).discover()[0]
    spool = Spool(SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000))
    revision = spool.accept(item)
    for fields in ({"source_refs": {"episodes": [99]}}, {"time_start": 2.0},
                   {"time_end": 9.0}, {"source_clock": "different-clock"},
                   {"kind": "opaque"}, {"content_type": "different/type"}):
        changed = replace(item.artifacts[0], **fields)
        with ContractRaises(ContractError, "immutable artifact contract conflict"):
            spool.accept(replace(item, source_revision="corrected", artifacts=(changed,) + item.artifacts[1:]))
    for fields in ({"complete": False}, {"source_format": "different"}, {"source_version": "v4"}):
        with ContractRaises(ContractError, "immutable revision contract conflict"):
            spool.accept(replace(item, **fields))
    assert spool.get_revision(revision.revision_id) == revision
    assert spool.db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 1
    assert spool.status()["record_count"] == len(item.artifacts)
    assert not spool._preparing
    spool.close()


def _hold_spool(root):
    spool = Spool(SpoolConfig(root, max_bytes=100000, aux_reserve_bytes=1000))
    print("LOCKED", flush=True)
    signal.pause()


def _pressure_snapshot_check():
    data = pd_fake_data("spool_pressure_snapshot")
    cfg = SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000,
                      aux_reserve_bytes=1000, max_records=6, max_video_bytes=5000)
    spool = Spool(cfg)
    adapter = LeRobotAdapter(data["ready"])
    runner = Runner(adapter, spool, FakeTransport(), RunnerConfig(retention_seconds=3600))
    assert len(runner.admit()) == 1
    status = spool.status()
    assert status["discovery"]["admitted"] == 1
    assert status["admission_pressure"]["recommended_action"] == "continue"
    assert status["admission_pressure"]["record_headroom"] == 1
    assert status["admission_pressure"]["next_manifest_fits"] is None
    source_paths = [value.source_path for value in adapter.os_load_manifest(data["manifest"]).artifacts]
    class Disk:
        f_bavail = 500
        f_frsize = 1
    with patch("vmodal_robot.spool.os.statvfs", return_value=Disk()):
        pressure = spool.status()["admission_pressure"]
        assert pressure["recommended_action"] == "pause_video_recording"
        assert pressure["video_headroom"] == 0
        assert not pressure["pause_recording"]
    spool.reject(data["manifest"], "spool byte quota exceeded: 100000", "capacity")
    assert spool.status()["admission_pressure"]["pause_recording"]
    with Spool(SpoolConfig(cfg.root, max_bytes=100000), readonly=True) as observer:
        observer.db.execute("BEGIN")
        observed = observer.status()
        assert observed["configuration"]["max_bytes"] == cfg.max_bytes
        assert observed["configuration"]["retention_seconds"] == 3600
        assert observed["max_video_bytes"] == 5000
        assert observed["discovery"] == status["discovery"]
        assert observed["admission_pressure"]["latest_deferral"]["category"] == "capacity"
        assert observer.cfg.max_records == 6
        with ContractRaises(RuntimeError, "readonly spool cannot admit data"):
            observer.prepare_accept(adapter.os_load_manifest(data["manifest"]))
        with ContractRaises(RuntimeError, "readonly spool cannot clean data"):
            observer.cleanup(0)
    assert all(os.path.isfile(path) for path in source_paths)
    spool.close()
    with Spool(cfg) as reopened:
        assert reopened.status()["discovery"] == status["discovery"]
        assert reopened.status()["admission_pressure"]["pause_recording"]
        runner = Runner(LeRobotAdapter(data["ready"]), reopened, FakeTransport())
        assert len(runner.admit()) == 1
        assert not reopened.status()["admission_pressure"]["pause_recording"]
        assert reopened.status()["discovery"]["admitted"] == 1
        assert reopened.status()["discovery"]["started_at"] != status["discovery"]["started_at"]


def _ownership_check():
    data = pd_fake_data("spool_single_owner_audit")
    item = LeRobotAdapter(data["ready"]).discover()[0]
    cfg = SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000)
    spool = Spool(cfg)
    plan = spool.prepare_accept(item)
    spool.copy_accept(plan)
    paths = list(plan["copied"])
    with ContractRaises(RuntimeError, "spool already in use"):
        Spool(cfg)
    assert all(os.path.isfile(path) for path in paths)
    revision = spool.commit_accept(plan)
    spool.close()
    with Spool(cfg) as reopened:
        assert reopened.get_revision(revision.revision_id) == revision
    with patch.object(Spool, "_schema", side_effect=OSError("startup failure")):
        with ContractRaises(OSError, "startup failure"):
            Spool(cfg)
    with Spool(cfg) as reopened:
        assert reopened.get_revision(revision.revision_id) == revision
    with subprocess.Popen([sys.executable, __file__, "_hold_spool", "--root=" + cfg.root],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as child:
        try:
            assert select.select([child.stdout], [], [], 3)[0], "child must acquire the lock"
            assert child.stdout.readline().strip() == "LOCKED"
            with ContractRaises(RuntimeError, "spool already in use"):
                Spool(cfg)
        finally:
            child.kill()
            child.wait(timeout=3)
    with Spool(cfg) as reopened:
        assert reopened.get_revision(revision.revision_id) == revision


def test5():
    data = pd_fake_data("spool_cleanup")
    spool = Spool(SpoolConfig(os.path.join(data["root"], "spool"), max_bytes=100000, aux_reserve_bytes=1000))
    revision = spool.accept(LeRobotAdapter(data["ready"]).discover()[0])
    runner = Runner(LeRobotAdapter(os.path.join(data["root"], "none")), spool, FakeTransport(), RunnerConfig(retention_seconds=3600))

    async def drain():
        while await runner.cycle(admit=False):
            pass

    asyncio.run(drain())
    producer_paths = [value.source_path for value in LeRobotAdapter(data["ready"]).discover()[0].artifacts]
    owned_paths = [spool.get_artifact(value).local_ref for value in revision.artifact_ids]
    assert spool.cleanup(0) == 5
    assert all(os.path.isfile(value) for value in producer_paths)
    assert not any(os.path.exists(value) for value in owned_paths)
    assert spool.status()["spool_bytes"] == 0
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
    log_info("test_spool: all passed")


if __name__ == '__main__':
    import fire
    fire.Fire()
