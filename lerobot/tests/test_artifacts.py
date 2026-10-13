"""Check lean wheels and reproducible public source exports.

Usage:
    python tests/test_artifacts.py test_all
"""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning
from src.utils.util_base import os_makedirs, os_path_cleanup

import glob
import re
import subprocess
import tarfile
import zipfile

from vmodal_robot import __version__
from tests.utils_datafake import pd_fake_data, os_fake_no_vmodal


dirtest="ztmp/ztests"


def test1():
    os_path_cleanup("dist")
    os_path_cleanup("build")
    subprocess.run([sys.executable, "-m", "build", "--no-isolation"], check=True)
    wheels = glob.glob("dist/*.whl")
    sdists = glob.glob("dist/*.tar.gz")
    assert len(wheels) == 1
    assert len(sdists) == 1
    with zipfile.ZipFile(wheels[0]) as handle:
        names = handle.namelist()
    assert "vmodal_robot/spool.py" in names
    assert "vmodal_robot/adapters/lerobot.py" in names
    assert "vmodal_robot/transports/vmodal.py" in names
    assert "vmodal_robot/reconciliation/temporal.py" in names
    assert "vmodal_robot/reconciliation/dataset.py" in names
    assert "vmodal_robot/markers.py" in names
    assert not any(name.startswith("tests/") for name in names)
    metadata_name = [name for name in names if name.endswith(".dist-info/METADATA")][0]
    with zipfile.ZipFile(wheels[0]) as handle:
        metadata = handle.read(metadata_name).decode("utf-8").lower()
    for name in ("torch", "pandas", "pyarrow", "rclpy", "opencv", "gstreamer"):
        assert f"requires-dist: {name}" not in metadata


def test2():
    wheel = glob.glob("dist/*.whl")[0]
    target = os.path.abspath(os.path.join(dirtest, __version__, "wheel_install"))
    os_path_cleanup(target)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--target", target, wheel], check=True)
    code = ("import vmodal_robot; import vmodal_robot.markers; "
            "import vmodal_robot.reconciliation.temporal; import vmodal_robot.reconciliation.dataset; "
            "print(vmodal_robot.__version__)")
    output = subprocess.check_output([sys.executable, "-c", code], cwd=target,
                                     env=dict(os.environ, PYTHONPATH=target), text=True)
    assert output.strip() == __version__


def test3():
    text = open("pyproject.toml", "r", encoding="utf-8").read()
    assert 'dependencies = ["fire>=0.6.0,<1"]' in text
    assert 'vmodal = ["vmodal>=1.0.0,<2"]' in text
    assert text.count('"packaging>=24.2,<27"') == 2
    for name in ("torch", "pandas", "pyarrow", "rclpy", "opencv", "gstreamer"):
        assert name not in text.lower()


def test4():
    sdist = glob.glob("dist/*.tar.gz")[0]
    with tarfile.open(sdist) as handle:
        names = handle.getnames()
    assert any(name.endswith("/README.md") for name in names)
    assert any(name.endswith("/LICENSE") for name in names)
    assert any(name.endswith("/tests/test_temporal.py") for name in names)
    assert any(name.endswith("/tests/test_dataset.py") for name in names)
    assert any(name.endswith("/tests/test_markers.py") for name in names)
    assert any(name.endswith("/test.sh") for name in names)
    assert not any("/ztmp/" in name for name in names)
    for path in ("docs/transport_contract.md", "docs/search_qualification.md", "docs/search_profile.json",
                 "examples/search_acceptance.py", "examples/measure_cloud_latency.py",
                 "examples/researcher_workflow.py", "docs/researcher_workflow.md"):
        assert any(name.endswith("/" + path) for name in names), path


def test5():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    path = os.path.join(root, ".github", "workflows", "sdk_robot_test_release.yml")
    if not os.path.isfile(path):
        log_info("Public standalone source: workflow gate is checked in the source monorepo")
        return
    text = open(path, "r", encoding="utf-8").read()
    assert "uinterface/sdk_ros_robot/**" in text
    assert "v-modal/vmodal_sdk_robotics" in text
    assert "needs: [secret_detection, offline_test, package]" in text
    assert "public_release_filter.txt" in text
    assert "git push --atomic" in text
    assert "trufflehog" in text
    pub = text.split("\n  publish_sdk_robot:\n", 1)[1].split("\n  sdk_docs_artifact:\n", 1)[0]
    before_install = pub.split("- name: Verify clean public installation", 1)[0]
    assert "uses: actions/setup-python@" in before_install
    assert 'python-version: "3.11"' in before_install
    assert "'packaging>=24.2,<27'" in pub


