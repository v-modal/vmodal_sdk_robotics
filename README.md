# V-Modal Robotics SDKs

This directory contains the V-Modal robotics integrations published to the
public [vmodal_sdk_robotics](https://github.com/v-modal/vmodal_sdk_robotics)
repository. Each integration is kept in its own subdirectory so its runtime,
dependencies, and documentation remain independent.

## Sub-repositories

| Sub-repository | Status | Details |
| --- | --- | --- |
| [LeRobot SDK](https://github.com/v-modal/vmodal_sdk_robotics/tree/main/lerobot) | Implemented | A lightweight, crash-resilient uploader for finalized LeRobot v3 datasets. It validates immutable handoff manifests, stores artifacts in a durable SQLite-backed spool, uploads video and auxiliary data independently, and resumes safely after restarts. The directory includes the installable `vmodal-robotics` Python package, CLI, API documentation source, and integration guide. |
| [Google Intrinsic](https://github.com/v-modal/vmodal_sdk_robotics/tree/main/google_intrisinc) | Architecture guide | A detailed design for connecting Intrinsic Core and ROS 2 robotics deployments to V-Modal. It covers camera and sensor capture, synchronized telemetry, task and transform provenance, asynchronous upload boundaries, and a proposed production rollout. The connector is documented but not yet implemented. |

## Repository layout

```text
vmodal_sdk_robotics/
├── README.md
├── lerobot/
│   ├── src/vmodal_robot/
│   ├── docs/
│   ├── pyproject.toml
│   └── README.md
└── google_intrisinc/
    └── readme.md
```

## LeRobot quick start

Install the tagged LeRobot package directly from its subdirectory:

```bash
python -m pip install \
  "vmodal-robotics[vmodal] @ git+https://github.com/v-modal/vmodal_sdk_robotics.git@v0.1.0#subdirectory=lerobot"
```

The LeRobot integration requires Python 3.10 through 3.13. See its
[README](https://github.com/v-modal/vmodal_sdk_robotics/tree/main/lerobot#readme)
for the handoff contract, configuration, CLI commands, durability guarantees,
and transport limitations.

## Documentation and releases

The generated [Robotics SDK API reference](https://v-modal.github.io/vmodal_sdk_robotics/)
documents the LeRobot Python package. Public releases are generated from the
tested source in this directory; `RELEASE_METADATA` in the public repository
records the exact source commit used for each publication.
