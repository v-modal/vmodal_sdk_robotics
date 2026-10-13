"""Run an offline researcher acceptance fixture with an independent mock archive.

# Usage:
    python examples/researcher_workflow.py run --name=researcher_demo

Fake media/native scanner inputs verify contracts, never real LeRobot or cloud
qualification. Run from a source checkout containing tests/ and its mock data.
"""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

from vmodal_robot.reconciliation.dataset import dataset_prepare, dataset_ready, dataset_recover
from src.utils.util_base import os_path_cleanup


def run(name="researcher_demo"):
    """Delete producer inputs, recover exact original bytes, report independent gates."""
    from tests.utils_dataset_fake import pd_fake_dataset, pd_fake_archive
    data = pd_fake_dataset(name)
    prepared, handoff = dataset_prepare(data["dataset"], data["item"], data["native"], data["bundles"], data["paths"])
    dataset_ready(data["dataset"], prepared, os.path.join(data["ready"], "researcher.ready.json"))
    archive = pd_fake_archive(prepared, handoff)
    os_path_cleanup(data["dataset"])
    os_path_cleanup(data["ready"])
    target = os.path.join(data["root"], "workstation-staging")
    result = dataset_recover(archive["reader"], archive["descriptor_ref"], archive["digest"], target)
    assert result["archive_valid"] and result["alignment_valid"]
    log_info("Offline mock recovery passed after producer removal")
    return {key: result[key] for key in ("root", "descriptor_sha256", "archive_valid", "alignment_valid", "training_loadable", "search_resolvable", "pending")}


if __name__ == "__main__":
    fire.Fire()
