"""Validate closed researcher inputs and recover immutable dataset closures.

# Usage:
    from vmodal_robot.reconciliation.dataset import dataset_prepare, dataset_recover
    # Supply explicit native scanner output and the qualified remote reader seam.
    # See docs/researcher_workflow.md for the complete offline example.
"""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass, replace
import os,sys
import fire
from vmodal_robot._support.util_log import log_info, log_error, log_trace, log_warning

import json
import math
from fractions import Fraction

from ..contracts import ArtifactInput, ContractError, RevisionInput, DatasetRevision, Artifact, Receipt
from ..utils import (os_safe_relative, os_path_has_symlink, os_json_load,
                     os_read_bytes, os_write_bytes, os_download_verified,
                     str_sha256_file, str_sha256_bytes, str_json_canonical)
from .temporal import temporal_bundle


def dataset_inventory(root: str, artifacts) -> List[Dict[str, Any]]:
    """Verify every explicit dependency, preserving original relative paths."""
    root = os.path.abspath(root)
    entries = []
    seen = set()
    for item in artifacts:
        path = os_safe_relative(item.rel_path)
        if path != item.rel_path or path in seen:
            raise ContractError(f"noncanonical or duplicate inventory path: {item.rel_path}")
        seen.add(path)
        source = os.path.join(root, path)
        if os.path.abspath(item.source_path) != source or os_path_has_symlink(source, root):
            raise ContractError(f"inventory source escapes closed dataset: {path}")
        if not os.path.isfile(source) or os.path.getsize(source) != item.byte_length:
            raise ContractError(f"inventory length mismatch: {path}")
        if str_sha256_file(source) != item.checksum:
            raise ContractError(f"inventory checksum mismatch: {path}")
        entries.append(dict(path=path, kind=item.kind, content_type=item.content_type,
                            size_bytes=item.byte_length, sha256=item.checksum,
                            source_refs=item.source_refs or {},
                            timing=dict(source_clock=item.source_clock, start=item.time_start, end=item.time_end)))
    if not entries:
        raise ContractError("closed inventory cannot be empty")
    return sorted(entries, key=lambda value: value["path"])


