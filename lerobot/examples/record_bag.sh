#!/usr/bin/env bash
help='
Record a bag and durably journal completion after the recorder exits successfully.
Source ROS before use. Paths must be persistent and absolute.
## Usage:
  bash examples/record_bag.sh record /data/bags/run-001 /data/closed /chatter
'
set -euo pipefail

robot_record() {
  local help='
  ## Usage:
    bash examples/record_bag.sh record /data/bags/run-001 /data/closed /chatter
  '
  local bag_dir="$1" closed_dir="$2" pid result=0
  shift 2
  [[ "$bag_dir" = /* && "$closed_dir" = /* ]] || { echo "$help"; return 1; }
  # Bash background jobs inherit ignored SIGINT; Jazzy honors that disposition.
  # Reset it before exec so forwarded termination actually closes the recorder.
  env --default-signal=INT,TERM ros2 bag record -o "$bag_dir" "$@" &
  pid=$!
  trap 'kill -INT "$pid" 2>/dev/null || true' INT TERM
  while true; do
    if wait "$pid"; then result=0; else result=$?; fi
    if ! kill -0 "$pid" 2>/dev/null; then break; fi
  done
  trap - INT TERM
  [[ "$result" = 0 ]] || return "$result"
  python3 -m vmodal_robot.ros2_manifest notify --bag_dir="$bag_dir" --closed_dir="$closed_dir"
}

case "${1:-}" in
  record) shift; robot_record "$@" ;;
  *) echo "$help"; exit 1 ;;
esac
