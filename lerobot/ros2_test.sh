#!/usr/bin/env bash
help='
Run the existing SDK suite while native ROS 2 Python nodes exchange messages.
Requires ROS 2, demo_nodes_py, pip, rsync and the existing SDK test dependencies.
## Usage:
  bash ros2_test.sh test /tmp/vmodal-ros2-test
'
set -eo pipefail
cd "$(dirname "$0")"

robot_ros2_cleanup() {
  local help='
  ## Usage:
    robot_ros2_cleanup 1234 1235
  '
  local pid
  for pid in "$@"; do
    kill "$pid" 2>/dev/null || true
  done
  for pid in "$@"; do
    wait "$pid" 2>/dev/null || true
  done
}

robot_ros2_test() {
  local help='
  ## Usage:
    bash ros2_test.sh test /tmp/vmodal-ros2-test
  '
  local logs="$1" talker listener demo_dir
  mkdir -p "$logs"
  logs="$(cd "$logs" && pwd)"
  : "${ROS_DISTRO:?Source a ROS 2 installation before running this test}"
  # ROS setup scripts may reference unset variables, so do not enable nounset.
  # shellcheck disable=SC1090,SC1091
  source "/opt/ros/$ROS_DISTRO/setup.bash"
  PYTHONPATH="$(pwd):${PYTHONPATH:-}"
  export PYTHONPATH
  python --version | tee "$logs/python.log"
  # Ubuntu build tools can come from apt without pip RECORD files. Install the
  # test.sh requirements alongside them instead of trying to uninstall apt files.
  # colcon-core requires setuptools<80; this also satisfies the SDK's >=77.
  python -m pip install --ignore-installed \
    'setuptools>=77,<80' 'wheel>=0.45,<1' 'packaging>=24.2,<27' \
    2>&1 | tee "$logs/bootstrap.log"
  if [[ -f ../../sdk_python/pyproject.toml ]]; then
    # Reuse the reference SDK manifest for its complete dependency set, including
    # cryptography, which is also apt-managed in the Jazzy image.
    python -m pip install --ignore-installed --no-build-isolation '../../sdk_python' \
      2>&1 | tee "$logs/reference-sdk-bootstrap.log"
  fi
  ros2 doctor --report > "$logs/ros2-doctor.log" 2>&1
  demo_dir="$(ros2 pkg prefix demo_nodes_py)/lib/demo_nodes_py"

  # Launch the installed Python executables directly so the trap owns their PIDs.
  timeout --kill-after=5s 20m "$demo_dir/talker" > "$logs/talker.log" 2>&1 &
  talker=$!
  timeout --kill-after=5s 20m "$demo_dir/listener" > "$logs/listener.log" 2>&1 &
  listener=$!
  # Expand numeric PIDs now; function-local variables disappear before EXIT.
  # shellcheck disable=SC2064
  trap "robot_ros2_cleanup '$talker' '$listener'" EXIT
  timeout --kill-after=5s 30s ros2 topic echo /chatter std_msgs/msg/String --once \
    > "$logs/chatter-before.log" 2>&1
  grep -F 'Hello World:' "$logs/chatter-before.log"

  # Includes packaging, wheel/public-install checks and the sibling SDK wire test.
  bash test.sh test 2>&1 | tee "$logs/sdk-tests.log"
  python -m twine check dist/* 2>&1 | tee "$logs/package.log"
  python -m pip install --force-reinstall --no-deps dist/*.whl \
    2>&1 | tee "$logs/wheel-install.log"
  (
    cd "$logs"
    # Resolve the installed wheel outside the checkout and its PYTHONPATH.
    env -u PYTHONPATH vmodal-robot --help > cli-help.log 2>&1
    env -u PYTHONPATH vmodal-robot status --spool_dir="$logs/spool" > cli-status.log
    python -m json.tool cli-status.log > /dev/null
  )

  kill -0 "$talker" "$listener"
  timeout --kill-after=5s 30s ros2 topic echo /chatter std_msgs/msg/String --once \
    > "$logs/chatter-after.log" 2>&1
  grep -F 'Hello World:' "$logs/chatter-after.log"
  grep -F 'I heard:' "$logs/listener.log"
  printf 'SDK suite, installed wheel CLI and ROS 2 message exchange passed.\n'
}

cmd="${1:-}"
case "$cmd" in
  test) robot_ros2_test "${2:?Pass an absolute diagnostics directory}" ;;
  *) echo "$help"; exit 1 ;;
esac