def dataset_native_validate(native: Dict[str, Any], inventory, info: Dict[str, Any]) -> Dict[str, Any]:
    """Check a pinned scanner's normalized rows; this does not decode Parquet/media.

    Exact feature declarations include dtype, shape, component names, units and
    coordinate/action semantics supplied by the caller. Native timestamps remain
    integer nanoseconds; no retiming or row repair is performed here.
    """
    paths = {value["path"]: value for value in inventory}
    if native.get("schema_version") != 1 or not native.get("producer_version") or not native.get("source_revision") or not native.get("dataset_id"):
        raise ContractError("native schema/producer version required")
    if not native.get("scanner_version") or not native.get("feature_profile_id"):
        raise ContractError("pinned scanner and feature profile required")
    if native.get("source_kind") not in ("real", "simulation", "synthetic"):
        raise ContractError("explicit real/simulation/synthetic source kind required")
    if native["source_kind"] == "simulation":
        provenance = native.get("simulation_provenance", {})
        if not all(key in provenance for key in ("engine", "version", "seed", "scene_sha256", "config_sha256", "robot_model", "calibration_sha256", "control_settings", "step_ns")):
            raise ContractError("complete simulation provenance required")
        if type(provenance["step_ns"]) is not int or provenance["step_ns"] <= 0 or native.get("utc_anchored") is not False:
            raise ContractError("simulation step must be positive; simulation cannot claim real UTC")
    for key in ("info_path",):
        if native.get(key) not in paths:
            raise ContractError(f"missing native metadata dependency: {key}")
    metadata = native.get("metadata_paths", [])
    if not metadata or any(path not in paths for path in metadata):
        raise ContractError("complete task/episode/stats metadata dependencies required")
    features = native.get("features", {})
    if not features or features != info.get("features"):
        raise ContractError("feature profile differs from native meta/info.json")
    for key in ("observation.state", "action"):
        desc = features.get(key, {})
        if not all(desc.get(name) for name in ("dtype", "shape", "names", "units", "semantics")):
            raise ContractError(f"explicit feature semantics required: {key}")
        if len(desc["shape"]) != 1 or desc["shape"][0] != len(desc["names"]) or len(desc["units"]) != len(desc["names"]):
            raise ContractError(f"feature shape/component contract mismatch: {key}")
    fps = native.get("fps")
    tolerance = native.get("timestamp_tolerance_ns")
    if not isinstance(fps, int) or isinstance(fps, bool) or fps <= 0 or info.get("fps") != fps:
        raise ContractError("positive pinned FPS matching native metadata required")
    if not isinstance(tolerance, int) or isinstance(tolerance, bool) or tolerance < 0:
        raise ContractError("explicit nonnegative timestamp tolerance required")
    tasks = native.get("task_indices", [])
    if not tasks or len(set(tasks)) != len(tasks) or any(not isinstance(x, int) or x < 0 for x in tasks):
        raise ContractError("unique native task identities required")
    episodes = native.get("episodes", [])
    rows = native.get("rows", [])
    if not episodes or not rows:
        raise ContractError("closed episode and row inventory required")
    if info.get("total_frames") != len(rows) or info.get("total_episodes") != len(episodes):
        raise ContractError("closed sample/episode inventory differs from native metadata totals")
    cameras = {key for key, desc in features.items() if desc.get("dtype") == "video"}
    if not cameras:
        raise ContractError("native camera feature required")
    ep_ids = set()
    row_ids = set()
    physical = set()
    matched = set()
    for episode in episodes:
        ep = episode["episode_index"]
        if not isinstance(ep, int) or isinstance(ep, bool) or ep < 0 or ep in ep_ids:
            raise ContractError("duplicate or invalid episode identity")
        ep_ids.add(ep)
        if not all(episode.get(key) for key in ("reference_clock", "reference_epoch")) or not isinstance(episode.get("reference_start_ns"), int):
            raise ContractError("explicit episode reference clock/epoch/origin required")
        start, end = episode["row_start"], episode["row_end"]
        length = episode["length"]
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in (start, end, length, episode["start_index"])):
            raise ContractError("integer episode/shard bounds required")
        if start < 0 or length <= 0 or end - start != length or episode["start_index"] < 0:
            raise ContractError("invalid episode physical row bounds")
        path = episode["data_path"]
        if path not in paths or paths[path]["kind"] != "telemetry":
            raise ContractError("episode telemetry missing from closure")
        bounds = episode.get("video_bounds", {})
        if set(bounds) != cameras:
            raise ContractError("every required camera needs explicit episode bounds")
        for camera, bound in bounds.items():
            if bound["path"] not in paths or paths[bound["path"]]["kind"] != "video":
                raise ContractError("camera original missing from closure")
            if not all(isinstance(bound[key], int) and not isinstance(bound[key], bool) for key in ("start_pts", "end_pts", "time_base_num", "time_base_den")):
                raise ContractError("integer PTS/rational time base required")
            if bound["end_pts"] <= bound["start_pts"] or min(bound["time_base_num"], bound["time_base_den"]) <= 0:
                raise ContractError("invalid camera episode interval")
        members = sorted((row for row in rows if row["episode_index"] == ep), key=lambda row: row["frame_index"])
        if len(members) != length:
            raise ContractError("episode length differs from decoded row inventory")
        prev = None
        for frame, row in enumerate(members):
            identity = (ep, row["frame_index"])
            place = (row["data_path"], row["row_ordinal"])
            if any(not isinstance(row[key], int) or isinstance(row[key], bool) for key in ("frame_index", "index", "row_ordinal", "timestamp_ns", "task_index")):
                raise ContractError("integer native row identities required")
            if row["frame_index"] != frame or row["index"] != episode["start_index"] + frame:
                raise ContractError("native frame/dataset index mismatch")
            if row["data_path"] != path or row["row_ordinal"] != start + frame:
                raise ContractError("native physical ordinal mismatch")
            if identity in matched or row["index"] in row_ids or place in physical:
                raise ContractError("duplicate native sample/physical row identity")
            matched.add(identity)
            row_ids.add(row["index"])
            physical.add(place)
            stamp = row["timestamp_ns"]
            if stamp < 0 or abs(Fraction(stamp) - Fraction(frame * 1_000_000_000, fps)) > tolerance:
                raise ContractError("native timestamp grid violates pinned tolerance")
            if prev is not None and stamp <= prev:
                raise ContractError("native timestamp regression/duplicate")
            prev = stamp
            if row["task_index"] not in tasks:
                raise ContractError("native row task is absent from metadata")
            values = row.get("features", {})
            if set(values) != set(features):
                raise ContractError("native row features differ from pinned feature profile")
            for key in ("observation.state", "action"):
                vec = values[key]
                if len(vec) != features[key]["shape"][0] or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in vec):
                    raise ContractError(f"invalid native vector: {key}")
            for key in cameras:
                value, bound = values[key], bounds[key]
                if not isinstance(value, dict) or value.get("path") != bound["path"] or type(value.get("pts")) is not int or not bound["start_pts"] <= value["pts"] < bound["end_pts"]:
                    raise ContractError("native camera feature references wrong artifact/episode PTS")
    if len(matched) != len(rows):
        raise ContractError("row references unknown episode")
    # Shared shards may contain real capture gaps; reject overlapping ownership.
    for camera in cameras:
        for i, left in enumerate(episodes):
            a = left["video_bounds"][camera]
            for right in episodes[i + 1:]:
                b = right["video_bounds"][camera]
                if a["path"] == b["path"] and max(a["start_pts"], b["start_pts"]) < min(a["end_pts"], b["end_pts"]):
                    raise ContractError("overlapping shared-shard episode video intervals")
    return dict(status="valid", samples=len(rows), episodes=len(episodes),
                feature_profile_id=native["feature_profile_id"], scanner_version=native["scanner_version"],
                source_kind=native["source_kind"],
                decoder_verified=False, pinned_reader_verified=False)


