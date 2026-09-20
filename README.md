# Robotics SDK reference publication

The generated V-Modal Robotics SDK class and method reference is published at:

**https://v-modal.github.io/vmodal_sdk_robotics/**

The site is hosted by GitHub Pages from the `gh-pages` branch of
`v-modal/vmodal_sdk_robotics`. Its source is the public Python API in
`uinterface/sdk_ros_robot/lerobot/src/vmodal_robot/`; the generated site is
stored in this `docs_sdk/` directory. Do not edit generated HTML by hand. This
README is the maintainer note that `docs.py` preserves during regeneration.

## Publish the SDK reference

Publication is handled by `.github/workflows/sdk_robot_test_release.yml`. To
publish only the reference from the monorepo root, run:

```bash
gh workflow run .github/workflows/sdk_robot_test_release.yml \
  -f publish_sdk_robot=false \
  -f publish_sdk_docs_only=true
```

The workflow generates and validates the site, uploads it as an immutable
workflow artifact, replaces the public repository's `gh-pages` branch, enables
branch-based GitHub Pages, and verifies the deployed `RELEASE_SHA`. A normal
SDK release also publishes the reference when `publish_sdk_robot=true`.

The workflow requires its configured `GH_TOKEN` secret to write the public
repository and configure GitHub Pages. Publication is complete only after the
`publish_sdk_docs` job passes its deployed SHA check.

## Generate and inspect locally

From `uinterface/sdk_ros_robot/lerobot`:

```bash
python -m pip install -e '.[docs]'
python docs.py generate
python docs.py check
python -m http.server 8000 --directory docs_sdk
```

Then open `http://localhost:8000/`. Commit public API docstrings, generator
changes, this README, and regenerated `docs_sdk/` output together.
