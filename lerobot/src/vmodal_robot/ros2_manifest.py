"""ROS 2 closed-bag notifications to durable ready manifests.

Publish absolute bag paths only after the recorder exits successfully. Notifications
are hints; a durable completion journal in closed_dir survives lost notifications.
"""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from vmodal_robot._support.util_log import log_info, log_error, log_trace, log_warning

from concurrent.futures import ThreadPoolExecutor
from .utils import os_bag_manifest, os_bag_closed, os_read_bytes, os_write_bytes


class ClosedBags:
    def __init__(self, closed_dir: str, ready_dir: str, source_id: str,
                 dataset_key: str, destination: str, fleet_refs: bool = False):
        self.closed_dir, self.ready_dir = closed_dir, ready_dir
        self.source_id, self.dataset_key, self.destination = source_id, dataset_key, destination
        self.fleet_refs = fleet_refs

    def notify(self, bag_dir: str):
        os_bag_closed(bag_dir, self.closed_dir)

    def scan(self):
        if not os.path.isdir(self.closed_dir):
            return
        for name in sorted(os.listdir(self.closed_dir)):
            if not name.endswith(".closed"):
                continue
            path = os.path.join(self.closed_dir, name)
            if os.path.isfile(path + ".done"):
                continue
            bag = ""
            try:
                bag = os_read_bytes(path).decode()
                target = os_bag_manifest(bag, self.ready_dir, self.source_id, self.dataset_key,
                                         self.destination, self.fleet_refs)
                os_write_bytes(path + ".done", target.encode(), immutable=True)
                log_info("closed bag ready", bag=bag, manifest=target)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log_error("closed bag handoff deferred", bag=bag, reason=str(exc))


def run(closed_dir: str, ready_dir: str, source_id: str, dataset_key: str,
        destination: str, topic: str = "/vmodal/closed_bag", fleet_refs: bool = False):
    """Spin a dedicated ROS node; hashing and journal writes run off callbacks."""
    import rclpy
    from rclpy.node import Node
    from rclpy.executors import ExternalShutdownException
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
    from std_msgs.msg import String
    handoff = ClosedBags(closed_dir, ready_dir, source_id, dataset_key, destination, fleet_refs)
    pool = ThreadPoolExecutor(max_workers=1)
    rclpy.init(args=[])
    node = Node("vmodal_closed_bag_manifest")
    pending = None

    def notify(msg):
        def write():
            try:
                handoff.notify(msg.data)
            except (OSError, ValueError) as exc:
                log_error("closed bag notification rejected", reason=str(exc))
        pool.submit(write)

    def scan():
        nonlocal pending
        if pending is None or pending.done():
            if pending is not None:
                pending.result()
            pending = pool.submit(handoff.scan)

    qos = QoSProfile(depth=100, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.TRANSIENT_LOCAL)
    node.create_subscription(String, topic, notify, qos)
    node.create_timer(1.0, scan)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
        pool.shutdown(wait=True)


def main():
    fire.Fire({"run": run, "notify": os_bag_closed}, name="vmodal-ros2-manifest")


if __name__ == "__main__":
    main()
