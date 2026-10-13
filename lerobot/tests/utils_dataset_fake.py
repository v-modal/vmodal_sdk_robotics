"""Independent synthetic researcher archive and normalized native scanner data.

# Usage:
    from tests.utils_dataset_fake import pd_fake_dataset, FakeArchive
    fixture = pd_fake_dataset("researcher-recovery")
"""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass, asdict
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning
from src.utils.util_base import os_makedirs, os_path_cleanup

import io
import json
import copy
from contextlib import contextmanager
from vmodal_robot.adapters.lerobot import LeRobotAdapter
from vmodal_robot.contracts import Artifact, DatasetRevision, Receipt
from vmodal_robot.utils import os_read_bytes, os_write_bytes, str_json_canonical, str_sha256_bytes, str_stable_id
from vmodal_robot.reconciliation.dataset import dataset_descriptor
from tests.utils_datafake import pd_fake_data

dirtest="ztmp/ztests"


@contextmanager
def pd_rejected(message):
    """Assert an intended fail-closed contract error; unrelated errors propagate."""
    try:
        yield
    except ValueError as exc:
        assert message in str(exc), str(exc)
    else:
        assert False, f"required rejection was absent: {message}"


def pd_fake_dataset(name="researcher"):
    """Fake MP4/Parquet bytes; native-reader/cloud qualification is deliberately false."""
    data = pd_fake_data(name)
    camera = "observation.images.front"
    wrist = "observation.images.wrist"
    state = dict(dtype="float32", shape=[2], names=["joint0", "joint1"], units=["rad", "rad"], semantics="measured joint angles")
    action = dict(dtype="float32", shape=[2], names=["joint0", "joint1"], units=["rad", "rad"], semantics="commanded absolute joint targets; active row interval")
    features = {"observation.state": state, "action": action,
                camera: dict(dtype="video", shape=[3, 2, 2], names=["channels", "height", "width"]),
                wrist: dict(dtype="video", shape=[3, 2, 2], names=["channels", "height", "width"])}
    info = dict(codebase_version="v3.0", fps=10, features=features, total_frames=4, total_episodes=2)
    value = str_json_canonical(info)
    os_write_bytes(os.path.join(data["dataset"], "meta/info.json"), value)
    for entry in data["data"]["artifacts"]:
        if entry["path"] == "meta/info.json":
            entry.update(size_bytes=len(value), sha256=str_sha256_bytes(value))
    for ep in (0, 1):
        path = f"audio/episode-{ep}.wav"
        value = f"fake-original-audio-{ep}".encode()
        os_write_bytes(os.path.join(data["dataset"], path), value)
        data["data"]["artifacts"].append(dict(path=path, kind="opaque", content_type="audio/wav", size_bytes=len(value), sha256=str_sha256_bytes(value), source_refs={"episode": ep}))
    os_write_bytes(data["manifest"], str_json_canonical(data["data"]))
    item = LeRobotAdapter(data["ready"]).os_load_manifest(data["manifest"])
    entries = {a.rel_path: a for a in item.artifacts}
    shard = "data/chunk-000/file-000.parquet"
    front_path = "videos/chunk-000/cam-front.mp4"
    wrist_path = "videos/chunk-000/cam-wrist.mp4"
    rows = []
    episodes = []
    sources = []
    for ep in (0, 1):
        origin = ep * 2_000_000_000
        bounds = {camera: dict(path=front_path, start_pts=ep * 200, end_pts=(ep + 1) * 200, time_base_num=1, time_base_den=1000),
                  wrist: dict(path=wrist_path, start_pts=ep * 200, end_pts=(ep + 1) * 200, time_base_num=1, time_base_den=1000)}
        episodes.append(dict(episode_index=ep, length=2, row_start=ep * 2, row_end=(ep + 1) * 2,
                             start_index=10 if ep == 0 else 100, data_path=shard, video_bounds=bounds,
                             reference_clock="session", reference_epoch="capture-epoch", reference_start_ns=origin))
        for frame in (0, 1):
            target = origin + frame * 100_000_000
            ordinal = ep * 2 + frame
            rows.append(dict(episode_index=ep, frame_index=frame, index=(10 if ep == 0 else 100) + frame,
                             timestamp_ns=frame * 100_000_000, task_index=0, data_path=shard, row_ordinal=ordinal,
                             features={"observation.state": [float(frame), 2.0], "action": [0.5, 1.0],
                                       camera: {"path": front_path, "pts": ep * 200 + frame * 100},
                                       wrist: {"path": wrist_path, "pts": ep * 200 + frame * 100}}))
            base = dict(revision=item.source_revision, episode=str(ep), clock="device", epoch="device-epoch", source_ns=target, ordinal=ordinal)
            for key, path in ((camera, front_path), (wrist, wrist_path)):
                sources.append(dict(base, source_id=f"{key}-{ep}-{frame}", modality=key, kind="video", path=path,
                                    checksum=entries[path].checksum, pts=ep * 200 + frame * 100, time_base_num=1, time_base_den=1000, track="video:0"))
            sources.append(dict(base, source_id=f"state-{ep}-{frame}", modality="observation.state", kind="telemetry", path=shard, checksum=entries[shard].checksum))
            sources.append(dict(base, source_id=f"action-{ep}-{frame}", modality="action", kind="action", path=shard, checksum=entries[shard].checksum,
                                apply_start_ns=target, apply_end_ns=target, effective_end_ns=target + 100_000_000, representation="absolute-joint-target-rad"))
        audio_path = f"audio/episode-{ep}.wav"
        sources.append(dict(source_id=f"audio-{ep}", revision=item.source_revision, episode=str(ep), clock="device", epoch="device-epoch",
                            source_ns=origin - 20_000_000, modality="audio", kind="audio", path=audio_path, checksum=entries[audio_path].checksum,
                            sample_rate=48000, sample_count=6720, origin_known=True, trim_known=True, decoder="synthetic-decoder-v1", track="audio:0"))
    native = dict(schema_version=1, dataset_id="synthetic-dataset", source_revision=item.source_revision, source_kind="synthetic", producer_version="synthetic-v3-writer", scanner_version="synthetic-scanner-v1",
                  feature_profile_id="synthetic-2joint-rgb-v1", info_path="meta/info.json", metadata_paths=["meta/episodes.jsonl"],
                  features=features, fps=10, timestamp_tolerance_ns=0, task_indices=[0], episodes=episodes, rows=rows)
    modalities = [camera, wrist, "observation.state", "action", "audio"]
    profile = dict(profile_id="synthetic-profile", version="1", required_modalities=modalities,
                   budgets_ns={key: 1_000_000 for key in modalities}, policies={camera: "nearest", wrist: "nearest", "observation.state": "nearest", "action": "active", "audio": "causal"},
                   max_age_ns={key: 2_000_000 for key in modalities}, audio_window_ns={"audio": 20_000_000},
                   pairwise_budgets_ns={f"{camera}|observation.state": 1_000_000})
    clocks = [dict(transform_id="synthetic-qualified-map-v1", source_clock="device", source_epoch="device-epoch",
                   reference_clock="session", reference_epoch="capture-epoch", source_anchor_ns=0, ref_anchor_ns=0,
                   valid_start_ns=-1_000_000_000, valid_end_ns=3_000_000_000, evidence="synthetic-independent-timing-ground-truth")]
    bundles = []
    for row in rows:
        ep, frame = row["episode_index"], row["frame_index"]
        identity = dict(dataset_id="synthetic-dataset", source_revision=item.source_revision, episode_index=ep, frame_index=frame,
                        timestamp_ns=row["timestamp_ns"], data_path=shard, row_ordinal=row["row_ordinal"])
        request = dict(sample_id=json.dumps(["synthetic-dataset", item.source_revision, ep, frame], separators=(",", ":")),
                       revision=item.source_revision, episode=str(ep), reference_clock="session", reference_epoch="capture-epoch",
                       target_ns=episodes[ep]["reference_start_ns"] + row["timestamp_ns"])
        bundles.append(dict(native_sample=identity, request=request, profile=copy.deepcopy(profile), clocks=copy.deepcopy(clocks), sources=copy.deepcopy(sources)))
    paths = {key: f"sidecars/revision-001/{key}.json" for key in ("native", "temporal", "alignment", "quality")}
    return dict(data, item=item, native=native, bundles=bundles, paths=paths)


