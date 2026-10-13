# Robotics SDK release helper

`.github/workflows/sdk_robot_test_release.yml` calls the reusable functions in
`uinterface/sdk_ros_robot/lerobot/release.sh`. GitHub Actions owns the checkouts,
Python matrix, job gates, credentials, environments and artifact transfers.
The helper owns the Bash implementation of each check and publication step.

From the source repository root:

```bash
bash uinterface/sdk_ros_robot/lerobot/release.sh --help
bash uinterface/sdk_ros_robot/lerobot/release.sh robot_release_test
bash uinterface/sdk_ros_robot/lerobot/release.sh robot_release_build
bash uinterface/sdk_ros_robot/lerobot/release.sh robot_release_docs_build
```

Functions can also be sourced. The script enables `set -euo pipefail`; sourcing
defines functions without dispatching a command or changing the working directory.
Package and documentation checks resolve paths from the script location.

```bash
source uinterface/sdk_ros_robot/lerobot/release.sh
robot_release_docs_check /tmp/sdk-docs-site
```

The source workflow retains its secret-detection gate, offline Python tests,
package validation and public-installation check before publishing source.
Documentation-only releases retain their secret gate and artifact validation.
Refactoring the helper does not change workflow inputs, job dependencies,
publication conditions, artifact names, tag fallback or atomic push behavior.

Export, verification, version lookup and source publication accept a public
checkout path as their first argument. They default to
`$GITHUB_WORKSPACE/vmodal_sdk_robotics`, or a checkout of that name under the
source repository when running locally. Export requires the source checkout to
match `RELEASE_SHA` and a separate destination containing `.git` before replacing files.
The workflow supplies the existing release variables and pinned scanner settings;
each function's usage block describes its required environment.

`robot_release_credentials` reuses `.github/workflows/utils.sh` to load `GH_TOKEN`
and write it to `GITHUB_ENV`. `robot_release_version` imports the public SDK through
the checked-in `utils.py` command, prints its version and writes `SDK_VERSION` to
`GITHUB_ENV` when available. The public-installation check uses the same command
outside the source tree to verify the installed package imports successfully.

This helper belongs to the source monorepo and is excluded by the public export
filter. The public repository continues to use its standalone test scripts and
simulation workflow. Use the source workflow's manual inputs for publication;
running local checks does not publish anything.

After committing and pushing the desired source snapshot, publish through the
source workflow rather than pushing the public repository directly:

```bash
gh workflow run .github/workflows/sdk_robot_test_release.yml \
  --repo arita37/vmx_mobile --ref dev \
  -f publish_sdk_robot=true -f publish_sdk_docs_only=false
```

The workflow publishes its committed source snapshot. Keep source SDK changes
in sync with fixes already published in the public repository before dispatching.
