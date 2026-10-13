"""Native bag manifest admission, immutable replay and rejection."""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning
from src.utils.util_base import os_makedirs, os_path_cleanup

from vmodal_robot.adapters.rosbag import RosbagAdapter
from vmodal_robot.utils import os_bag_manifest, str_sha256_file
from vmodal_robot.spool import Spool, SpoolConfig
from tests.utils_datafake import pd_fake_bag, os_save_bytes
from tests.utils_temporal_fake import ContractRaises

dirtest="ztmp/ztests"


def test1():
    data = pd_fake_bag("bag_manifest")
    target = os_bag_manifest(data["bag"], data["ready"], "robot", "bags", "robot-data")
    assert target == os_bag_manifest(data["bag"], data["ready"], "robot", "bags", "robot-data")
    item = RosbagAdapter(data["ready"]).discover()[0]
    assert item.source_format == "rosbag2"
    assert len(item.artifacts) == 2
    spool = Spool(SpoolConfig(os.path.join(data["root"], "spool")))
    revision = spool.accept(item)
    assert revision.source_format == "rosbag2"
    for identity in revision.artifact_ids:
        artifact = spool.get_artifact(identity)
        assert artifact.checksum == str_sha256_file(artifact.local_ref)
    assert spool.accept(item).revision_id == revision.revision_id
    spool.close()


def test2():
    data = pd_fake_bag("bag_active")
    wal = os.path.join(data["bag"], "recording_0.db3-wal")
    os_save_bytes(wal, b"pending")
    with ContractRaises(ValueError, "still open"):
        os_bag_manifest(data["bag"], data["ready"], "robot", "bags", "robot-data")
    assert not os.path.exists(data["ready"])
    os.unlink(wal)
    os.unlink(os.path.join(data["bag"], "recording_0.db3"))
    with ContractRaises(ValueError, "regular file"):
        os_bag_manifest(data["bag"], data["ready"], "robot", "bags", "robot-data")


def test_all():
    test1()
    test2()
    log_info("test_rosbag: all passed")


if __name__ == "__main__":
    fire.Fire({"test_all": test_all})
