#!/usr/bin/env bash
help='
## Usage:
  bash test.sh test
  bash test.sh package
'
set -euo pipefail
cd "$(dirname "$0")"
robot_root="$(pwd)"
export PYTHONPATH="${robot_root}:${PYTHONPATH:-}"

robot_test() {
  local help='
  ## Usage:
    bash test.sh test
  '
  python3 -m pip install 'setuptools>=68,<77' 'wheel>=0.45,<1' 'packaging>=24.2,<27'
  python3 -m pip install --no-build-isolation -e '.[dev,s3,rosbag]'
  local wire=False
  if [[ -f ../../sdk_python/pyproject.toml ]]; then
    python3 -m pip install -e '../../sdk_python'
    wire=True
  fi
  python3 tests/test_utils.py test_all
  python3 tests/test_lerobot.py test_all
  python3 tests/test_spool.py test_all
  python3 tests/test_runner.py test_all
  python3 tests/test_cli.py test_all
  python3 tests/test_monitoring.py test_all
  python3 tests/test_s3.py test_all
  python3 tests/test_rosbag.py test_all
  python3 tests/test_ros2_manifest.py test_all
  python3 tests/test_vmodal.py test_all --wire="$wire"
  python3 tests/test_search_acceptance.py test_all
  python3 tests/test_measure_cloud_latency.py test_all
  python3 tests/test_temporal.py test_all
  python3 tests/test_dataset.py test_all
  python3 tests/test_markers.py test_all
  python3 tests/test_artifacts.py test_all
}

robot_package() {
  local help='
  ## Usage:
    bash test.sh package
  '
  python3 -m pip install 'setuptools>=68,<77' 'wheel>=0.45,<1' 'packaging>=24.2,<27'
  python3 -m pip install --no-build-isolation -e '.[dev]'
  python3 tests/test_artifacts.py test_all
}

cmd="${1:-test}"
case "$cmd" in
  test) robot_test ;;
  package) robot_package ;;
  *) echo "$help"; exit 1 ;;
esac
