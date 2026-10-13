# Linux ROS 2 compatibility CI

`.github/workflows/sdk_python_test_simul_release.yml` tests this SDK in official
ROS containers: Humble on Ubuntu 22.04 with system Python 3.10, and Jazzy on
Ubuntu 24.04 with system Python 3.12. Using the system interpreter preserves the
ABI required by the installed `rclpy` extension.

The workflow runs manually, on matching pull requests, and on matching pushes to
`dev`. It includes reference Python SDK changes because `bash test.sh test`
also exercises that sibling SDK's wire contract.

The public repository has a standalone version at
`.github/workflows/sdk_robot_test_simul_release.yml`, maintained in the source
SDK's `.github/workflows/` directory. The release workflow exports that directory
to the public repository root and verifies that the workflow matches its source.
It uses `WORKDIR: lerobot` and runs manually, on matching pull requests, and on
matching pushes to `main`. It requires no private repository or cloud credentials;
the reference Python SDK wire test is omitted when the sibling SDK is absent.

Run the public tests from GitHub's Actions tab or with:

```bash
gh workflow run sdk_robot_test_simul_release.yml \
  --repo v-modal/vmodal_sdk_robotics --ref main
```

`ros2_test.sh` starts real ROS Python talker/listener nodes, requires a received
message within 30 seconds, runs the existing SDK test suite, validates package
metadata, installs the built wheel, and checks its CLI outside the source tree.
It then requires another fresh ROS message and confirms the listener received
data. Node processes are stopped on success or failure. Logs are uploaded even
when testing fails; distributions are uploaded only after all checks pass.

On an existing Linux ROS installation with `demo_nodes_py`, `python3` pointing to
the ROS system interpreter, pip and rsync installed, run from `lerobot/`:

```bash
bash ros2_test.sh test /tmp/vmodal-ros2-test
```

The workflow installs dependencies into disposable containers without creating
virtualenvs. Its step-local `PIP_BREAK_SYSTEM_PACKAGES=1` permits this on Ubuntu
24.04; it is not SDK runtime configuration.
The helper installs the required setuptools, wheel and packaging build tools
alongside Ubuntu's apt-managed versions, which pip cannot uninstall reliably.
Setuptools is restricted to `>=68,<77`; compatible `setup.cfg` metadata also
works when stock Ubuntu 22.04 pip loads distro setuptools during isolation.
The workflow runs `python3 -m pip install .` before bootstrap, checks the installed
distribution name, and runs its CLI outside the checkout. No `python` symlink is
created. This catches both rejected names and silently empty UNKNOWN wheels.
When the sibling Python SDK exists, its manifest also supplies the dependency
bootstrap, including cryptography, without uninstalling apt-managed packages.

The native integration additionally records `/chatter` using ros2 bag, closes it,
notifies the actual manifest node, and admits the native bag through RosbagAdapter.
LeRobot assertion tests use synthetic artifacts and fake receivers. A separate
job verifies the worked S3 transport against actual MinIO. These checks do not
establish robot hardware, Gazebo, every storage provider or search readiness.
All public jobs depend on a secret-detection gate.
