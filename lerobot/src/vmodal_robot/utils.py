"""Shared durable file operations for immutable artifacts and researcher sidecars.

# Usage:
  - str_sha256_file("dataset/meta/info.json")
  - os_write_bytes("dataset/sidecars/quality.json", b"{}", immutable=True)
"""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from vmodal_robot._support.util_log import log_info, log_error, log_trace, log_warning

import hashlib
import json
import shutil
import tempfile
import fcntl


def str_sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def os_bag_manifest(bag_dir: str, ready_dir: str, source_id: str,
                    dataset_key: str, destination: str, fleet_refs: bool = False) -> str:
    """Call only after the recorder has closed and stopped modifying this bag."""
    import yaml
    root = os.path.abspath(bag_dir)
    if not all((source_id.strip(), dataset_key.strip(), destination.strip())):
        raise ValueError("source_id, dataset_key and destination are required")
    meta = os.path.join(root, "metadata.yaml")
    before_meta = os.stat(meta)
    try:
        info = yaml.safe_load(os_read_bytes(meta))["rosbag2_bagfile_information"]
    except yaml.YAMLError as exc:
        raise ValueError("invalid bag metadata YAML") from exc
    paths = info["relative_file_paths"]
    if not isinstance(paths, list) or not paths or len(set(paths)) != len(paths):
        raise ValueError("metadata must list unique closed storage files")
    artifacts = []
    for rel in ["metadata.yaml"] + sorted(paths):
        rel = os_safe_relative(rel)
        path = os.path.join(root, rel)
        if os_path_has_symlink(path, root) or not os.path.isfile(path):
            raise ValueError("bag artifact must be a regular file within bag_dir")
        if rel != "metadata.yaml" and not rel.endswith((".db3", ".mcap", ".db3.zstd", ".mcap.zstd")):
            raise ValueError("unsupported native bag storage file")
        if os.path.exists(path + "-wal") or os.path.exists(path + "-shm"):
            raise ValueError("SQLite bag is still open or requires checkpointing")
        before = os.stat(path)
        with open(path, "rb") as handle:
            os.fsync(handle.fileno())
        checksum = str_sha256_file(path)
        after = os.stat(path)
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("bag artifact changed during handoff")
        refs = dict(bag_name=os.path.basename(root), storage_identifier=info["storage_identifier"])
        if fleet_refs:
            refs.update(robot_id=source_id, mission_id=dataset_key)
        artifacts.append(dict(path=rel, kind="metadata" if rel == "metadata.yaml" else "opaque",
                              size_bytes=after.st_size, sha256=checksum,
                              content_type="application/yaml" if rel == "metadata.yaml" else "application/octet-stream",
                              source_refs=refs))
    after_meta = os.stat(meta)
    if (before_meta.st_ino, before_meta.st_size, before_meta.st_mtime_ns) != (after_meta.st_ino, after_meta.st_size, after_meta.st_mtime_ns):
        raise ValueError("bag metadata changed during handoff")
    identity = str_sha256_bytes(str_json_canonical(dict(bag=root, artifacts=artifacts)))
    value = dict(contract_version=1, source_format="rosbag2", source_version="1",
                 source_id=source_id, dataset_key=dataset_key, destination=destination,
                 dataset_root=root, source_revision=identity, complete=True, artifacts=artifacts)
    target = os.path.join(ready_dir, identity + ".ready.json")
    os_write_bytes(target, str_json_canonical(value), immutable=True)
    return os.path.abspath(target)


def os_bag_closed(bag_dir: str, closed_dir: str):
    """Persist successful recorder completion; call only after recorder exit."""
    if not os.path.isabs(bag_dir):
        raise ValueError("closed bag notification requires an absolute path")
    key = str_sha256_bytes(bag_dir.encode())
    os_write_bytes(os.path.join(closed_dir, key + ".closed"), bag_dir.encode(), immutable=True)


def str_json_canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def os_read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def os_makedirs_durable(path: str):
    """Persist new directory entries through their existing ancestor."""
    created = []
    current = os.path.abspath(path)
    while not os.path.isdir(current):
        created.append(current)
        current = os.path.dirname(current)
    os.makedirs(path, exist_ok=True)
    for folder in created:
        os_fsync_dir(folder)
        os_fsync_dir(os.path.dirname(folder))