def test6():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    target = os.path.abspath(os.path.join(dirtest, __version__, "public_export"))
    os_path_cleanup(target)
    os_makedirs(target)
    subprocess.run(
        [
            "rsync", "-a", "--delete", "--delete-excluded", "--prune-empty-dirs",
            f"--filter=merge {os.path.join(root, 'public_release_filter.txt')}",
            root + os.sep, target + os.sep,
        ],
        check=True,
    )
    assert os.path.isfile(os.path.join(target, "src", "vmodal_robot", "spool.py"))
    assert os.path.isfile(os.path.join(target, "tests", "test_temporal.py"))
    assert os.path.isfile(os.path.join(target, "tests", "test_dataset.py"))
    assert os.path.isfile(os.path.join(target, "tests", "test_markers.py"))
    assert not os.path.exists(os.path.join(target, "env.sh"))
    for name in ("ztmp", "ztmp_sandbox", "ztmptest", "build", "dist"):
        assert not os.path.exists(os.path.join(target, name)), name
    paths = ("docs/transport_contract.md", "docs/search_qualification.md", "docs/search_profile.json",
             "examples/search_acceptance.py", "examples/measure_cloud_latency.py",
             "examples/researcher_workflow.py", "docs/researcher_workflow.md",
             "test.sh", "docs.py", "docs_sdk/README.md", "public_release_filter.txt")
    for name in paths:
        assert os.path.isfile(os.path.join(target, name)), name
    for name in ("README.md", "docs/transport_contract.md", "docs/search_qualification.md"):
        path = os.path.join(target, name)
        text = open(path, "r", encoding="utf-8").read()
        for link in re.findall(r"\]\(([^)]+)\)", text):
            if "://" in link or link.startswith("#"):
                continue
            local = os.path.normpath(os.path.join(os.path.dirname(path), link.split("#")[0]))
            assert os.path.isfile(local), (name, link)
    readme = open(os.path.join(target, "README.md"), "r", encoding="utf-8").read()
    assert "cd vmodal_sdk_robotics/lerobot\npython -m pip install ." in readme
    assert "bash test.sh test" in readme
    assert "cd vmodal_sdk_robotics\nbash test.sh test" not in readme
    assert "fail capability validation before creating a spool" in readme
    assert "unlimited attempts by default" in readme
    install = os.path.abspath(os.path.join(dirtest, __version__, "public_install"))
    os_path_cleanup(install)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--no-build-isolation",
                    "--target", install, target], check=True)
    env = dict(os.environ, PYTHONPATH=install)
    output = subprocess.check_output(
        [sys.executable, "-c", "import vmodal_robot; print(vmodal_robot.__version__)"],
        cwd=install,
        env=env,
        text=True,
    )
    assert output.strip() == __version__
    subprocess.run([sys.executable, "-c", "from vmodal_robot.cli import RobotCli; import fire; fire.Fire(RobotCli())",
                    "--help"], cwd=install, env=env, check=True, stdout=subprocess.DEVNULL)
    for name in ("search_acceptance.py", "measure_cloud_latency.py", "researcher_workflow.py"):
        subprocess.run([sys.executable, os.path.join(target, "examples", name), "--help"],
                       cwd=install, env=env, check=True, stdout=subprocess.DEVNULL)


def test7():
    """An installed developer SDK cannot hide optional imports in the public core."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    block = os_fake_no_vmodal(os.path.abspath(os.path.join(dirtest, __version__, "no_sdk")))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((block, root, os.path.join(root, "src"))))
    probe = subprocess.run([sys.executable, "-c", "import vmodal"], cwd=root,
                           env=env, text=True, capture_output=True)
    assert probe.returncode != 0
    assert "optional vmodal SDK import blocked" in probe.stderr
    subprocess.run([sys.executable, os.path.join(root, "tests", "test_vmodal.py"),
                    "test_all", "--wire=False"], cwd=root, env=env, check=True)
    required = subprocess.run([sys.executable, os.path.join(root, "tests", "test_vmodal.py"),
                               "test_all", "--wire=True"], cwd=root, env=env,
                              text=True, capture_output=True)
    assert required.returncode != 0
    assert "optional vmodal SDK import blocked" in required.stderr


def test_all():
    os_makedirs(dirtest)
    test1()
    test2()
    test3()
    test4()
    test5()
    test6()
    test7()
    log_info("test_artifacts: all passed")


if __name__ == '__main__':
    import fire
    fire.Fire()