class FakeNativeRows(list):
    """Bound full-table scans so a scaling regression fails without a long wait."""
    scans = 0

    def __iter__(self):
        self.scans += 1
        assert self.scans <= 2, "native validation repeatedly scans the complete row table"
        return super().__iter__()


def pd_fake_native_scale(native, count=5000):
    """Build independent episode rows sharing media shards for scaling checks."""
    value = copy.deepcopy(native)
    value["episodes"], value["rows"] = [], []
    for ep in range(count):
        episode = copy.deepcopy(native["episodes"][0])
        episode.update(episode_index=ep, row_start=ep*2, row_end=ep*2+2,
                       start_index=ep*2, reference_start_ns=ep*200_000_000)
        for bound in episode["video_bounds"].values():
            bound.update(start_pts=ep*200, end_pts=ep*200+200)
        value["episodes"].append(episode)
        for frame, template in enumerate(native["rows"][:2]):
            row = copy.deepcopy(template)
            row.update(episode_index=ep, index=ep*2+frame, row_ordinal=ep*2+frame)
            for key, feature in value["features"].items():
                if feature["dtype"] == "video":
                    row["features"][key]["pts"] = ep*200+frame*100
            value["rows"].append(row)
    # Deliberately unsorted input verifies grouping does not change ownership.
    value["episodes"].reverse()
    value["rows"].reverse()
    value["rows"] = FakeNativeRows(value["rows"])
    info = dict(fps=value["fps"], features=value["features"], total_frames=count*2, total_episodes=count)
    return value, info


