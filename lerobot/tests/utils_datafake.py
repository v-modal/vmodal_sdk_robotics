"""Shared synthetic spool, timing and researcher test-data factories.

# Usage:
  - from tests.utils_datafake import pd_fake_data, pd_fake_temporal, pd_fake_dataset
"""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning
from src.utils.util_base import os_makedirs, os_path_cleanup

import asyncio
import hashlib
import json
import textwrap

from vmodal_robot.contracts import Artifact, DatasetRevision, Receipt, RevisionInput, TransportRetry


dirtest="ztmp/ztests"


def pd_fake_temporal():
    from tests.utils_temporal_fake import pd_fake_temporal as create
    return create()


def pd_fake_dataset(name="researcher"):
    from tests.utils_dataset_fake import pd_fake_dataset as create
    return create(name)


def os_fake_no_vmodal(root: str) -> str:
    """Block optional SDK imports in this process and every Python subprocess."""
    code = textwrap.dedent('''\
        """Reject optional SDK imports during standalone acceptance tests."""
        import importlib.abc
        import sys

        class NoSdk(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "vmodal" or fullname.startswith("vmodal."):
                    raise ModuleNotFoundError("optional vmodal SDK import blocked by standalone test")

        sys.meta_path.insert(0, NoSdk())
    ''')
    os_save_bytes(os.path.join(root, "sitecustomize.py"), code.encode())
    return root


def pd_fake_search() -> Dict[str, Any]:
    """Synthetic policy evidence only; never a production calibration profile."""
    scope = dict(collection="synthetic-owner/vid_file/test/front", model="synthetic-model",
                 metric="synthetic-distance", direction="lower_is_closer",
                 preprocessing="synthetic-v1", camera_domain="synthetic-front",
                 index_version="synthetic-v1", sampling_policy="synthetic-v1",
                 query_kind="text", search_combine_mode="single-source")
    profile = dict(schema_version=1, qualified=True, calibration_version="synthetic-v1",
                   calibration_record="synthetic-held-out-record", consumer_approved=True,
                   false_accept_budget=0.01, scope=scope.copy(), distance_field="score",
                   max_distance=0.4, freshness_seconds=5.0, max_clock_uncertainty_seconds=0.2)
    evidence = dict(distance_field="score", raw_distance_qualified=True,
                    wire_record="synthetic-hit-record", clock_mapping_qualified=True,
                    clock_mapping_record="synthetic-episode-anchor", capture_wall_time=998.0,
                    clock_uncertainty_seconds=0.1)
    return dict(hit={"score": 0.3, "score_ui": 1.0, "item_id": "synthetic-hit"},
                scope=scope, profile=profile, evidence=evidence, now_wall=1000.0)


def os_save_bytes(path: str, value: bytes):
    os_makedirs(os.path.dirname(path))
    with open(path, "wb") as handle:
        handle.write(value)


