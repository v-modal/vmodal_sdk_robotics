"""Record real ROS traffic, close the recorder, notify the node, admit the bag."""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning
from src.utils.util_base import os_makedirs, os_path_cleanup

import signal
import subprocess
import time
from vmodal_robot.adapters.rosbag import RosbagAdapter
from vmodal_robot.spool import Spool, SpoolConfig
from vmodal_robot.ros2_manifest import ClosedBags
from tests.utils_datafake import pd_fake_bag

dirtest="ztmp/ztests"


def test1():
    root = os.path.abspath(os.path.join(dirtest, "ros2_live"))
    os_path_cleanup(root)
    os_makedirs(root)
    bag, ready, closed = [os.path.join(root, name) for name in ("bag", "ready", "closed")]
    recorder = subprocess.Popen(["ros2", "bag", "record", "-o", bag, "/chatter"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    node = None
    try:
        time.sleep(5)
        assert recorder.poll() is None
        recorder.send_signal(signal.SIGINT)
        output, _ = recorder.communicate(timeout=20)
        assert recorder.returncode == 0, output
        assert os.path.isfile(os.path.join(bag, "metadata.yaml")), output
        node = subprocess.Popen([sys.executable, "-m", "vmodal_robot.ros2_manifest", "run",
                "--closed_dir=" + closed, "--ready_dir=" + ready, "--source_id=ci-robot",
                "--dataset_key=ros-ci", "--destination=ros-ci"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        subprocess.run(["ros2", "topic", "pub", "--once", "--qos-durability", "transient_local",
                        "/vmodal/closed_bag", "std_msgs/msg/String", "{data: '" + bag + "'}"],
                       check=True, timeout=30)
        end = time.monotonic() + 20
        while time.monotonic() < end and not os.path.isdir(ready):
            assert node.poll() is None
            time.sleep(.1)
        items = RosbagAdapter(ready).discover()
        assert len(items) == 1
        spool = Spool(SpoolConfig(os.path.join(root, "spool")))
        try:
            revision = spool.accept(items[0])
            assert revision.source_format == "rosbag2"
            assert len(revision.artifact_ids) >= 2
        finally:
            spool.close()
    finally:
        for proc in (recorder, node):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                proc.communicate(timeout=20)
    log_info("real ROS closed-bag manifest and admission passed")


def test2():
    root = os.path.abspath(os.path.join(dirtest, "ros2_wrapper"))
    os_path_cleanup(root)
    os_makedirs(root)
    bag, ready, closed = [os.path.join(root, name) for name in ("bag", "ready", "closed")]
    proc = subprocess.Popen(["bash", "examples/record_bag.sh", "record", bag, closed, "/chatter"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(5)
        assert proc.poll() is None
        proc.send_signal(signal.SIGTERM)
        output, _ = proc.communicate(timeout=20)
        assert proc.returncode == 0, output
        assert os.path.isdir(closed), output
        ClosedBags(closed, ready, "robot", "wrapper", "ros-ci").scan()
        assert len(RosbagAdapter(ready).discover()) == 1
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=20)
    log_info("recorder SIGTERM wrapper and completion journal passed")


def test_all():
    test1()
    test2()


if __name__ == "__main__":
    fire.Fire({"test_all": test_all})