class FakeArchive:
    """A separate byte store with no producer path, spool or transport fallback."""
    def __init__(self, payloads):
        self.payloads = dict(payloads)
        self.reads = []

    def open_artifact(self, remote_ref):
        self.reads.append(remote_ref)
        return io.BytesIO(self.payloads[remote_ref])


def pd_fake_archive(prepared, handoff):
    """Allocate synthetic acknowledged identities then freeze independent bytes."""
    artifacts = []
    receipts = {}
    payloads = {}
    for item in prepared.artifacts:
        identity = str_stable_id(item.rel_path, item.checksum)
        artifact = Artifact(1, identity, "synthetic-dataset", prepared.source_id, item.kind, item.rel_path,
                            item.content_type, item.byte_length, item.checksum, item.source_path, source_refs=item.source_refs)
        artifacts.append(artifact)
        remote = f"mock://artifacts/{identity}"
        receipts[identity] = Receipt(identity, remote, "ACKNOWLEDGED", item.checksum, checksum_verified=True)
        payloads[remote] = os_read_bytes(item.source_path)
    revision = DatasetRevision(1, str_stable_id(prepared.source_revision), "synthetic-dataset", prepared.source_id,
                               prepared.source_format, prepared.source_version, prepared.destination, prepared.source_revision,
                               prepared.complete, tuple(a.artifact_id for a in artifacts))
    descriptor = dataset_descriptor(handoff, revision, artifacts, receipts)
    value = str_json_canonical(descriptor)
    ref = f"mock://descriptors/{revision.revision_id}"
    payloads[ref] = value
    return dict(reader=FakeArchive(payloads), descriptor_ref=ref, digest=str_sha256_bytes(value),
                descriptor=descriptor, revision=revision, artifacts=artifacts, receipts=receipts)
