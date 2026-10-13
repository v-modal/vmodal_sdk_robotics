#!/usr/bin/env bash
help='
## Usage:
  bash cli.sh run --ready_dir=/data/ready --spool_dir=/var/lib/vmodal-robot
  bash cli.sh status --spool_dir=/var/lib/vmodal-robot
  bash cli.sh flush --spool_dir=/var/lib/vmodal-robot --deadline_seconds=120
'
set -euo pipefail
cd "$(dirname "$0")"

robot_cli() {
  local help='
  ## Usage:
    bash cli.sh status --spool_dir=/var/lib/vmodal-robot
  '
  : "$help"
  python -m vmodal_robot.cli "$@"
}

robot_cli "$@"
