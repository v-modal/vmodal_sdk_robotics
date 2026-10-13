"""Generate and verify the public Robotics SDK API reference.

Usage:
    python docs.py generate
    python docs.py check
"""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

import tempfile
from pathlib import Path

from utils import (
    os_compare_trees,
    os_read_text,
    os_replace_tree,
    os_run,
    os_text_files,
    os_write_text,
)


root = Path(__file__).resolve().parent
dest = root / "docs_sdk"

modules = (
    "vmodal_robot",
    "vmodal_robot.contracts",
    "vmodal_robot.runner",
    "vmodal_robot.spool",
    "vmodal_robot.adapters.lerobot",
    "vmodal_robot.transports.vmodal",
    "vmodal_robot.cli",
    "vmodal_robot.monitoring",
    "vmodal_robot.markers",
    "vmodal_robot.reconciliation.temporal",
    "vmodal_robot.reconciliation.dataset",
)

required_files = (
    "README.md",
    ".nojekyll",
    "index.html",
    "vmodal_robot.html",
    "vmodal_robot/contracts.html",
    "vmodal_robot/runner.html",
    "vmodal_robot/spool.html",
    "vmodal_robot/adapters/lerobot.html",
    "vmodal_robot/transports/vmodal.html",
    "vmodal_robot/cli.html",
    "vmodal_robot/monitoring.html",
    "vmodal_robot/markers.html",
    "vmodal_robot/reconciliation/temporal.html",
    "vmodal_robot/reconciliation/dataset.html",
)

required_symbols = (
    "ArtifactInput",
    "RevisionInput",
    "DatasetRevision",
    "Receipt",
    "Adapter",
    "Transport",
    "RunnerConfig",
    "Runner",
    "SpoolConfig",
    "Spool",
    "LeRobotAdapter",
    "VmodalTransport",
    "RobotCli",
)

forbidden_terms = (
    'class="view-source-toggle-state"',
    "TEST_CLIENT_CLERK_USER_API_TOKEN",
    "TEST_CLIENT_USER_ID",
    "ztmp/env_gitignore.sh",
)


def os_validate_site(site: Path) -> None:
    for name in required_files:
        if not (site / name).is_file():
            raise ValueError(f"generated SDK reference is missing: {name}")

    rows = os_text_files(site)
    joined = "\n".join(value for _, value in rows)
    for symbol in required_symbols:
        if symbol not in joined:
            raise ValueError(f"public SDK symbol is missing from pdoc output: {symbol}")

    forbidden = forbidden_terms + (str(root), str(Path.home()))
    for path, value in rows:
        lower = value.lower()
        for item in forbidden:
            if item.lower() in lower:
                raise ValueError(f"forbidden documentation content {item!r} found in {path}")


def os_build_site(site: Path) -> None:
    args = [
        sys.executable,
        "-m",
        "pdoc",
        "--no-search",
        "--no-show-source",
        "--footer-text",
        "V-Modal Robotics SDK",
        "--output-directory",
        str(site),
        *modules,
    ]
    os_run(args, root)
    os_write_text(site / "README.md", os_read_text(dest / "README.md"))
    os_write_text(site / ".nojekyll", "")
    for path, value in os_text_files(site):
        # Normalize pdoc's trailing spaces and use one stable final newline.
        text = "\n".join(line.rstrip() for line in value.splitlines()).rstrip("\n")
        os_write_text(site / path, text + "\n" if value else "")
    os_validate_site(site)


def generate() -> str:
    with tempfile.TemporaryDirectory(prefix=".docs-sdk-", dir=root) as name:
        site = Path(name) / "site"
        os_build_site(site)
        os_replace_tree(site, dest, "docs_sdk")
    message = f"generated Robotics SDK reference in {dest}"
    log_info(message)
    return message


def check() -> str:
    if not dest.is_dir():
        raise ValueError("docs_sdk is missing; run docs.py generate")
    os_validate_site(dest)
    with tempfile.TemporaryDirectory(prefix=".docs-sdk-check-", dir=root) as name:
        site = Path(name) / "site"
        os_build_site(site)
        os_compare_trees(dest, site)
    return "Robotics SDK reference is current and safe to publish"


if __name__ == "__main__":
    fire.Fire({"generate": generate, "check": check})
