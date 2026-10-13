"""Plain-assert archive, native-row and remote-only researcher acceptance checks.

# Usage:
    python tests/test_dataset.py test_all
"""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass, replace
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning
from src.utils.util_base import os_makedirs, os_path_cleanup

import copy
import io
import time
from unittest.mock import patch
from vmodal_robot.reconciliation.dataset import (dataset_inventory, dataset_native_validate, dataset_alignment,
                                                dataset_prepare, dataset_ready, dataset_descriptor, dataset_recover)
from vmodal_robot.adapters.lerobot import LeRobotAdapter
from vmodal_robot.utils import os_read_bytes, os_write_bytes, str_sha256_bytes, str_json_canonical
from tests.utils_datafake import pd_fake_dataset
from tests.utils_dataset_fake import pd_fake_archive, pd_rejected, pd_fake_native_scale

dirtest="ztmp/ztests"


def test1():
    data = pd_fake_dataset("dataset_remote_only")
    prepared, handoff = dataset_prepare(data["dataset"], data["item"], data["native"], data["bundles"], data["paths"])
    archive = pd_fake_archive(prepared, handoff)
    originals = {a.rel_path: os_read_bytes(a.source_path) for a in prepared.artifacts}
    ready = os.path.join(data["ready"], "researcher.ready.json")
    dataset_ready(data["dataset"], prepared, ready)
    admitted = LeRobotAdapter(data["ready"]).os_load_manifest(ready)
    assert len(admitted.artifacts) == len(prepared.artifacts)
    by_path = {a.rel_path: a for a in admitted.artifacts}
    assert by_path[prepared.artifacts[0].rel_path].source_clock == prepared.artifacts[0].source_clock
    assert by_path[prepared.artifacts[0].rel_path].time_end == prepared.artifacts[0].time_end
    # Remove producer/ready paths before recovery; no spool exists in this fixture.
    os_path_cleanup(data["dataset"])
    os_path_cleanup(data["ready"])
    target = os.path.join(data["root"], "workstation-staging")
    result = dataset_recover(archive["reader"], archive["descriptor_ref"], archive["digest"], target)
    assert result["archive_valid"] is True
    assert result["alignment_valid"] is True
    assert result["training_loadable"] is False and result["search_resolvable"] is False
    assert result["native"]["samples"] == 4 and result["native"]["episodes"] == 2
    for path, expected in originals.items():
        assert os_read_bytes(os.path.join(target, path)) == expected
    aligned = result["alignment"][3]
    state = aligned["selections"]["observation.state"]["sources"][0]
    assert state["ordinal"] == 3  # dataset-wide index is 101, not physical row 3.
    audio = aligned["selections"]["audio"]
    assert audio["sample_ranges"] == [["audio-1", 4800, 5760]]
    front = aligned["selections"]["observation.images.front"]["sources"][0]
    assert front["pts"] == 300 and front["source_ns"] == 2_100_000_000


def test2():
    data = pd_fake_dataset("dataset_resume")
    prepared, handoff = dataset_prepare(data["dataset"], data["item"], data["native"], data["bundles"], data["paths"])
    archive = pd_fake_archive(prepared, handoff)
    target = os.path.join(data["root"], "workstation-staging")
    first = dataset_recover(archive["reader"], archive["descriptor_ref"], archive["digest"], target)
    archive["reader"].reads.clear()
    second = dataset_recover(archive["reader"], archive["descriptor_ref"], archive["digest"], target)
    assert first == second and archive["reader"].reads == [archive["descriptor_ref"]]
    damaged = archive["descriptor"]["inventory"][0]
    os_write_bytes(os.path.join(target, damaged["path"]), b"corrupt cached bytes")
    archive["reader"].reads.clear()
    repaired = dataset_recover(archive["reader"], archive["descriptor_ref"], archive["digest"], target)
    assert repaired == first
    assert archive["reader"].reads == [archive["descriptor_ref"], damaged["remote_ref"]]


def test3():
    data = pd_fake_dataset("dataset_corruption")
    prepared, handoff = dataset_prepare(data["dataset"], data["item"], data["native"], data["bundles"], data["paths"])
    archive = pd_fake_archive(prepared, handoff)
    target = os.path.join(data["root"], "workstation-staging")
    member = archive["descriptor"]["inventory"][0]
    original = archive["reader"].payloads[member["remote_ref"]]
    archive["reader"].payloads[member["remote_ref"]] = bytes([original[0] ^ 1]) + original[1:]
    with pd_rejected("checksum"):
        dataset_recover(archive["reader"], archive["descriptor_ref"], archive["digest"], target)
    assert not os.path.exists(os.path.join(target, member["path"]))
    archive["reader"].payloads[member["remote_ref"]] = original
    result = dataset_recover(archive["reader"], archive["descriptor_ref"], archive["digest"], target)
    assert result["archive_valid"]
    with pd_rejected("descriptor checksum"):
        dataset_recover(archive["reader"], archive["descriptor_ref"], "0" * 64, target)


