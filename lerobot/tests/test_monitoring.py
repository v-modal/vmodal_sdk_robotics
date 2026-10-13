"""Prometheus snapshots remain read-only while the uploader writes and retries."""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning
from src.utils.util_base import os_makedirs, os_path_cleanup

import http.client
import select
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

from vmodal_robot.monitoring import metrics_server, spool_snapshot, str_prometheus
from vmodal_robot.adapters.lerobot import LeRobotAdapter
from vmodal_robot.spool import Spool, SpoolConfig
from tests.utils_datafake import pd_fake_data

dirtest="ztmp/ztests"


def test1():
    data = pd_fake_data("monitoring_snapshot")
    root = os.path.join(data["root"], "spool")
    spool = Spool(SpoolConfig(root))
    revision = spool.accept(LeRobotAdapter(data["ready"]).discover()[0])
    artifact, _ = spool.claim("video")
    before = spool.artifact_state(artifact.artifact_id)
    status = spool_snapshot(root)
    assert status["lanes"]["video"]["pending_count"] == 2
    assert status["lanes"]["aux"]["pending_count"] == 3
    assert status["spool_bytes"] > 0
    assert spool.artifact_state(artifact.artifact_id) == before == "SENDING"
    text = str_prometheus(status)
    assert 'vmodal_robot_lane_pending_count{lane="video"} 2\n' in text
    assert 'vmodal_robot_lane_pending_bytes{lane="aux"}' in text
    assert 'vmodal_robot_lane_oldest_age_seconds{lane="video"}' in text
    assert "vmodal_robot_spool_bytes " in text and "vmodal_robot_retry_count 0\n" in text
    assert root not in text and "remote_ref" not in text
    spool.mark_retry(artifact.artifact_id, "temporary outage", 0)
    second, _ = spool.claim("video")
    spool.mark_retry(second.artifact_id, "temporary outage", 0)
    spool.claim("video")
    assert spool_snapshot(root)["retry_count"] == 1
    spool.close()


def test2():
    data = pd_fake_data("monitoring_http")
    root = os.path.join(data["root"], "spool")
    server = metrics_server(root, port=0)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
    worker.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET", "/metrics")
        response = conn.getresponse()
        assert response.status == 503
        assert b"vmodal_robot_spool_up 0" in response.read()
        assert not os.path.exists(root)
        os_makedirs(root)
        with open(os.path.join(root, "state.sqlite3"), "wb") as handle:
            handle.write(b"invalid database")
        conn.request("GET", "/metrics")
        response = conn.getresponse()
        assert response.status == 503
        assert b"vmodal_robot_spool_up 0" in response.read()
        os.remove(os.path.join(root, "state.sqlite3"))
        spool = Spool(SpoolConfig(root))
        spool.accept(LeRobotAdapter(data["ready"]).discover()[0])
        for _ in range(3):
            conn.request("GET", "/metrics")
            response = conn.getresponse()
            assert response.status == 200
            assert "version=0.0.4" in response.getheader("Content-Type")
            assert b"vmodal_robot_spool_up 1" in response.read()
        conn.request("GET", "/unknown")
        response = conn.getresponse()
        assert response.status == 404
        response.read()
        conn.close()
        spool.close()
    finally:
        server.shutdown()
        worker.join(5)
        server.server_close()
    assert not worker.is_alive()


def test3():
    data = pd_fake_data("monitoring_concurrent")
    root = os.path.join(data["root"], "spool")
    spool = Spool(SpoolConfig(root))
    spool.accept(LeRobotAdapter(data["ready"]).discover()[0])
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(spool_snapshot, root) for _ in range(20)]
        artifact, _ = spool.claim("video")
        spool.mark_retry(artifact.artifact_id, "outage", 0)
        for future in futures:
            assert future.result()["record_count"] == 5
    assert spool.artifact_state(artifact.artifact_id) == "RETRY_WAIT"
    spool.close()


def test4():
    data = pd_fake_data("monitoring_pressure")
    root = os.path.join(data["root"], "spool")
    cfg = SpoolConfig(root, max_bytes=100000, aux_reserve_bytes=1000,
                      max_records=6, max_video_bytes=5000)
    spool = Spool(cfg)
    adapter = LeRobotAdapter(data["ready"])
    list(adapter.discover())
    spool.record_discovery(adapter.discovery_status())
    spool.record_retention(123)
    spool.reject(data["manifest"], "capacity temporarily exceeded", "capacity")
    status = spool_snapshot(root)
    assert status["configuration"]["max_bytes"] == 100000
    assert status["configuration"]["retention_seconds"] == 123
    assert status["max_video_bytes"] == 5000
    text = str_prometheus(status)
    assert "vmodal_robot_discovery_observed 1\n" in text
    assert "vmodal_robot_discovery_manifests_present 1\n" in text
    assert "vmodal_robot_discovery_age_seconds " in text
    assert "vmodal_robot_pause_recording 1\n" in text
    assert "vmodal_robot_pause_video_recording 1\n" in text
    assert "vmodal_robot_disk_headroom_bytes 100000\n" in text
    spool.close()


def test5():
    data = pd_fake_data("monitoring_signal")
    root = os.path.join(data["root"], "missing-spool")
    proc = subprocess.Popen([sys.executable, "-m", "vmodal_robot.monitoring", "serve",
                             "--spool_dir=" + root, "--port=0"], stderr=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    try:
        ready, _, _ = select.select([proc.stderr], [], [], 5)
        assert ready, "exporter did not start"
        assert b"spool metrics listening" in proc.stderr.readline()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0
        assert not os.path.exists(root)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        proc.stderr.close()
        proc.stdout.close()


def test_all():
    os_makedirs(dirtest)
    test1()
    test2()
    test3()
    test4()
    test5()
    log_info("test_monitoring: all passed")


if __name__ == '__main__':
    fire.Fire()
