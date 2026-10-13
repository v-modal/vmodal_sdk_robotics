"""Native rosbag2 handoff uses the existing immutable artifact contract."""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from vmodal_robot._support.util_log import log_info, log_error, log_trace, log_warning

from .lerobot import LeRobotAdapter
from ..contracts import ContractError


class RosbagAdapter(LeRobotAdapter):
    source_format = "rosbag2"
    source_version = "1"

    def os_load_manifest(self, manifest_path: str):
        item = super().os_load_manifest(manifest_path)
        if not item.complete or not any(a.rel_path == "metadata.yaml" and a.kind == "metadata" for a in item.artifacts):
            raise ContractError("closed rosbag requires complete metadata.yaml")
        if not any(a.kind == "opaque" for a in item.artifacts):
            raise ContractError("closed rosbag requires native storage files")
        if any(a.kind not in ("opaque", "metadata") for a in item.artifacts):
            raise ContractError("rosbag artifacts must be native opaque storage or metadata")
        return item