def test4():
    data = pd_fake_dataset("dataset_native_negatives")
    inventory = dataset_inventory(data["dataset"], data["item"].artifacts)
    info = dict(fps=10, features=data["native"]["features"], total_frames=4, total_episodes=2)
    native, large_info = pd_fake_native_scale(data["native"])
    start = time.perf_counter()
    report = dataset_native_validate(native, inventory, large_info)
    log_info(f"native validation: {report['episodes']} episodes, {report['samples']} rows, {time.perf_counter()-start:.3f}s")
    assert report["episodes"] == 5000 and report["samples"] == 10000
    changes = [("physical ordinal", lambda n: n["rows"][2].update(row_ordinal=100)),
               ("timestamp grid", lambda n: n["rows"][1].update(timestamp_ns=99_000_000)),
               ("task is absent", lambda n: n["rows"][0].update(task_index=9)),
               ("invalid native vector", lambda n: n["rows"][0]["features"].update(action=[float("nan"), 1])),
               ("features differ", lambda n: n["rows"][0]["features"].pop("observation.images.wrist")),
               ("native camera feature", lambda n: n["rows"][0]["features"]["observation.images.front"].update(pts=201)),
               ("overlapping", lambda n: n["episodes"][1]["video_bounds"]["observation.images.front"].update(start_pts=100))]
    for reason, change in changes:
        native = copy.deepcopy(data["native"])
        change(native)
        with pd_rejected(reason):
            dataset_native_validate(native, inventory, info)
    # Numeric PTS ranges that differ can still overlap on rational media time.
    native = copy.deepcopy(data["native"])
    native["episodes"][1]["video_bounds"]["observation.images.front"]["time_base_den"] = 2000
    with pd_rejected("overlapping"):
        dataset_native_validate(native, inventory, info)
    bundles = copy.deepcopy(data["bundles"])
    bundles[0]["request"]["target_ns"] = 1
    with pd_rejected("retimes"):
        dataset_alignment(data["native"], bundles, inventory)
    bundles = copy.deepcopy(data["bundles"])
    bundles[0]["profile"]["required_modalities"].remove("observation.images.wrist")
    with pd_rejected("omits"):
        dataset_alignment(data["native"], bundles, inventory)
    bundles = copy.deepcopy(data["bundles"])
    bundles[0]["sources"][0]["pts"] = 201
    with pd_rejected("outside exact native"):
        dataset_alignment(data["native"], bundles, inventory)
    bundles = copy.deepcopy(data["bundles"])
    # A bounded temporal match cannot replace the original already-aligned frame.
    bundles[0]["sources"][0]["pts"] = 1
    results = dataset_alignment(data["native"], bundles, inventory)
    assert results[0]["status"] == "unresolved"
    assert "observation.images.front:native_camera_reference_mismatch" in results[0]["reasons"]


def test5():
    data = pd_fake_dataset("dataset_corrections")
    prepared, handoff = dataset_prepare(data["dataset"], data["item"], data["native"], data["bundles"], data["paths"])
    archive = pd_fake_archive(prepared, handoff)
    before = {key: os_read_bytes(os.path.join(data["dataset"], path)) for key, path in data["paths"].items()}
    # Exact replay is idempotent and emits the same sidecar checksums.
    again, replay = dataset_prepare(data["dataset"], data["item"], data["native"], data["bundles"], data["paths"])
    assert replay == handoff and [a.checksum for a in again.artifacts] == [a.checksum for a in prepared.artifacts]
    bundles = copy.deepcopy(data["bundles"])
    for bundle in bundles:
        bundle["clocks"][0].update(transform_id="corrected-clock-v2", ref_anchor_ns=100_000)
    with pd_rejected("immutable"):
        dataset_prepare(data["dataset"], data["item"], data["native"], bundles, data["paths"])
    item = replace(data["item"], source_revision="alignment-correction-002")
    paths = {key: f"sidecars/correction-002/{key}.json" for key in data["paths"]}
    corrected, new = dataset_prepare(data["dataset"], item, data["native"], bundles, paths,
                                     parent_descriptor_sha256=archive["digest"], correction_reason="new bounded clock-anchor evidence")
    assert new["parent_descriptor_sha256"] == archive["digest"]
    assert new["native_source_revision"] == handoff["native_source_revision"]
    assert all(os_read_bytes(os.path.join(data["dataset"], data["paths"][key])) == value for key, value in before.items())
    remote = pd_fake_archive(corrected, new)
    target = os.path.join(data["root"], "workstation-correction")
    report = dataset_recover(remote["reader"], remote["descriptor_ref"], remote["digest"], target)
    assert report["archive_valid"] and not report["alignment_valid"]  # shifted audio windows are no longer fully covered.
    assert report["training_loadable"] is False and report["search_resolvable"] is False


