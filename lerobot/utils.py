from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

import shutil
import subprocess
from pathlib import Path


def os_run(args: List[str], cwd: Path) -> str:
    row = subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True)
    return row.stdout


def os_read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def os_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def os_text_files(base: Path) -> List[Tuple[Path, str]]:
    rows: List[Tuple[Path, str]] = []
    for path in sorted(item for item in base.rglob("*") if item.is_file()):
        try:
            value = os_read_text(path)
        except UnicodeDecodeError:
            continue
        rows.append((path.relative_to(base), value))
    return rows


def os_replace_tree(source: Path, target: Path, expected_name: str) -> None:
    if target.name != expected_name:
        raise ValueError(f"refusing to replace unexpected directory: {target}")
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(source), str(target))


def os_compare_trees(expected: Path, actual: Path) -> None:
    left = {
        path.relative_to(expected): path.read_bytes()
        for path in expected.rglob("*")
        if path.is_file()
    }
    right = {
        path.relative_to(actual): path.read_bytes()
        for path in actual.rglob("*")
        if path.is_file()
    }
    if left.keys() != right.keys():
        missing = sorted(str(path) for path in left.keys() - right.keys())
        extra = sorted(str(path) for path in right.keys() - left.keys())
        raise ValueError(f"docs_sdk file mismatch; missing={missing}, extra={extra}")
    changed = sorted(str(path) for path in left if left[path] != right[path])
    if changed:
        raise ValueError(f"docs_sdk content is stale: {changed}")


def str_robot_version(source: str = "") -> str:
    """Import the exported source or installed SDK and report its version."""
    if source:
        sys.path.insert(0, os.path.abspath(source))
    from vmodal_robot import __version__
    return __version__


if __name__ == '__main__':
    fire.Fire({"str_robot_version": str_robot_version})
