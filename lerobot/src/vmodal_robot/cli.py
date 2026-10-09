from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

import asyncio
import json
import math
import signal

from .adapters.lerobot import LeRobotAdapter
from .contracts import TransportBlocked
from .runner import Runner, RunnerConfig
from .spool import Spool, SpoolConfig
from .transports.vmodal import VmodalTransport


def os_path_value(value: str, env_name: str, default: str) -> str:
    return os.path.abspath(os.path.expanduser(value or os.environ.get(env_name, default)))


def _int_value(value: int, env_name: str, default: int) -> int:
    return int(value) if int(value) > 0 else int(os.environ.get(env_name, default))


def _video_limit(value: Optional[int]) -> int:
    value = os.environ.get("VMODAL_ROBOT_MAX_VIDEO_BYTES", 100 * 1024 * 1024) if value is None else value
    if type(value) not in (int, str) or not str(value).isdigit() or int(value) <= 0:
        raise ValueError("max_video_bytes must be a positive integer number of bytes")
    return int(value)


def _retention_value(value: Optional[float]) -> float:
    value = float(os.environ.get("VMODAL_ROBOT_RETENTION_SECONDS", 0) if value is None else value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("retention_seconds must be finite and nonnegative")
    return value


def _spool(spool_dir: str, max_bytes: int, aux_reserve_bytes: int, max_records: int,
           max_video_bytes: Optional[int] = None) -> Spool:
    root = os_path_value(spool_dir, "VMODAL_ROBOT_SPOOL_DIR", "./vmodal_robot_spool")
    return Spool(
        SpoolConfig(
            root=root,
            max_bytes=_int_value(max_bytes, "VMODAL_ROBOT_MAX_BYTES", 10 * 1024 * 1024 * 1024),
            aux_reserve_bytes=_int_value(
                aux_reserve_bytes, "VMODAL_ROBOT_AUX_RESERVE_BYTES", 512 * 1024 * 1024
            ),
            max_records=_int_value(max_records, "VMODAL_ROBOT_MAX_RECORDS", 10000),
            max_video_bytes=_video_limit(max_video_bytes),
        )
    )


async def _run(runner: Runner, shutdown_deadline: float, flush: bool = False) -> bool:
    loop = asyncio.get_running_loop()
    installed = []
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, runner.stop, shutdown_deadline)
            installed.append(name)
        except NotImplementedError:
            pass
    completed = False
    try:
        completed = await (runner.flush(shutdown_deadline) if flush else runner.run())
    finally:
        try:
            closed = await runner.close()
        finally:
            for name in installed:
                loop.remove_signal_handler(name)
    log_info("robot drain finished", completed=completed, transport_closed=closed)
    return completed and closed


async def _session(ready: str, spool_dir: str, max_bytes: int, aux_reserve_bytes: int,
                   max_records: int, cfg: RunnerConfig, deadline: float, flush: bool = False,
                   max_video_bytes: Optional[int] = None) -> bool:
    limit = _video_limit(max_video_bytes)
    transport = VmodalTransport.from_env()
    spool = None
    try:
        transport.validate_capabilities(limit)
        spool = _spool(spool_dir, max_bytes, aux_reserve_bytes, max_records, limit)
        runner = Runner(LeRobotAdapter(ready), spool, transport, cfg)
        return await _run(runner, deadline, flush)
    finally:
        if spool is not None:
            spool.close()
        else:
            await asyncio.wait_for(transport.close(), cfg.request_timeout_seconds)


class RobotCli:
    def run(
        self,
        ready_dir: str = "",
        spool_dir: str = "",
        max_bytes: int = 0,
        aux_reserve_bytes: int = 0,
        max_records: int = 0,
        poll_seconds: float = 1.0,
        request_timeout_seconds: float = 120.0,
        shutdown_deadline: float = 30.0,
        max_video_bytes: Optional[int] = None,
        retention_seconds: Optional[float] = None,
    ):
        """Continuously accept ready manifests and deliver their artifacts."""
        ready = os_path_value(ready_dir, "VMODAL_ROBOT_READY_DIR", "./ready")
        cfg = RunnerConfig(poll_seconds=poll_seconds, request_timeout_seconds=request_timeout_seconds,
                           retention_seconds=_retention_value(retention_seconds))
        completed = asyncio.run(_session(ready, spool_dir, max_bytes, aux_reserve_bytes,
                                        max_records, cfg, shutdown_deadline, max_video_bytes=max_video_bytes))
        if not completed:
            raise SystemExit(1)
        return True

    def status(
        self,
        spool_dir: str = "",
        max_bytes: int = 0,
        aux_reserve_bytes: int = 0,
        max_records: int = 0,
        max_video_bytes: Optional[int] = None,
        retention_seconds: Optional[float] = None,
    ) -> str:
        """Print durable backlog and delivery status as JSON."""
        limit = _video_limit(max_video_bytes)
        retention = _retention_value(retention_seconds)
        transport = VmodalTransport(None)
        try:
            qualification = transport.validate_capabilities(limit)
        except TransportBlocked as exc:
            qualification = {"qualified": False, "reason": str(exc),
                             "max_video_bytes": transport.capabilities.max_video_bytes}
        spool = _spool(spool_dir, max_bytes, aux_reserve_bytes, max_records, limit)
        try:
            values = spool.status(retention)
            values.update(max_bytes=spool.cfg.max_bytes, aux_reserve_bytes=spool.cfg.aux_reserve_bytes,
                          max_records=spool.cfg.max_records, retention_seconds=retention,
                          transport_qualification=qualification)
            return json.dumps(values, sort_keys=True)
        finally:
            spool.close()

    def flush(
        self,
        spool_dir: str = "",
        deadline_seconds: float = 60.0,
        max_bytes: int = 0,
        aux_reserve_bytes: int = 0,
        max_records: int = 0,
        max_video_bytes: Optional[int] = None,
        retention_seconds: Optional[float] = None,
    ) -> bool:
        """Stop admission and deliver the existing durable backlog."""
        completed = asyncio.run(_session(".", spool_dir, max_bytes, aux_reserve_bytes,
                                        max_records, RunnerConfig(retention_seconds=_retention_value(retention_seconds)),
                                        deadline_seconds, True, max_video_bytes))
        if not completed:
            raise SystemExit(1)
        return True


def main():
    fire.Fire(RobotCli())


if __name__ == "__main__":
    main()
