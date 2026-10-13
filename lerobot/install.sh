#!/usr/bin/env bash
help='
## Usage:
  bash install.sh install
  bash install.sh cloud
'
set -euo pipefail
cd "$(dirname "$0")"

robot_install() {
  local help='
  ## Usage:
    bash install.sh install
  '
  python -m pip install 'setuptools>=77,<85' 'wheel>=0.45,<1' 'packaging>=24.2,<27'
  python -m pip install --no-build-isolation -e .
}

robot_install_cloud() {
  local help='
  ## Usage:
    bash install.sh cloud
  '
  python -m pip install 'setuptools>=77,<85' 'wheel>=0.45,<1' 'packaging>=24.2,<27'
  python -m pip install --no-build-isolation -e '.[vmodal]'
}

cmd="${1:-install}"
case "$cmd" in
  install) robot_install ;;
  cloud) robot_install_cloud ;;
  *) echo "$help"; exit 1 ;;
esac
