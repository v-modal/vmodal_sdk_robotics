from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

import asyncio
import json
import signal

from .adapters.lerobot import LeRobotAdapter
from .runner import Runner, RunnerConfig
from .spool import Spool, SpoolConfig
from .transports.vmodal import VmodalTransport


def os_path_value(value: str, env_name: str, default: str) -> str:
    return os.path.abspath(os.path.expanduser(value or os.environ.get(env_name, default)))


def _int_value(value: int, env_name: str, default: int) -> int:
    return int(value) if int(value) > 0 else int(os.environ.get(env_name, default))


def _spool(spool_dir: str, max_bytes: int, aux_reserve_bytes: int, max_records: int) -> Spool:
    root = os_path_value(spool_dir, "VMODAL_ROBOT_SPOOL_DIR", "./vmodal_robot_spool")
    return Spool(
        SpoolConfig(
            root=root,
            max_bytes=_int_value(max_bytes, "VMODAL_ROBOT_MAX_BYTES", 10 * 1024 * 1024 * 1024),
            aux_reserve_bytes=_int_value(
                aux_reserve_bytes, "VMODAL_ROBOT_AUX_RESERVE_BYTES", 512 * 1024 * 1024
            ),
            max_records=_int_value(max_records, "VMODAL_ROBOT_MAX_RECORDS", 10000),
        )
    )


async def _run(runner: Runner, shutdown_deadline: float):
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, runner.stop)
        except NotImplementedError:
            pass
    await runner.run()
    await runner.flush(shutdown_deadline)


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
    ):
        """Continuously accept ready manifests and deliver their artifacts."""
        ready = os_path_value(ready_dir, "VMODAL_ROBOT_READY_DIR", "./ready")
        spool = _spool(spool_dir, max_bytes, aux_reserve_bytes, max_records)
        transport = VmodalTransport.from_env()
        runner = Runner(
            LeRobotAdapter(ready),
            spool,
            transport,
            RunnerConfig(poll_seconds=poll_seconds, request_timeout_seconds=request_timeout_seconds),
        )
        try:
            asyncio.run(_run(runner, shutdown_deadline))
        finally:
            asyncio.run(transport.close())
            spool.close()

    def status(
        self,
        spool_dir: str = "",
        max_bytes: int = 0,
        aux_reserve_bytes: int = 0,
        max_records: int = 0,
    ) -> str:
        """Print durable backlog and delivery status as JSON."""
        spool = _spool(spool_dir, max_bytes, aux_reserve_bytes, max_records)
        try:
            return json.dumps(spool.status(), sort_keys=True)
        finally:
            spool.close()

    def flush(
        self,
        spool_dir: str = "",
        deadline_seconds: float = 60.0,
        max_bytes: int = 0,
        aux_reserve_bytes: int = 0,
        max_records: int = 0,
    ) -> bool:
        """Stop admission and deliver the existing durable backlog."""
        spool = _spool(spool_dir, max_bytes, aux_reserve_bytes, max_records)
        transport = VmodalTransport.from_env()
        runner = Runner(LeRobotAdapter("."), spool, transport)
        try:
            return asyncio.run(runner.flush(deadline_seconds))
        finally:
            asyncio.run(transport.close())
            spool.close()


def main():
    fire.Fire(RobotCli())


if __name__ == "__main__":
    main()