def dataset_alignment(native, bundles, inventory):
    """Bind normalized temporal inputs to exact native rows and closed source bytes."""
    paths = {value["path"]: value for value in inventory}
    rows = {(row["episode_index"], row["frame_index"]): row for row in native["rows"]}
    episodes = {str(episode["episode_index"]): episode for episode in native["episodes"]}
    source_revision = native["source_revision"]
    seen = set()
    results = []
    for bundle in bundles:
        request = bundle["request"]
        # Sample IDs encode all canonical identity fields, never only dataset index.
        sample = request["sample_id"]
        if not isinstance(sample, str) or sample in seen:
            raise ContractError("unique canonical temporal sample ID required")
        seen.add(sample)
        identity = bundle.get("native_sample")
        if not isinstance(identity, dict) or identity.get("source_revision") != source_revision or identity.get("dataset_id") != native["dataset_id"]:
            raise ContractError("native sample revision identity required")
        required = {key for key, desc in native["features"].items() if desc.get("dtype") == "video"} | {"observation.state", "action"}
        if not required.issubset(set(bundle["profile"]["required_modalities"])):
            raise ContractError("temporal profile omits required native camera/state/action")
        row = rows.get((identity.get("episode_index"), identity.get("frame_index")))
        if row is None or request["episode"] != str(row["episode_index"]) or request["revision"] != source_revision:
            raise ContractError("temporal request/native episode mismatch")
        expected = [identity.get("dataset_id"), source_revision, row["episode_index"], row["frame_index"]]
        if not expected[0] or sample != json.dumps(expected, separators=(",", ":")):
            raise ContractError("temporal sample ID must encode dataset/revision/episode/frame")
        if identity.get("timestamp_ns") != row["timestamp_ns"] or identity.get("data_path") != row["data_path"] or identity.get("row_ordinal") != row["row_ordinal"]:
            raise ContractError("temporal/native row ordinal or timestamp mismatch")
        episode = episodes[str(row["episode_index"])]
        if request["reference_clock"] != episode["reference_clock"] or request["reference_epoch"] != episode["reference_epoch"] or request["target_ns"] != episode["reference_start_ns"] + row["timestamp_ns"]:
            raise ContractError("temporal request retimes the native sample grid")
        for source in bundle["sources"]:
            path = source.get("path")
            if path not in paths or source.get("checksum") != paths[path]["sha256"]:
                raise ContractError("temporal source missing/checksum differs from immutable inventory")
            ep = episodes.get(source.get("episode"))
            if ep is None or source.get("revision") != source_revision:
                raise ContractError("temporal source/native revision or episode mismatch")
            if source["kind"] == "video":
                bound = ep["video_bounds"].get(source["modality"])
                if bound is None or path != bound["path"] or not bound["start_pts"] <= source.get("pts", -1) < bound["end_pts"]:
                    raise ContractError("video source is outside exact native episode/camera PTS interval")
                if source.get("time_base_num") != bound["time_base_num"] or source.get("time_base_den") != bound["time_base_den"]:
                    raise ContractError("video source time base differs from native episode mapping")
            if source["kind"] in ("telemetry", "action") and paths[path]["kind"] == "telemetry":
                if path != ep["data_path"] or not ep["row_start"] <= source["ordinal"] < ep["row_end"]:
                    raise ContractError("telemetry/action source physical ordinal outside native episode")
        core = {key: bundle[key] for key in ("request", "profile", "clocks", "sources")}
        result = temporal_bundle(core)
        reasons = list(result["reasons"])
        for key, feature in native["features"].items():
            selected = result["selections"].get(key)
            if selected is None or selected["status"] != "valid":
                continue
            refs = selected["sources"]
            if feature.get("dtype") == "video":
                expected = row["features"][key]
                if len(refs) != 1 or refs[0]["path"] != expected["path"] or refs[0]["pts"] != expected["pts"]:
                    reasons.append(f"{key}:native_camera_reference_mismatch")
            if key in ("observation.state", "action"):
                if len(refs) != 1 or refs[0]["path"] != row["data_path"] or refs[0]["ordinal"] != row["row_ordinal"]:
                    reasons.append(f"{key}:native_physical_row_mismatch")
        if reasons:
            result.update(status="unresolved", reasons=sorted(set(reasons)))
        results.append(result)
    if len(bundles) != len(rows):
        raise ContractError("every native sample requires exactly one persisted temporal request")
    actual = {(b["native_sample"]["episode_index"], b["native_sample"]["frame_index"]) for b in bundles}
    if actual != set(rows):
        raise ContractError("temporal requests do not cover unique native samples")
    return results


