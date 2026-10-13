"""Durable source events; offline attachment uses the shared temporal selector.

# Usage:
  - vmodal-robot mark "grasp slipped" --tags=failure --path=./markers.jsonl
  - from vmodal_robot import mark
  - mark("e-stop", ("failure",), path="./markers.jsonl", episode_index=0)

Each call fsyncs one bounded JSONL record under a local file lock. Disk errors
propagate. This is synchronous local I/O with no real-time latency guarantee;
recorders with strict deadlines must own a bounded durable event writer. Clock
defaults to monotonic time without a UTC claim. ROS callers provide header time,
clock and epoch explicitly; no ROS runtime dependency is introduced.
"""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass, asdict
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

import json
import time
import uuid
from fractions import Fraction
from math import ceil
from .utils import os_marker_append, os_marker_rotate, os_read_bytes


@dataclass(frozen=True)
class Marker:
    marker_id: str
    label: str
    tags: Tuple[str, ...]
    source_ns: int
    clock: str
    epoch: str
    origin: str = "code"
    episode_index: Optional[int] = None
    uncertainty_ns: int = 0
    timestamp_meaning: str = "event_recorded"

    def __post_init__(self):
        if not all(isinstance(x, str) and x.strip() for x in
                   (self.marker_id, self.label, self.clock, self.epoch, self.timestamp_meaning)):
            raise ValueError("marker identity, label, clock, epoch and timestamp meaning are required")
        if self.origin not in ("operator", "ros", "code"):
            raise ValueError("marker origin must be operator, ros or code")
        if type(self.source_ns) is not int or type(self.uncertainty_ns) is not int or self.uncertainty_ns < 0:
            raise ValueError("marker time must be integer ns with nonnegative uncertainty")
        if self.episode_index is not None and (type(self.episode_index) is not int or self.episode_index < 0):
            raise ValueError("episode_index must be a nonnegative integer")
        if not isinstance(self.tags, tuple) or any(not isinstance(x, str) or not x for x in self.tags):
            raise ValueError("marker tags must be a tuple of nonempty strings")


def mark(label: str, tags: Union[str, Tuple[str, ...]] = (), path: str = "./markers.jsonl",
         source_ns: Optional[int] = None, clock: str = "monotonic", epoch: str = "",
         origin: str = "code", episode_index: Optional[int] = None,
         uncertainty_ns: int = 0, marker_id: str = "",
         timestamp_meaning: str = "event_recorded") -> Marker:
    """Persist one event. Explicit device times require their explicit reset epoch."""
    if source_ns is None:
        if clock != "monotonic":
            raise ValueError("explicit source_ns required for a non-monotonic source clock")
        source_ns = time.monotonic_ns()
        # Process identity prevents joining rebooted/restarted recorder epochs.
        epoch = epoch or f"process-{os.getpid()}-{_PROCESS_EPOCH}"
    elif not epoch:
        raise ValueError("explicit source time requires a clock epoch")
    tags = tuple(x.strip() for x in tags.split(",") if x.strip()) if isinstance(tags, str) else tuple(tags)
    marker = Marker(marker_id or str(uuid.uuid4()), label, tags, source_ns, clock, epoch,
                    origin, episode_index, uncertainty_ns, timestamp_meaning)
    os_marker_append(path, dict(contract_version=1, **asdict(marker)))
    return marker


_PROCESS_EPOCH = uuid.uuid4().hex


def marker_rotate(path: str, target: str):
    """Rotate before hashing/handoff; subsequent events create a new active file."""
    os_marker_rotate(path, target)
    return target


def marker_load(path: str) -> Tuple[Marker, ...]:
    """Offline replay deduplicates identical IDs and rejects conflicting events."""
    found = {}
    for line in os_read_bytes(path).splitlines():
        record = json.loads(line)
        if record.pop("contract_version", None) != 1:
            raise ValueError("unsupported marker contract_version")
        record["tags"] = tuple(record["tags"])
        marker = Marker(**record)
        if marker.marker_id in found and found[marker.marker_id] != marker:
            raise ValueError(f"conflicting marker identity: {marker.marker_id}")
        found[marker.marker_id] = marker
    return tuple(found[key] for key in sorted(found))


def marker_attach(marker: Marker, revision: str, frames, clocks, profile,
                  reference_clock: str, reference_epoch: str) -> Dict[str, Any]:
    """Offline bounded attachment; keep original events even when no frame qualifies."""
    from .reconciliation.temporal import TemporalRequest, temporal_map, temporal_video
    result = dict(marker=asdict(marker), status="unattached", reason="", selection=None)
    if marker.episode_index is None:
        return dict(result, reason="missing_episode")
    matches = [clock for clock in clocks if clock.source_clock == marker.clock and
               clock.source_epoch == marker.epoch and clock.reference_clock == reference_clock and
               clock.reference_epoch == reference_epoch and
               clock.valid_start_ns <= marker.source_ns <= clock.valid_end_ns]
    if len(matches) != 1:
        return dict(result, reason="unknown_clock_relation" if not matches else "ambiguous_clock_relation")
    clock = matches[0]
    mapped = temporal_map(clock, marker.source_ns, marker.epoch)
    if mapped.status != "valid":
        return dict(result, reason=mapped.reason)
    # The request boundary is integer ns: nearest-even rounding is accounted for.
    target = round(mapped.time_ns)
    uncertainty = ceil(mapped.uncertainty_ns +
                       marker.uncertainty_ns * Fraction(clock.scale_num, clock.scale_den) +
                       abs(mapped.time_ns - target))
    request = TemporalRequest(f"marker:{marker.marker_id}", revision, str(marker.episode_index),
                              reference_clock, reference_epoch, target, uncertainty)
    selection = temporal_video(request, frames, clocks, profile)
    return dict(result, status="attached" if selection.status == "valid" else "unattached",
                reason=selection.reason, selection=selection)
