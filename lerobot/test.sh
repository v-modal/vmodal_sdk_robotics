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
  python -m pip install 'setuptools>=77,<85' 'wheel>=0.45,<1' 'packaging>=24.2,<27'
  python -m pip install --no-build-isolation -e '.[dev]'
  local wire=False
  if [[ -f ../../sdk_python/pyproject.toml ]]; then
    python -m pip install --no-build-isolation -e '../../sdk_python'
    wire=True
  fi
  python tests/test_utils.py test_all
  python tests/test_lerobot.py test_all
  python tests/test_spool.py test_all
  python tests/test_runner.py test_all
  python tests/test_cli.py test_all
  python tests/test_vmodal.py test_all --wire="$wire"
  python tests/test_search_acceptance.py test_all
  python tests/test_measure_cloud_latency.py test_all
  python tests/test_temporal.py test_all
  python tests/test_dataset.py test_all
  python tests/test_markers.py test_all
  python tests/test_artifacts.py test_all
}

robot_package() {
  local help='
  ## Usage:
    bash test.sh package
  '
  python -m pip install 'setuptools>=77,<85' 'wheel>=0.45,<1' 'packaging>=24.2,<27'
  python -m pip install --no-build-isolation -e '.[dev]'
  python tests/test_artifacts.py test_all
}

cmd="${1:-test}"
case "$cmd" in
  test) robot_test ;;
  package) robot_package ;;
  *) echo "$help"; exit 1 ;;
esac