def dataset_prepare(root: str, item: RevisionInput, native: Dict[str, Any], bundles: List[Dict[str, Any]],
                    paths: Dict[str, str], parent_descriptor_sha256: str = "", correction_reason: str = ""):
    """Finalize immutable sidecars after the caller closes the pinned producer.

    The caller supplies paths, normalized scanner output and complete temporal
    evidence. Invalid joins remain explicit and archival delivery stays possible.
    Late evidence requires a new revision, new paths, a parent digest and reason.
    """
    if not item.complete or item.source_format != "lerobot" or item.source_version != "v3":
        raise ContractError("closed native LeRobot v3 revision required")
    if set(paths) != {"native", "temporal", "alignment", "quality"}:
        raise ContractError("explicit native/temporal/alignment/quality sidecar paths required")
    if bool(parent_descriptor_sha256) != bool(correction_reason):
        raise ContractError("correction requires both parent descriptor digest and reason")
    if parent_descriptor_sha256 and (len(parent_descriptor_sha256) != 64 or any(c not in "0123456789abcdef" for c in parent_descriptor_sha256)):
        raise ContractError("invalid parent descriptor SHA256")
    root = os.path.abspath(root)
    inventory = dataset_inventory(root, item.artifacts)
    report = dataset_native_validate(native, inventory, os_json_load(os.path.join(root, native["info_path"])))
    if native["source_kind"] == "simulation" and native.get("simulation_destination") != item.destination:
        raise ContractError("simulation requires an explicit isolated destination declaration")
    results = dataset_alignment(native, bundles, inventory)
    quality = dataset_quality(native, report, results, parent_descriptor_sha256, correction_reason)
    values = dict(native=native, temporal=dict(schema_version=1, bundles=bundles),
                  alignment=dict(schema_version=1, results=results), quality=quality)
    known = {value["path"] for value in inventory}
    normalized = [os_safe_relative(paths[key]) for key in sorted(paths)]
    if set(normalized) != set(paths.values()) or len(set(normalized)) != 4 or set(normalized) & known:
        raise ContractError("sidecar paths must be canonical, distinct and separate from originals")
    sidecars = []
    for key in sorted(paths):
        path = os_safe_relative(paths[key])
        if path != paths[key] or path in known or path in [x.rel_path for x in sidecars]:
            raise ContractError("sidecar paths must be canonical, distinct and separate from originals")
        target = os.path.join(root, path)
        if os_path_has_symlink(target, root):
            raise ContractError("sidecar path contains symlink")
        data = str_json_canonical(values[key])
        os_write_bytes(target, data, immutable=True)
        sidecars.append(ArtifactInput(path, "metadata", "application/json", target, len(data), str_sha256_bytes(data),
                                    source_refs=dict(researcher_sidecar=key, source_revision=item.source_revision)))
    artifacts = tuple(replace(a, source_refs=dict(a.source_refs or {}, researcher_sidecars=paths)) for a in item.artifacts) + tuple(sidecars)
    prepared = replace(item, artifacts=artifacts)
    handoff = dict(schema_version=1, source_id=item.source_id, dataset_key=item.dataset_key,
                   source_revision=item.source_revision, source_format=item.source_format,
                   source_version=item.source_version, destination=item.destination,
                   complete=True, sidecars=paths, inventory=dataset_inventory(root, artifacts),
                   native_source_revision=native["source_revision"],
                   native_dataset_id=native["dataset_id"],
                   parent_descriptor_sha256=parent_descriptor_sha256, correction_reason=correction_reason)
    return prepared, handoff