def test6():
    data = pd_fake_dataset("dataset_closure")
    prepared, handoff = dataset_prepare(data["dataset"], data["item"], data["native"], data["bundles"], data["paths"])
    archive = pd_fake_archive(prepared, handoff)
    with pd_rejected("exact revision dependency closure"):
        dataset_descriptor(handoff, archive["revision"], archive["artifacts"][:-1], archive["receipts"])
    receipts = dict(archive["receipts"])
    identity = archive["artifacts"][0].artifact_id
    receipts[identity] = replace(receipts[identity], checksum_verified=False)
    with pd_rejected("verified durable"):
        dataset_descriptor(handoff, archive["revision"], archive["artifacts"], receipts)
    target = os.path.join(data["root"], "workstation-foreign")
    os_write_bytes(os.path.join(target, "stale.parquet"), b"old revision")
    with pd_rejected("foreign revision"):
        dataset_recover(archive["reader"], archive["descriptor_ref"], archive["digest"], target)


def test7():
    data = pd_fake_dataset("dataset_tampered_alignment")
    prepared, handoff = dataset_prepare(data["dataset"], data["item"], data["native"], data["bundles"], data["paths"])
    archive = pd_fake_archive(prepared, handoff)
    descriptor = copy.deepcopy(archive["descriptor"])
    alignment = next(x for x in descriptor["inventory"] if x["path"] == data["paths"]["alignment"])
    original = archive["reader"].payloads[alignment["remote_ref"]]
    import json
    stored = json.loads(original)
    stored["results"][0]["selections"]["observation.state"]["sources"][0]["ordinal"] = 100
    value = str_json_canonical(stored)
    archive["reader"].payloads[alignment["remote_ref"]] = value
    alignment.update(size_bytes=len(value), sha256=str_sha256_bytes(value))
    raw = str_json_canonical(descriptor)
    archive["reader"].payloads[archive["descriptor_ref"]] = raw
    target = os.path.join(data["root"], "workstation-tampered")
    with pd_rejected("joins differ"):
        dataset_recover(archive["reader"], archive["descriptor_ref"], str_sha256_bytes(raw), target)


def test8():
    data = pd_fake_dataset("dataset_interrupted")
    prepared, handoff = dataset_prepare(data["dataset"], data["item"], data["native"], data["bundles"], data["paths"])
    archive = pd_fake_archive(prepared, handoff)
    target = os.path.join(data["root"], "workstation-interrupted")
    # Truncation is the observable byte-stream equivalent of an interrupted download.
    entry = archive["descriptor"]["inventory"][2]
    original = archive["reader"].payloads[entry["remote_ref"]]
    archive["reader"].payloads[entry["remote_ref"]] = original[:len(original) // 2]
    with pd_rejected("length"):
        dataset_recover(archive["reader"], archive["descriptor_ref"], archive["digest"], target)
    assert not os.path.exists(os.path.join(target, entry["path"]))
    archive["reader"].payloads[entry["remote_ref"]] = original
    assert dataset_recover(archive["reader"], archive["descriptor_ref"], archive["digest"], target)["archive_valid"]


def test9():
    data = pd_fake_dataset("dataset_simulation")
    native = copy.deepcopy(data["native"])
    native.update(source_kind="simulation", utc_anchored=False, simulation_destination=data["item"].destination,
                  simulation_provenance=dict(engine="synthetic-engine", version="1", seed=0, scene_sha256="1" * 64,
                                             config_sha256="2" * 64, robot_model="synthetic-robot", calibration_sha256="3" * 64,
                                             control_settings={"policy": "synthetic"}, step_ns=100_000_000))
    prepared, handoff = dataset_prepare(data["dataset"], data["item"], native, data["bundles"], data["paths"])
    archive = pd_fake_archive(prepared, handoff)
    result = dataset_recover(archive["reader"], archive["descriptor_ref"], archive["digest"], os.path.join(data["root"], "sim-workstation"))
    assert result["alignment_valid"] and result["native"]["source_kind"] == "simulation"
    assert result["search_resolvable"] is False
    native["utc_anchored"] = True
    inventory = dataset_inventory(data["dataset"], data["item"].artifacts)
    info = dict(fps=10, features=native["features"], total_frames=4, total_episodes=2)
    with pd_rejected("cannot claim real UTC"):
        dataset_native_validate(native, inventory, info)


def test_all():
    test1()
    test2()
    test3()
    test4()
    test5()
    test6()
    test7()
    test8()
    test9()
    log_info("dataset acceptance passed; synthetic archive/native scanner only")


if __name__ == "__main__":
    fire.Fire()
