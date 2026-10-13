from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning
from src.utils.util_base import os_makedirs, os_path_cleanup

import json
from unittest.mock import patch

from vmodal_robot.adapters.lerobot import LeRobotAdapter
from tests.utils_datafake import pd_fake_data


dirtest="ztmp/ztests"


def test1():
    data = pd_fake_data("adapter_valid")
    adapter = LeRobotAdapter(data["ready"])
    items = adapter.discover()
    assert len(items) == 1
    item = items[0]
    assert item.source_version == "v3"
    assert item.complete is True
    assert len(item.artifacts) == 5
    assert [value.kind for value in item.artifacts].count("video") == 2
    assert [value.kind for value in item.artifacts].count("telemetry") == 1
    assert item.artifacts[0].source_refs["episodes"] == [0, 1]
    adapter.admission_feedback(item, "accepted")
    assert adapter.discover() == []


def test2():
    data = pd_fake_data("adapter_version", version="v2")
    adapter = LeRobotAdapter(data["ready"])
    assert adapter.discover() == []
    errors = adapter.drain_errors()
    assert len(errors) == 1
    assert "only LeRobot dataset v3" in errors[0][1]


def test3():
    data = pd_fake_data("adapter_escape")
    manifest = data["data"]
    manifest["artifacts"][0]["path"] = "../outside.mp4"
    with open(data["manifest"], "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    adapter = LeRobotAdapter(data["ready"])
    assert adapter.discover() == []
    assert "path must be relative" in adapter.drain_errors()[0][1]


def test4():
    data = pd_fake_data("adapter_no_metadata")
    manifest = data["data"]
    manifest["artifacts"] = [value for value in manifest["artifacts"] if value["kind"] != "metadata"]
    with open(data["manifest"], "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    adapter = LeRobotAdapter(data["ready"])
    assert adapter.discover() == []
    assert "metadata snapshot" in adapter.drain_errors()[0][1]


def test5():
    data = pd_fake_data("adapter_retained")
    os.remove(data["manifest"])
    for i in range(250):
        value = dict(data["data"], source_revision=f"revision-{i:03d}")
        path = os.path.join(data["ready"], f"revision-{i:03d}.ready.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
    adapter = LeRobotAdapter(data["ready"])
    assert adapter.discover(0) == []
    assert adapter.discover(-1) == []
    assert adapter.discovery_status()["unseen"] == 250
    revisions = []
    for size in (100, 100, 50):
        items = adapter.discover(100)
        assert len(items) == size
        status = adapter.discovery_status()
        assert status["manifests_present"] == 250
        assert status["pending"] == size
        assert status["unseen"] == 250 - len(revisions) - size
        for item in items:
            adapter.admission_feedback(item, "accepted")
            revisions.append(item.source_revision)
    assert len(set(revisions)) == 250
    assert adapter.discover() == []
    assert adapter.discovery_status()["admitted"] == 250
    path = os.path.join(data["ready"], "000-new.ready.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(dict(data["data"], source_revision="new"), handle)
    assert adapter.discover(1)[0].source_revision == "new"
    assert len(LeRobotAdapter(data["ready"]).discover(300)) == 251


def test6():
    data = pd_fake_data("adapter_deferred")
    for i in range(3):
        path = os.path.join(data["ready"], f"later-{i}.ready.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(dict(data["data"], source_revision=f"later-{i}"), handle)
    adapter = LeRobotAdapter(data["ready"])
    first = adapter.discover(1)[0]
    adapter.admission_feedback(first, "deferred")
    assert adapter.discovery_status()["deferred"] == 1
    found = []
    for _ in range(4):
        item = adapter.discover(1)[0]
        found.append(item.source_revision)
        adapter.admission_feedback(item, "accepted")
    assert len(set(found)) == 4
    assert first.source_revision in found
    assert adapter.discover() == []
    assert adapter.discovery_status()["deferred"] == 0
    assert adapter.discovery_status()["admitted"] == 4


def test7():
    data = pd_fake_data("adapter_corrected", version="v2")
    adapter = LeRobotAdapter(data["ready"])
    assert adapter.discover() == []
    assert len(adapter.drain_errors()) == 1
    assert adapter.discover() == []
    assert adapter.drain_errors() == []
    assert adapter.discovery_status()["rejected"] == 1
    with open(data["manifest"], "w", encoding="utf-8") as handle:
        json.dump(dict(data["data"], source_version="v3", source_revision="corrected"), handle)
    assert adapter.discovery_status()["unseen"] == 1
    item = adapter.discover()[0]
    adapter.admission_feedback(item, "rejected")
    assert adapter.discovery_status()["rejected"] == 1
    assert adapter.discover() == []
    with open(data["manifest"], "w", encoding="utf-8") as handle:
        json.dump(dict(data["data"], source_version="v3", source_revision="corrected-again"), handle)
    assert adapter.discover()[0].source_revision == "corrected-again"


def test8():
    data = pd_fake_data("adapter_races")
    missing = os.path.join(data["ready"], "000-removed.ready.json")
    adapter = LeRobotAdapter(data["ready"])
    with patch("vmodal_robot.adapters.lerobot.glob.glob", return_value=[missing, data["manifest"]]):
        items = adapter.discover(1)
        assert len(items) == 1
        assert adapter.drain_errors()[0][0] == missing
        adapter.admission_feedback(items[0], "accepted")
        assert adapter.discover() == []
        assert adapter.drain_errors() == []
    adapter = LeRobotAdapter(data["ready"])
    with patch.object(adapter, "os_load_manifest", side_effect=FileNotFoundError("removed during load")):
        assert adapter.discover() == []
    assert "removed during load" in adapter.drain_errors()[0][1]
    assert len(adapter.discover()) == 1


def test9():
    data = pd_fake_data("adapter_mixed")
    with open(os.path.join(data["ready"], "000-invalid.ready.json"), "w", encoding="utf-8") as handle:
        json.dump([], handle)
    adapter = LeRobotAdapter(data["ready"])
    assert len(adapter.discover(1)) == 1
    assert "JSON object required" in adapter.drain_errors()[0][1]


def test_all():
    os_makedirs(dirtest)
    test1()
    test2()
    test3()
    test4()
    test5()
    test6()
    test7()
    test8()
    test9()
    log_info("test_lerobot: all passed")


if __name__ == '__main__':
    import fire
    fire.Fire()
