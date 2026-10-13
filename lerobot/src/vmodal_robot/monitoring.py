"""Read-only spool snapshots and a continuous Prometheus HTTP endpoint."""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from vmodal_robot._support.util_log import log_info, log_error, log_trace, log_warning

import math
import signal
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .spool import Spool, SpoolConfig


def spool_snapshot(spool_dir: str, max_bytes: int = 10 * 1024 * 1024 * 1024,
                   retention_seconds: Optional[float] = None,
                   aux_reserve_bytes: int = 512 * 1024 * 1024,
                   max_records: int = 10000,
                   max_video_bytes: int = 1024 * 1024 * 1024) -> Dict[str, Any]:
    """Use the spool status contract without starting recovery or a writer."""
    root = os.path.abspath(os.path.expanduser(spool_dir))
    path = os.path.join(root, "state.sqlite3")
    if not os.path.isfile(path):
        raise FileNotFoundError("spool database does not exist")
    if max_bytes <= 0 or (retention_seconds is not None and
                         (not math.isfinite(retention_seconds) or retention_seconds < 0)):
        raise ValueError("max_bytes must be positive and retention_seconds finite and nonnegative")
    cfg = SpoolConfig(root, max_bytes=max_bytes, aux_reserve_bytes=aux_reserve_bytes,
                      max_records=max_records, max_video_bytes=max_video_bytes)
    spool = Spool(cfg, readonly=True)
    try:
        spool.db.execute("BEGIN")
        return spool.status(retention_seconds)
    finally:
        spool.close()


def str_prometheus(status: Dict[str, Any]) -> str:
    """Format documented numeric status fields; paths and credentials are absent."""
    lines = ["# TYPE vmodal_robot_spool_up gauge", "vmodal_robot_spool_up 1"]

    def metric(name, value, labels=""):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("metric must be finite: " + name)
        lines.append(f"vmodal_robot_{name}{labels} {value:.12g}")

    for name in ("spool_bytes", "record_count", "historical_record_count", "disk_free_bytes",
                 "disk_headroom_bytes", "blocked_count", "retry_count", "rejected_handoffs",
                 "deferred_handoffs", "cleanup_eligible_count", "video_naming_deferred"):
        lines.append(f"# TYPE vmodal_robot_{name} gauge")
        metric(name, status.get(name, 0))
    for name in ("pending_count", "pending_bytes", "oldest_age_seconds"):
        lines.append(f"# TYPE vmodal_robot_lane_{name} gauge")
        for lane in ("video", "aux"):
            metric("lane_" + name, status["lanes"][lane][name], '{lane="' + lane + '"}')
    discovery = status.get("discovery")
    lines.append("# TYPE vmodal_robot_discovery_observed gauge")
    metric("discovery_observed", int(bool(discovery)))
    if discovery:
        for name in ("manifests_present", "unseen", "pending", "deferred", "rejected", "admitted"):
            if name in discovery:
                lines.append(f"# TYPE vmodal_robot_discovery_{name} gauge")
                metric("discovery_" + name, discovery[name])
        if discovery.get("observed_at") is not None:
            lines.append("# TYPE vmodal_robot_discovery_age_seconds gauge")
            metric("discovery_age_seconds", max(0, time.time() - discovery["observed_at"]))
    pressure = status.get("admission_pressure")
    if pressure:
        for name in ("pause_recording", "pause_video_recording"):
            lines.append(f"# TYPE vmodal_robot_{name} gauge")
            metric(name, int(bool(pressure[name])))
    return "\n".join(lines) + "\n"


def metrics_server(spool_dir: str, host: str = "127.0.0.1", port: int = 9108,
                   max_bytes: int = 10 * 1024 * 1024 * 1024,
                   retention_seconds: Optional[float] = None) -> ThreadingHTTPServer:
    """Build an HTTP server; each request owns a fresh read-only DB connection."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            code = 200
            try:
                body = str_prometheus(spool_snapshot(spool_dir, max_bytes, retention_seconds))
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError) as exc:
                log_warning("spool metrics unavailable", reason=str(exc))
                code, body = 503, "# TYPE vmodal_robot_spool_up gauge\nvmodal_robot_spool_up 0\n"
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)


def serve(spool_dir: str, host: str = "127.0.0.1", port: int = 9108,
          max_bytes: int = 10 * 1024 * 1024 * 1024,
          retention_seconds: Optional[float] = None):
    """Continuously serve /metrics; missing/unreadable spool returns HTTP 503."""
    server = metrics_server(spool_dir, host, port, max_bytes, retention_seconds)
    previous = signal.getsignal(signal.SIGTERM)

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        log_info("spool metrics listening", host=host, port=server.server_port)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
        server.server_close()


def main():
    fire.Fire({"serve": serve}, name="vmodal-robot-metrics")


if __name__ == "__main__":
    main()
