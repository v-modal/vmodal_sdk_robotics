# Critical robotics SDK issues and fixes

Audit date: 2026-10-13. Scope: `uinterface/sdk_ros_robot/`, with executable
runtime code in `lerobot/`. Independent sub-agents reviewed spool/runner,
dataset reconciliation, and transport/CLI/examples. Only confirmed risks to
accepted data, published dataset correctness, or essential operation are listed.

## Remaining prerequisite: qualified cloud backend

**Status: unresolved upstream integration prerequisite; existing protective gate.**

`src/vmodal_robot/transports/vmodal.py:VmodalTransport.from_env` always fails
capability validation. Consequently, stock `vmodal-robot run` and `flush` cannot
deliver a revision. The reference Python SDK's video upload alone does not
provide durable storage/reconciliation for telemetry, metadata, and opaque
artifacts or idempotent revision publication. Constructing a fake receiver or
accepting a gateway success would not fix that missing data contract.

**Required fix:** provide a qualified backend implementing unchanged-byte
`deliver_artifact`, remote checksum verification and durable receipts,
destination-scoped artifact idempotency, and idempotent `publish_revision` with
all original descriptors. Qualify stored/absent/unknown lookup semantics and
supported video size, then wire that implementation into `from_env`. Run live
byte-for-byte download, lost-response/restart, and duplicate-side-effect tests
before enabling the stock CLI. Until that evidence exists, keep the startup
gate and use a separately qualified injected Python transport.

**Regression coverage:** `tests/test_vmodal.py:test2` and
`tests/test_cli.py:test3` verify failure before spool admission. Transport wire,
receipt, idempotency, restart, signal-drain, search acceptance, and latency
accounting tests remain required; mock tests do not qualify a live backend.

## Implemented fixes

### Concurrent spool owners could remove live admission payloads

**Status: fixed.** Opening a second `Spool` ran startup recovery while the first
owner could be copying an admission object that had no committed row yet.
Recovery could classify those live bytes as orphaned. Separate owners could
also race delivery claims and cleanup.

**Fix:** hold a nonblocking exclusive OS lock on `state.lock` for the lifetime
of a spool, acquired before database migration/recovery. A competing opener
fails clearly. Release the lock on close and failed initialization; process
termination releases it through the OS. Because the current `status` command
opens this same recovering spool, stop its owner before using CLI status.

**Verification:** subprocess regressions check exclusion during admission,
release on close, SIGKILL, and failed initialization. Existing spool crash,
migration, cleanup, and runner tests cover recovery with one owner.

### Deduplication silently reused conflicting artifact descriptors

**Status: fixed.** Stable artifact IDs include dataset/path/checksum, but exclude
camera/episode references, clock, time bounds, kind, and content type. Reusing
the same bytes/path with corrected references silently retained the old
descriptor, potentially publishing a new revision with wrong episode/camera or
time alignment. Reusing an existing revision ID could similarly discard changes
to source format/version or completeness.

**Fix:** compare the immutable descriptor fields before accepting an existing
ID and reject conflicts before copy or commit. Preserve existing identities and
receipts. Give a changed artifact contract a new path; give a changed revision
contract a new source revision.

**Verification:** `tests/test_spool.py` supplies same-byte conflicting
references, times, clock, kind, content type, and revision fields, and asserts
rejection with the original revision and record count intact.

### Per-camera causal selection admitted future exposure

**Status: fixed.** A camera's `causal` policy filtered on exposure timestamp
without accounting for its end when global `profile.causal` was false. A frame
starting before the requested time but finishing afterward could be treated as
causal evidence.

**Fix:** apply acquisition/exposure-end causality for per-modality `causal` and
`hold` policies as well as the global flag, including clock uncertainty.

**Verification:** `tests/test_temporal.py:test3` selects a start-timestamped
exposure spanning the target under camera-only causal policy and expects
`future_evidence`.

### Active-action selection hid competing uncertain commands

**Status: fixed.** The selector discarded commands with unknown application
times, an application window crossing the target, unqualified application
mapping, or excessive uncertainty, then could select an older active command.
That result claimed valid action alignment despite unresolved competing
activity.

**Fix:** an active-action selection remains unresolved when a potentially
active command cannot be qualified. Commands proven not yet issued or already
expired may be excluded. Preserve the separate supervised-action policy.

**Verification:** `tests/test_temporal.py:test6` combines the old valid command
with uncertain competing commands and expects unresolved results, including
both input orders for excessive application uncertainty. Proven-expired and
ordinary valid commands retain their previous behavior; uncertainty around an
effective-end boundary and an unmapped competing clock remain unresolved.

### Shared-video overlap checks compared incompatible PTS units

**Status: fixed.** The native validator compared integer PTS values directly
between episodes sharing a video path. Different declared rational time bases
could make numeric nonoverlap describe the same actual media interval, allowing
conflicting episode ownership.

**Fix:** normalize interval endpoints to exact rational media time before
checking overlap, grouped by camera and video path.

**Verification:** `tests/test_dataset.py:test4` changes one episode's time base
so its raw PTS range appears separate while actual media time overlaps; validation
must reject it.

### Native validation scaled quadratically with episode count

**Status: fixed.** Each episode rescanned the complete row table, followed by
pairwise shared-video interval checks. Native dataset preparation and recovery
became impractical as episode/sample counts grew.

**Fix:** group rows once by episode; group and sort rational media intervals
once per camera/path. Retain all row identity, feature, timestamp, and ownership
checks. Work now scales with rows plus interval/group sorting instead of
episode-by-row and episode-by-episode products.

**Verification:** a deliberately unsorted 5,000-episode / 10,000-row fixture
preserves ownership and bounds full-row-table scans, so the former implementation
fails immediately instead of requiring a long timing-based test.

## Audit verification

Each new defect regression was confirmed to fail against the original code and
pass after its fix. The spool, runner, adapter, dataset, and temporal standalone
test suites passed. An independent transport audit also ran
`tests/test_vmodal.py test_all --wire=True`, `tests/test_cli.py test_all`,
`tests/test_search_acceptance.py test_all`, and
`tests/test_measure_cloud_latency.py test_all`; all passed.

Independent final source review found no additional critical defects in these
local changes. The unresolved cloud prerequisite above remains outside mock
coverage and must not be described as fixed by this release.

Final combined verification passed: `bash test.sh test` (including package,
installed-wheel, standalone-export, and reference SDK wire checks),
`python -m twine check dist/*`, and `python docs.py generate` followed by
`python docs.py check`. The 5,000-episode / 10,000-row validation took 0.079
seconds in the existing local Python 3.11 environment. This measurement is
local synthetic evidence, not a deployed performance guarantee.