def dataset_quality(native, report, results, parent_descriptor_sha256, correction_reason):
    """Persist only reproducible validation evidence and independently pending gates."""
    return dict(schema_version=1, native=report, alignment_valid=all(r["status"] == "valid" for r in results),
                training_loadable=False, search_resolvable=False, producer_version=native["producer_version"],
                reasons=[r for r in results if r["status"] != "valid"],
                parent_descriptor_sha256=parent_descriptor_sha256, correction_reason=correction_reason)


def dataset_ready(root: str, item: RevisionInput, ready_path: str):
    """Emit the existing adapter's atomic handoff last, with verified closed bytes."""
    inventory = dataset_inventory(root, item.artifacts)
    manifest = dict(contract_version=1, source_id=item.source_id, dataset_key=item.dataset_key,
                    dataset_root=os.path.relpath(os.path.abspath(root), os.path.dirname(os.path.abspath(ready_path))),
                    source_revision=item.source_revision, source_format=item.source_format,
                    source_version=item.source_version, destination=item.destination, complete=item.complete,
                    artifacts=inventory)
    os_write_bytes(ready_path, str_json_canonical(manifest), immutable=True)
    return manifest


def dataset_descriptor(handoff, revision: DatasetRevision, artifacts: List[Artifact], receipts: Dict[str, Receipt]):
    """Bind complete published closure identities; storage/publication stays external.

    A descriptor constructed here still needs a durable, qualified publication
    implementation. The workstation independently requires its expected digest.
    """
    if not revision.complete or revision.source_revision != handoff["source_revision"] or revision.destination != handoff["destination"] or revision.source_id != handoff["source_id"]:
        raise ContractError("revision/handoff publication identity mismatch")
    if len(set(revision.artifact_ids)) != len(revision.artifact_ids) or set(revision.artifact_ids) != {a.artifact_id for a in artifacts}:
        raise ContractError("descriptor must contain exact revision dependency closure")
    paths = {entry["path"]: entry for entry in handoff["inventory"]}
    if len(paths) != len(artifacts) or set(paths) != {a.rel_path for a in artifacts}:
        raise ContractError("descriptor omitted or duplicated inventory member")
    inventory = []
    for artifact in sorted(artifacts, key=lambda a: a.rel_path):
        entry = paths[artifact.rel_path]
        receipt = receipts.get(artifact.artifact_id)
        if artifact.dataset_id != revision.dataset_id or artifact.source_id != revision.source_id or artifact.byte_length != entry["size_bytes"] or artifact.checksum != entry["sha256"] or artifact.kind != entry["kind"] or artifact.content_type != entry["content_type"]:
            raise ContractError("artifact identity/bytes differ from immutable handoff")
        if receipt is None or receipt.artifact_id != artifact.artifact_id or receipt.status != "ACKNOWLEDGED" or not receipt.checksum_verified or receipt.checksum != artifact.checksum or not receipt.remote_ref:
            raise ContractError("verified durable artifact receipt required for every dependency")
        inventory.append(dict(entry, artifact_id=artifact.artifact_id, remote_ref=receipt.remote_ref))
    return dict(handoff, revision_id=revision.revision_id, dataset_id=revision.dataset_id, inventory=inventory)