def str_sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def pd_fake_data(name: str = "fixture", version: str = "v3") -> Dict[str, Any]:
    root = os.path.abspath(os.path.join(dirtest, name))
    os_path_cleanup(root)
    ready = os_makedirs(os.path.join(root, "ready"))
    dataset = os_makedirs(os.path.join(root, "dataset"))
    values = {
        "videos/chunk-000/cam-front.mp4": b"fake-mp4-front\x00\x01",
        "videos/chunk-000/cam-wrist.mp4": b"fake-mp4-wrist\x02\x03",
        "data/chunk-000/file-000.parquet": b"PAR1fake-telemetry-two-episodesPAR1",
        "meta/info.json": json.dumps({"codebase_version": "v3.0", "features": {"action": {"dtype": "float32"}}}, sort_keys=True).encode(),
        "meta/episodes.jsonl": b'{"episode_index":0,"data_from":0,"data_to":2}\n{"episode_index":1,"data_from":2,"data_to":4}\n',
    }
    for rel_path, value in values.items():
        os_save_bytes(os.path.join(dataset, rel_path), value)
    artifacts = []
    for rel_path, value in values.items():
        if rel_path.endswith(".mp4"):
            kind, content_type = "video", "video/mp4"
            refs = {"camera_key": os.path.basename(rel_path)[:-4], "episodes": [0, 1], "video_offsets": [0.0, 2.0]}
        elif rel_path.endswith(".parquet"):
            kind, content_type = "telemetry", "application/vnd.apache.parquet"
            refs = {"episodes": [0, 1], "row_ranges": [[0, 2], [2, 4]]}
        else:
            kind, content_type = "metadata", "application/json"
            refs = {"snapshot": "revision-001"}
        artifacts.append(
            {
                "path": rel_path,
                "kind": kind,
                "content_type": content_type,
                "size_bytes": len(value),
                "sha256": str_sha(value),
                "source_refs": refs,
                "timing": {"source_clock": "lerobot.timestamp", "start": 0.0, "end": 4.0},
            }
        )
    manifest = {
        "contract_version": 1,
        "source_format": "lerobot",
        "source_version": version,
        "source_id": "robot-01",
        "dataset_key": "factory/pick-place",
        "dataset_root": "../dataset",
        "destination": "robot-data/robot-01",
        "source_revision": "revision-001",
        "complete": True,
        "artifacts": artifacts,
    }
    manifest_path = os.path.join(ready, "revision-001.ready.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, sort_keys=True)
    return {"root": root, "ready": ready, "dataset": dataset, "manifest": manifest_path, "data": manifest}


class FakeTransport:
    def __init__(self):
        self.remote: Dict[str, Receipt] = {}
        self.payloads: Dict[str, bytes] = {}
        self.revisions: Dict[str, Receipt] = {}
        self.artifact_effects = 0
        self.revision_effects = 0
        self.fail_once = set()
        self.lost_once = set()
        self.video_gate: Optional[asyncio.Event] = None
        self.video_started: Optional[asyncio.Event] = None

    async def deliver(self, artifact: Artifact, destination: str) -> Receipt:
        if artifact.kind == "video" and self.video_gate is not None:
            if self.video_started is not None:
                self.video_started.set()
            await self.video_gate.wait()
        if artifact.artifact_id in self.fail_once:
            self.fail_once.remove(artifact.artifact_id)
            raise TransportRetry("temporary fake outage")
        receipt = self.remote.get(artifact.artifact_id)
        if receipt is None:
            self.artifact_effects += 1
            with open(artifact.local_ref, "rb") as handle:
                value = handle.read()
            assert hashlib.sha256(value).hexdigest() == artifact.checksum
            self.payloads[artifact.artifact_id] = value
            receipt = Receipt(artifact.artifact_id, f"fake://{destination}/{artifact.artifact_id}", "ACKNOWLEDGED", artifact.checksum)
            self.remote[artifact.artifact_id] = receipt
        if artifact.artifact_id in self.lost_once:
            self.lost_once.remove(artifact.artifact_id)
            raise TransportRetry("fake response lost after durable acceptance")
        return receipt

    async def reconcile(self, artifact: Artifact, destination: str) -> Optional[Receipt]:
        return self.remote.get(artifact.artifact_id)

    async def publish_revision(self, revision: DatasetRevision) -> Receipt:
        receipt = self.revisions.get(revision.revision_id)
        if receipt is None:
            self.revision_effects += 1
            receipt = Receipt(revision.revision_id, f"fake://revision/{revision.revision_id}", "ACKNOWLEDGED")
            self.revisions[revision.revision_id] = receipt
        return receipt

    async def reconcile_revision(self, revision: DatasetRevision) -> Optional[Receipt]:
        return self.revisions.get(revision.revision_id)


def pd_fake_latency():
    """One monotonic clock with an outage, delayed indexing and visibility."""
    events = dict(capture=0.0, finalized=2.0, manifest_ready=3.0, accepted=5.0,
                  first_attempt=100.0, upload_start=102.0, durable_ack=104.0,
                  last_artifact_ack=105.0, revision_published=107.0,
                  index_submit=108.0, index_ready=115.0,
                  first_verified_retrieval=120.0, query_start=119.0,
                  decision=121.0, accepted_decision=121.0)
    return dict(run_id="synthetic-001", clock_domain="fake-monotonic-seconds", events=events,
                correlation=dict(artifact_id="fake-artifact", revision_id="fake-revision", event_id="fake-event"),
                status="success", durable_ack_verified=True, revision_published_verified=True,
                index_ready_verified=True, search_verified=True, accepted_verified=True,
                capture_wall_time=1000.0, decision_wall_time=1121.0,
                clock_mapping_qualified=True, clock_mapping_record="synthetic-episode-anchor",
                clock_uncertainty_seconds=.1, bytes=1000, retries=2)
