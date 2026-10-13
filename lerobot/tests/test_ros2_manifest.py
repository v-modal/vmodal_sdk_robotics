"""Completion journal survives restart and retries incomplete bag handoff."""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning
from src.utils.util_base import os_makedirs, os_path_cleanup

from vmodal_robot.ros2_manifest import ClosedBags
from vmodal_robot.adapters.rosbag import RosbagAdapter
from vmodal_robot.utils import os_bag_manifest, os_read_bytes
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
    items = RosbagAdapter(data["ready"]).discover()
    assert len(items) == 1
    assert items[0].source_id == "robot" and items[0].dataset_key == "bags"
    assert all("mission_id" not in a.source_refs for a in items[0].artifacts)
    target = os_bag_manifest(data["bag"], data["ready"], "robot", "bags", "robot-data")
    original = os_read_bytes(target)
    assert os_bag_manifest(data["bag"], data["ready"], "robot", "bags", "robot-data") == target
    assert os_read_bytes(target) == original
    handoff.notify(data["bag"])
    handoff.scan()
    assert len(os.listdir(data["ready"])) == 1
    assert any(name.endswith(".done") for name in os.listdir(data["closed"]))


def test2():
    data = pd_fake_bag("bag_fleet_attribution")
    handoff = ClosedBags(data["closed"], data["ready"], "robot-01", "mission-01", "fleet", fleet_refs=True)
    handoff.notify(data["bag"])
    handoff.scan()
    item = RosbagAdapter(data["ready"]).discover()[0]
    assert item.source_id == "robot-01" and item.dataset_key == "mission-01"
    assert all(a.source_refs["robot_id"] == "robot-01" and a.source_refs["mission_id"] == "mission-01"
               for a in item.artifacts)


def test_all():
    test1()
    test2()
    log_info("test_ros2_manifest: all passed")


if __name__ == "__main__":
    fire.Fire({"test_all": test_all})