def os_write_bytes(path: str, value: bytes, immutable: bool = False):
    """Atomically commit bytes; immutable replay verifies bytes and never replaces."""
    parent = os.path.dirname(os.path.abspath(path))
    os_makedirs_durable(parent)
    fd, temp = tempfile.mkstemp(prefix=".sidecar-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        if immutable:
            try:
                os.link(temp, path)
            except FileExistsError:
                if os.path.islink(path) or os_read_bytes(path) != value:
                    raise ValueError(f"immutable file conflict: {path}")
        else:
            os.replace(temp, path)
        os_fsync_dir(parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def os_download_verified(reader, remote_ref: str, target: str, size: int, checksum: str):
    """Resume verified files; stream remote bytes to a temporary file before exposure."""
    if type(size) is not int or size < 0 or len(checksum) != 64:
        raise ValueError("expected nonnegative size and SHA-256 are required")
    parent = os.path.dirname(os.path.abspath(target))
    os_makedirs_durable(parent)
    if os.path.islink(target):
        raise ValueError(f"recovery target is a symlink: {target}")
    if os.path.isfile(target) and os.path.getsize(target) == size and str_sha256_file(target) == checksum:
        return False
    fd, temp = tempfile.mkstemp(prefix=".download-", dir=parent)
    digest, length = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as output, reader.open_artifact(remote_ref) as source:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                length += len(block)
                if length > size:
                    raise ValueError(f"download length mismatch: {remote_ref}")
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if length != size or digest.hexdigest() != checksum:
            raise ValueError(f"download checksum/length mismatch: {remote_ref}")
        os.replace(temp, target)
        os_fsync_dir(parent)
        return True
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def os_marker_append(path: str, record: Dict[str, Any], max_bytes: int = 4096):
    """One bounded local append, fsynced per call; failed partial writes roll back."""
    value = str_json_canonical(record) + b"\n"
    if len(value) > max_bytes:
        raise ValueError(f"marker exceeds {max_bytes} bytes")
    parent = os.path.dirname(os.path.abspath(path))
    os_makedirs_durable(parent)
    with open(path + ".lock", "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(path, "a+b") as output:
            length = output.seek(0, os.SEEK_END)
            try:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
            except OSError:
                output.seek(length)
                output.truncate()
                output.flush()
                os.fsync(output.fileno())
                raise
        os_fsync_dir(parent)


def os_marker_rotate(path: str, target: str):
    """Close a marker artifact under the same writer lock; never overwrite a target."""
    if os.path.abspath(path) == os.path.abspath(target):
        raise ValueError("closed marker path must differ from active path")
    parent = os.path.dirname(os.path.abspath(target))
    os_makedirs_durable(parent)
    with open(path + ".lock", "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if os.path.lexists(target):
            raise ValueError(f"closed marker artifact exists: {target}")
        if not os.path.isfile(path) or os.path.islink(path):
            raise ValueError("active marker artifact must be a regular file")
        with open(path, "rb") as handle:
            os.fsync(handle.fileno())
        os.rename(path, target)
        os_fsync_dir(parent)
        os_fsync_dir(os.path.dirname(os.path.abspath(path)))


def os_fsync_dir(path: str):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def str_sha256_file(path: str, chunk_bytes: int = 1024 * 1024, stop_event=None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            os_check_stop(stop_event)
            block = handle.read(chunk_bytes)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def str_stable_id(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        value = str(part).encode("utf-8")
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def os_safe_relative(path: str) -> str:
    value = str(path).replace("\\", "/").strip()
    norm = os.path.normpath(value).replace("\\", "/")
    if not value or os.path.isabs(value) or norm == ".." or norm.startswith("../"):
        raise ValueError(f"path must be relative and remain inside the dataset: {path}")
    return norm


def os_path_has_symlink(path: str, stop: str) -> bool:
    current = os.path.abspath(path)
    stop_abs = os.path.abspath(stop)
    while current != stop_abs:
        if os.path.islink(current):
            return True
        parent = os.path.dirname(current)
        if parent == current or os.path.commonpath((stop_abs, parent)) != stop_abs:
            return True
        current = parent
    return os.path.islink(stop_abs)


def os_json_load(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def os_check_stop(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise InterruptedError("spool admission stopped")


def os_atomic_copy(source: str, target: str, chunk_bytes: int = 1024 * 1024, stop_event=None) -> Tuple[int, str]:
    os_check_stop(stop_event)
    parent = os.path.dirname(target)
    created = []
    current = os.path.abspath(parent)
    while not os.path.isdir(current):
        created.append(current)
        current = os.path.dirname(current)
    os.makedirs(parent, exist_ok=True)
    before = os.stat(source, follow_symlinks=False)
    if not os.path.isfile(source) or os.path.islink(source):
        raise ValueError(f"source must be a regular non-symlink file: {source}")
    digest = hashlib.sha256()
    fd, temp = tempfile.mkstemp(prefix=".accept-", dir=parent)
    size = 0
    try:
        with os.fdopen(fd, "wb") as output, open(source, "rb") as input_file:
            while True:
                os_check_stop(stop_event)
                block = input_file.read(chunk_bytes)
                if not block:
                    break
                os_check_stop(stop_event)
                output.write(block)
                digest.update(block)
                size += len(block)
            os_check_stop(stop_event)
            output.flush()
            os.fsync(output.fileno())
        after = os.stat(source, follow_symlinks=False)
        fields_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        fields_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if fields_before != fields_after or size != before.st_size:
            raise ValueError(f"source changed while being accepted: {source}")
        os_check_stop(stop_event)
        os.replace(temp, target)
        os_fsync_dir(parent)
        for path in created:
            os_fsync_dir(os.path.dirname(path))
        return size, digest.hexdigest()
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def os_remove_empty_parents(path: str, stop: str):
    current = os.path.dirname(path)
    stop_abs = os.path.abspath(stop)
    while os.path.abspath(current).startswith(stop_abs + os.sep):
        try:
            os.rmdir(current)
        except OSError:
            break
        current = os.path.dirname(current)


def os_json_save(path: str, value: Dict[str, Any]):
    text = json.dumps(value, indent=2, allow_nan=False) + "\n"
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".report-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os_fsync_dir(parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
