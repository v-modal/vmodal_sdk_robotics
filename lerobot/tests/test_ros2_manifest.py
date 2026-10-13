"""Completion journal survives restart and retries incomplete bag handoff."""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning
from src.utils.util_base import os_makedirs, os_path_cleanup

from vmodal_robot.ros2_manifest import ClosedBags
from vmodal_robot.adapters.rosbag import RosbagAdapter
from tests.utils_datafake import pd_fake_bag, os_save_bytes

dirtest="ztmp/ztests"


def test1():
    data = pd_fake_bag("bag_journal")
    handoff = ClosedBags(data["closed"], data["ready"], "robot", "bags", "robot-data")
    wal = os.path.join(data["bag"], "recording_0.db3-wal")
    os_save_bytes(wal, b"active")
    handoff.notify(data["bag"])
    handoff.scan()
    assert not os.path.exists(data["ready"])
    os.unlink(wal)
    # Reconstruct node state: journal discovery retries without another ROS event.
    handoff = ClosedBags(data["closed"], data["ready"], "robot", "bags", "robot-data")
    handoff.scan()
    assert len(RosbagAdapter(data["ready"]).discover()) == 1
    handoff.notify(data["bag"])
    handoff.scan()
    assert len(os.listdir(data["ready"])) == 1
    assert any(name.endswith(".done") for name in os.listdir(data["closed"]))


def test_all():
    test1()
    log_info("test_ros2_manifest: all passed")


if __name__ == "__main__":
    fire.Fire({"test_all": test_all})