def dataset_recover(reader, descriptor_ref: str, expected_sha256: str, target: str) -> Dict[str, Any]:
    """Recover by remote descriptor alone; resume verified files and fail closed.

    `reader.open_artifact(ref)` returns a binary context manager. It must resolve
    a published descriptor and renewable stable artifact refs through a qualified
    SDK implementation. Tests supply an independent byte store, never a spool.
    `target` is a caller-selected staging directory, exposed only after this
    function returns archive_valid. A checksum failure never writes completion.
    """
    with reader.open_artifact(descriptor_ref) as handle:
        data = handle.read()
    if str_sha256_bytes(data) != expected_sha256:
        raise ContractError("remote descriptor checksum mismatch")
    descriptor = json.loads(data)
    if descriptor.get("schema_version") != 1 or descriptor.get("complete") is not True or not descriptor.get("revision_id") or not descriptor.get("dataset_id"):
        raise ContractError("complete versioned published descriptor required")
    inventory = descriptor.get("inventory", [])
    seen = set()
    identities = set()
    refs = set()
    for entry in inventory:
        path = os_safe_relative(entry["path"])
        if path != entry["path"] or path in seen or entry["artifact_id"] in identities or entry["remote_ref"] in refs:
            raise ContractError("duplicate/noncanonical descriptor dependency")
        if not isinstance(entry["size_bytes"], int) or isinstance(entry["size_bytes"], bool) or entry["size_bytes"] < 0 or len(entry["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in entry["sha256"]):
            raise ContractError("invalid descriptor length/checksum")
        seen.add(path)
        identities.add(entry["artifact_id"])
        refs.add(entry["remote_ref"])
    sidecars = descriptor.get("sidecars", {})
    if not inventory or set(sidecars) != {"native", "temporal", "alignment", "quality"} or any(path not in seen for path in sidecars.values()) or len(set(sidecars.values())) != 4:
        raise ContractError("complete original/sidecar inventory required")
    target = os.path.abspath(target)
    if os_path_has_symlink(target, os.path.dirname(target)):
        raise ContractError("recovery target contains a symlink")
    # Reject existing files outside this exact closure rather than exposing stale revisions.
    for base, dirs, files in os.walk(target):
        if any(os.path.islink(os.path.join(base, name)) for name in dirs + files):
            raise ContractError("recovery staging contains a symlink")
        for name in files:
            if os.path.relpath(os.path.join(base, name), target).replace(os.sep, "/") not in seen:
                raise ContractError("recovery staging contains a foreign revision member")
    for entry in inventory:
        path = os.path.join(target, entry["path"])
        if os_path_has_symlink(path, target):
            raise ContractError("recovery artifact path contains symlink")
        os_download_verified(reader, entry["remote_ref"], path, entry["size_bytes"], entry["sha256"])
    native = os_json_load(os.path.join(target, sidecars["native"]))
    native_report = dataset_native_validate(native, inventory, os_json_load(os.path.join(target, native["info_path"])))
    temporal = os_json_load(os.path.join(target, sidecars["temporal"]))
    alignment = os_json_load(os.path.join(target, sidecars["alignment"]))
    if temporal.get("schema_version") != 1 or alignment.get("schema_version") != 1:
        raise ContractError("unsupported temporal/alignment sidecar schema")
    bundles, stored = temporal["bundles"], alignment["results"]
    if native["source_revision"] != descriptor.get("native_source_revision") or native["dataset_id"] != descriptor.get("native_dataset_id"):
        raise ContractError("descriptor/native capture revision mismatch")
    if native["source_kind"] == "simulation" and native.get("simulation_destination") != descriptor["destination"]:
        raise ContractError("simulation descriptor destination differs from isolation declaration")
    results = dataset_alignment(native, bundles, inventory)
    if str_json_canonical(results) != str_json_canonical(stored):
        raise ContractError("recovered temporal joins differ from persisted alignment")
    quality = os_json_load(os.path.join(target, sidecars["quality"]))
    valid = all(r["status"] == "valid" for r in results)
    expected_quality = dataset_quality(native, native_report, results, descriptor.get("parent_descriptor_sha256", ""), descriptor.get("correction_reason", ""))
    if str_json_canonical(quality) != str_json_canonical(expected_quality):
        raise ContractError("persisted quality differs from reconstructed validation")
    return dict(schema_version=1, descriptor_sha256=expected_sha256, revision_id=descriptor["revision_id"],
                dataset_id=descriptor["dataset_id"], destination=descriptor["destination"],
                native_dataset_id=native["dataset_id"], native_source_revision=native["source_revision"],
                archive_valid=True, alignment_valid=valid, training_loadable=False, search_resolvable=False,
                native=native_report, alignment=results, root=target,
                pending=dict(training_loadable="pinned_native_reader_and_policy_batch_not_qualified",
                             search_resolvable="deployed_index_and_exact_search_mapping_not_qualified"))


if __name__ == "__main__":
    fire.Fire()
