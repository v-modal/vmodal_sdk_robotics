"""Run the existing core through the worked S3/MinIO transport."""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from vmodal_robot._support.util_log import log_info, log_error, log_trace, log_warning

import asyncio
from vmodal_robot.cli import _run
from vmodal_robot.runner import Runner, RunnerConfig
from vmodal_robot.spool import Spool, SpoolConfig
from vmodal_robot.adapters.lerobot import LeRobotAdapter
from vmodal_robot.adapters.rosbag import RosbagAdapter


def run(bucket: str, ready_dir: str, spool_dir: str, endpoint: str = "",
        prefix: str = "vmodal", source_format: str = "lerobot",
        flush: bool = False, deadline_seconds: float = 30):
    """Use SDK credential discovery; bucket must already exist."""
    from aiobotocore.session import get_session
    from botocore.config import Config
    from vmodal_robot.transports.s3 import S3Transport
    if source_format not in ("lerobot", "rosbag2"):
        raise ValueError("source_format must be lerobot or rosbag2")

    async def session():
        cfg = Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 0},
                     s3={"addressing_style": "path"})
        async with get_session().create_client("s3", endpoint_url=endpoint or None,
                                              region_name="us-east-1", config=cfg) as client:
            transport = S3Transport(client, bucket, prefix)
            spool = Spool(SpoolConfig(spool_dir))
            adapter = (RosbagAdapter if source_format == "rosbag2" else LeRobotAdapter)(ready_dir)
            try:
                return await _run(Runner(adapter, spool, transport, RunnerConfig()), deadline_seconds, flush)
            finally:
                spool.close()
    if not asyncio.run(session()):
        raise SystemExit(1)


if __name__ == "__main__":
    fire.Fire({"run": run}, name="s3-upload")
