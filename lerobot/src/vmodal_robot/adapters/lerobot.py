from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

import glob
import mimetypes
import re

from ..contracts import ARTIFACT_KINDS, ArtifactInput, ContractError, RevisionInput
from ..utils import os_json_load, os_path_has_symlink, os_safe_relative


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LeRobotAdapter:
    """Discover explicit LeRobot v3 completion manifests without importing LeRobot."""

    def __init__(self, ready_dir: str):
        self.ready_dir = os.path.abspath(os.path.expanduser(ready_dir))
        self._errors: List[Tuple[str, str]] = []
        self._seen: Dict[str, Tuple[int, int]] = {}

    def discover(self, limit: int = 100) -> List[RevisionInput]:
        items = []
        paths = sorted(glob.glob(os.path.join(self.ready_dir, "*.ready.json")))[: max(0, limit)]
        for path in paths:
            stat = os.stat(path)
            stamp = (stat.st_size, stat.st_mtime_ns)
            if self._seen.get(path) == stamp:
                continue
            try:
                items.append(self.os_load_manifest(path))
            except Exception as exc:
                self._errors.append((path, str(exc)))
            self._seen[path] = stamp
        return items

    def drain_errors(self) -> List[Tuple[str, str]]:
        values = self._errors
        self._errors = []
        return values

    def os_load_manifest(self, manifest_path: str) -> RevisionInput:
        data = os_json_load(manifest_path)
        if data.get("contract_version") != 1:
            raise ContractError("ready manifest contract_version must be 1")
        if data.get("source_format") != "lerobot":
            raise ContractError("source_format must be lerobot")
        if data.get("source_version") != "v3":
            raise ContractError("only LeRobot dataset v3 is supported")
        root_value = str(data.get("dataset_root", "."))
        if os.path.isabs(root_value):
            dataset_root = os.path.abspath(root_value)
        else:
            dataset_root = os.path.abspath(os.path.join(os.path.dirname(manifest_path), root_value))
        if not os.path.isdir(dataset_root):
            raise ContractError(f"dataset_root does not exist: {dataset_root}")
        source_id = str(data.get("source_id", "")).strip()
        dataset_key = str(data.get("dataset_key", "")).strip()
        destination = str(data.get("destination", "")).strip()
        source_revision = str(data.get("source_revision", "")).strip()
        if not all((source_id, dataset_key, destination, source_revision)):
            raise ContractError("source_id, dataset_key, destination, and source_revision are required")
        raw_artifacts = data.get("artifacts")
        if not isinstance(raw_artifacts, list) or not raw_artifacts:
            raise ContractError("artifacts must be a non-empty list")
        artifacts = []
        seen = set()
        for raw in raw_artifacts:
            if not isinstance(raw, dict):
                raise ContractError("each artifact must be an object")
            rel_path = os_safe_relative(str(raw.get("path", "")))
            if rel_path in seen:
                raise ContractError(f"duplicate artifact path: {rel_path}")
            seen.add(rel_path)
            kind = str(raw.get("kind", ""))
            if kind not in ARTIFACT_KINDS:
                raise ContractError(f"unsupported artifact kind: {kind}")
            source_path = os.path.abspath(os.path.join(dataset_root, rel_path))
            real_path = os.path.realpath(source_path)
            if os.path.commonpath((dataset_root, real_path)) != dataset_root:
                raise ContractError(f"artifact escapes dataset_root: {rel_path}")
            if os_path_has_symlink(source_path, dataset_root):
                raise ContractError(f"artifact path contains a symlink: {rel_path}")
            if not os.path.isfile(source_path):
                raise ContractError(f"ready artifact is missing: {rel_path}")
            if kind == "video" and not rel_path.lower().endswith(".mp4"):
                raise ContractError(f"LeRobot video must be MP4: {rel_path}")
            if kind == "telemetry" and not rel_path.lower().endswith(".parquet"):
                raise ContractError(f"LeRobot telemetry must be Parquet: {rel_path}")
            checksum = str(raw.get("sha256", "")).lower()
            if not _SHA256.fullmatch(checksum):
                raise ContractError(f"valid sha256 is required: {rel_path}")
            size = raw.get("size_bytes")
            if not isinstance(size, int) or size < 0:
                raise ContractError(f"non-negative size_bytes is required: {rel_path}")
            content_type = str(raw.get("content_type", "")).strip()
            if not content_type:
                content_type = mimetypes.guess_type(rel_path)[0] or "application/octet-stream"
            refs = raw.get("source_refs", {})
            if not isinstance(refs, dict):
                raise ContractError(f"source_refs must be an object: {rel_path}")
            timing = raw.get("timing", {})
            if not isinstance(timing, dict):
                raise ContractError(f"timing must be an object: {rel_path}")
            artifacts.append(
                ArtifactInput(
                    rel_path=rel_path,
                    kind=kind,
                    content_type=content_type,
                    source_path=source_path,
                    byte_length=size,
                    checksum=checksum,
                    source_clock=str(timing.get("source_clock", "")),
                    time_start=timing.get("start"),
                    time_end=timing.get("end"),
                    source_refs=refs,
                )
            )
        if not any(item.kind == "metadata" for item in artifacts):
            raise ContractError("each LeRobot revision requires an immutable metadata snapshot")
        return RevisionInput(
            source_id=source_id,
            dataset_key=dataset_key,
            source_format="lerobot",
            source_version="v3",
            destination=destination,
            source_revision=source_revision,
            complete=bool(data.get("complete", False)),
            artifacts=tuple(artifacts),
            manifest_path=os.path.abspath(manifest_path),
        )
