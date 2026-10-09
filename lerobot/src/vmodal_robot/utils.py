from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

import hashlib
import json
import shutil
import tempfile


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
