# Reported fixes and validation

Date: 2026-10-13. Package: 0.2.1. Work and tests use the public repository
`v-modal/vmodal_sdk_robotics`, not the private monorepo's SDK checkout.

| Issue | Resolution / evidence |
| --- | --- |
| 1. 100-manifest ceiling | Preserve existing fair filtered batching; existing tests admit 250 manifests and retry deferred work |
| 2. 100 MiB video ceiling | Local default is 1 GiB; real 101 MiB spool admission/hash test; exact backend boundaries remain enforced |
| 3. SIGTERM drain budget | Preserve one absolute deadline through recovery, delivery, publication and client close; actual subprocess signal tests across phases |
| 4. Spool reclamation | Preserve automatic verified ACK + all referencing publication + retention checks; shared/unverified/pending payload tests |
| 5. Missing storage integration | Worked async S3/MinIO Transport implementation, all native kinds, remote SHA-256, conditional idempotency, descriptor/revision publication, reconciliation and fault tests |
| 6. ROS 2 manifest node | Dedicated native bag adapter/node, durable completion journal and recorder wrapper; actual closed `/chatter` bag notification/admission and SIGTERM wrapper tests |
| 7. README install untested | Public workflow runs exact `python3 -m pip install .` before bootstrap, verifies distro pip version, installed distribution and CLI outside checkout |
| 8. python command | Entry scripts and both READMEs use python3; CI no longer creates a python symlink |
| 9. top-level src package | Wheel publishes only vmodal_robot namespaces; logging/base helpers move to qualified _support; installed wheel import tests forbid src entries |
| 10. CLI names / leaked imports | Stable vmodal-robot name and explicit Fire components in examples; clean-install help assertions |
| 11. Fixture glob warnings | MANIFEST.in includes actual Python/shell/Dockerfile sources; build test rejects missing-file glob warnings |
| 12. Spool provisioning | Document relative fallback, absolute persistent overrides and service-user /var/lib provisioning commands |

The public baseline already fixed issues 1, 3 and 4. This change retains their
implementation and expands the surrounding integration/installation coverage.

## Local public-checkout evidence

- Fresh Ubuntu 22.04 / Python 3.10 container: distro pip 22.0.2, no build-tool
  upgrades, build isolation enabled, `python3 -m pip install .`, installed name
  `vmodal-robotics`, version 0.2.1, CLI help outside checkout: passed.
- `bash test.sh test`: passed. Includes simple assertions/faults, SIGTERM phase
  checks, 250-manifest discovery, large local admission, migration/reclamation,
  wheel/source builds, lean imports and example help.
- Humble native `bash ros2_test.sh test /tmp/vmodal-ros-final`: passed, including
  real bag recording, actual manifest-node event, recorder SIGTERM wrapper,
  installed-wheel CLI and ROS traffic before/after the full suite.
- Actual MinIO built from upstream release source at commit
  `0d7408fc9969caf07de6a8c3a84f9fbb10a6739e`: all four native kinds, repeated
  delivery/publication, downloaded-byte verification, descriptors and ACK-gated
  cleanup round trip passed.
- Secret-detection gate and shell syntax / git whitespace checks: passed.
- Session-scoped data-contract/import/wiring audit: no outstanding findings.

## Install diagnosis

Compatible setuptools metadata lives in `setup.cfg` with static package name and
dynamic version. Fresh-container reproduction showed an additional Ubuntu pip
isolation/backend-path failure: restricting setuptools and replacing PEP 639
alone still silently built an empty UNKNOWN wheel. Distro setuptools understands
setup.cfg, so the documented isolated install now succeeds with the original pip.
The upstream [setuptools report](https://github.com/pypa/setuptools/issues/3269)
describes this UNKNOWN-wheel failure. CI checks actual installed behavior rather
than just pip's exit status. Source-only `src.utils` test helpers remain outside
the wheel; production runtime imports use the qualified support package.

## Deployment limits

S3 is a worked storage example, not universal provider qualification or a search
backend. Validate conditional writes/durability and keep its prefix exclusively
SDK-owned; lifecycle deletion invalidates receipts. The reference V-Modal SDK's
separate 100 MiB upload cap and unavailable stock generic backend remain explicit.
The example bypasses that upload API using ordinary object storage.

ROS bags require explicit successful closure and immutable directories. Durable
journals cover node downtime; completion hints alone cannot survive a recorder
crash before it journals. Supervisor hard stops remain necessary for nonreturning
kernel I/O. SQLite deduplication history grows after payload reclamation.

Before the private monorepo exporter publishes another SDK version, import these
public changes into its source mirror so that exporter does not overwrite them.
