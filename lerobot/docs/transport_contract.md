# Transport, spool migration, and recovery contract

The local uploader preserves native LeRobot v3 MP4, Parquet, metadata, and opaque
bytes. Admission copies a complete immutable manifest into SQLite-backed storage;
it never deletes producer data or partially accepts a revision. Continuous local
delivery requires a qualified transport. No stock generic cloud backend is wired
into `VmodalTransport.from_env()`; the CLI refuses `run`/`flush` before creating a
spool. Credentials or a larger upload cap do not bypass this qualification gate.

## Qualified Python integration

Reuse `Runner`, `Spool`, and `LeRobotAdapter` with a transport implementing
`deliver`, `reconcile`, and `publish_revision`. Custom adapters may implement
only `discover(limit)`; the built-in adapter also exposes optional
`admission_feedback(item, outcome)` and `drain_errors()` hooks. Admission feedback
is `accepted` only after the spool commit. Capacity/I/O failures are `deferred`;
immutable malformed input is `rejected` until its content changes. Eligible batch
limits apply after filtering and deferred work rotates fairly.

For the existing V-Modal bridge, supply
`VmodalTransport(client, artifact_api=qualified_api)`. Its generic API provides:

| Operation | Contract |
| --- | --- |
| `deliver_artifact(artifact, destination)` | Store unchanged bytes by destination and artifact ID; return durable verified receipt. |
| `reconcile_artifact(artifact, destination)` | Return verified stored receipt, `None` only for confirmed absence, or raise `TransportUnknown` for unsupported/unavailable lookup. |
| `publish_revision(revision)` | Resolve original descriptors and checksums for every artifact ID; preserve paths/cameras/episodes/offsets; publish idempotently by revision ID. |
| `reconcile_revision(revision)` | Optional durable publication lookup with the same stored/absent/unknown distinction. |

The API must expose `TransportCapabilities` with supported artifact kinds,
durable receipts, remote checksum verification, artifact and revision idempotency,
revision publication, and a positive integer `max_video_bytes`. Optional
reconciliation flags require corresponding methods. Capability declarations are
an integration contract backed by qualification evidence, not a self-certification
mechanism. The runner validates its admission limit against the declared cap
before discovery. Effective size is the minimum of admission and qualified
backend limits. The reference Python SDK's independent 100 MiB preflight cap
still matters to any backend implementation that uses its upload call.

An artifact `Receipt` must carry the expected `artifact_id`, a stable nonempty
`remote_ref`, `status="ACKNOWLEDGED"`, the exact expected SHA-256, and
`checksum_verified=True` based on remote verification. HTTP success, a storage
key allocation, a copied local checksum, and index submission are insufficient.
A revision receipt must identify the revision and prove durable publication.

Remote behavior is scoped by destination and stable identity: same ID and bytes
returns the same logical durable result; same ID with different checksum is a
conflict. Repeat uploads/publications must not create duplicate logical objects,
revisions, finalization effects, or indexing jobs. Lost responses require lookup
or qualified idempotent repeat, including interrupted publication. Original
source paths and source clocks stay in references; do not invent UTC timestamps
from LeRobot episode-relative seconds.

## Delivery and retention

Video, auxiliary, publication, and discovery/admission workers advance
independently. Each upload lane has one in-flight artifact. Filesystem
hashing/copying runs outside the network event loop; one thread owns SQLite.
Transient network failures retry with capped exponential backoff and jitter,
unlimited by default (`RunnerConfig.max_attempts=0`). An explicit positive limit
retains finite-attempt compatibility. Invalid inputs, authorization/configuration
errors, unsupported operations, and identity/checksum conflicts become visible
operator-action states. Unknown remote outcomes remain uncertain unless qualified
idempotency permits replay. An empty runnable lane does not mean a complete drain
when blocked revisions or deferred handoffs remain.

Automatic cleanup runs during admission and delivery/publication progress. Its
default retention is zero seconds after acknowledgement, but deletion still
requires a verified artifact ACK and durable publication of **every** referencing
revision. Pending, blocked, and uncertain payloads remain. Producer data is never
cleanup-owned. Set `retention_seconds` or `VMODAL_ROBOT_RETENTION_SECONDS` to retain
ACK payloads longer. Retained byte/record quotas release after cleanup; SQLite IDs
and receipts remain as deduplication history and grow over time.

`max_video_bytes` is exposed by `run`, `flush`, and `status`, with
`VMODAL_ROBOT_MAX_VIDEO_BYTES`; the default is 104,857,600 bytes (100 MiB).
200 decimal MB is 200,000,000 bytes. A producer's shard target is not a strict
guaranteed maximum. Admission is all-or-nothing: an oversized bundled video
defers telemetry too. For independent telemetry, emit a separate complete
manifest with its metadata snapshot.

Signals establish one absolute shutdown deadline covering recovery, uploads,
publication, and client close. A second signal forces cancellation. Cancellation
retains ambiguous claims for restart recovery. Copy loops cooperatively stop
between chunks and finish before SQLite closes. A nonreturning kernel filesystem
call or noncooperative custom transport needs a process supervisor hard stop;
Python cancellation cannot establish a hard bound for arbitrary blocking code.

## Existing-spool migration and incident recovery

Never run two daemon versions against one spool. Stop the daemon and preserve
producer originals. Before opening an old spool, back up the complete spool
directory, including payloads and a consistent SQLite database/WAL state. Test
the new version first on that copy and a disposable destination.

The implementation creates `state.before-fixes-v1.sqlite3` before additive schema
or legacy video naming migrations. This database snapshot is not a payload
backup. Video naming changes from `payload` to the **full existing artifact ID
plus `.mp4`**, without changing IDs. Migration validates owned regular files,
length and SHA-256, then uses a durable hard link or verified copy before updating
the row and removing the old name. Capacity/I/O failure defers migration; checksum
conflicts block delivery and preserve evidence. Interrupted migration recovery
retains the authoritative verified copy and preserves conflicting bytes.

Legacy receipts receive `checksum_verified=False`; migration does not turn an
echoed local checksum into remote evidence. A qualified transport must reconcile
retained legacy acknowledged artifacts before cleanup. If lookup is unavailable,
that data stays retained and visible for operator action.

For a blocked operation, fix its configuration/capability cause first, retain its
original error and IDs, and reconcile the remote outcome before requeueing.
Confirmed storage can ACK; confirmed absence or qualified idempotent replay can
retry. Never clear an identity/checksum conflict just to make the queue progress.
Requeueing needs an audited operator procedure; there is no public CLI command
that blindly resets every blocked row. Keep uncertain claims when lookup cannot
establish an outcome.

Use a reviewed allowlist of affected artifact/revision IDs and record the old
error, operator decision, qualification evidence, and reconciliation receipt.
Only capability/configuration blocks whose cause is corrected qualify. After
confirmed absence or verified idempotent replay eligibility, an integration can
use the existing `Spool.mark_ready(artifact_id)` or
`Spool.mark_revision_ready(revision_id)` for those reviewed IDs. A stored lookup
uses its validated acknowledgement instead. Preserve conflict states and never
perform a blanket SQL state reset.

If the spool database is lost, restore its matching database/payload backup and
reconcile against durable remote receipts. First admission generates a persisted
dataset ID; deleting the database and readmitting producer files can generate
new identities. Exactly-once replay after database deletion is not guaranteed.
Do not reclaim pending space by deleting the database or accepted payloads.
For rollback, stop the new version and verify old-schema/local-reference
compatibility before restarting an older version; a database snapshot alone
does not restore already reclaimed payloads.

## Qualification gates

Local assertion/fault tests verify discovery, atomic admission, migration, quotas,
lane independence, lost receipts, retries, retention, and signal behavior using
fake receivers. Stock cloud support remains gated on a deployed generic backend
that retrieves exact bytes of every kind, publishes complete revisions, preserves
same-basename camera/chunk identities, and deduplicates finalization/index jobs
after lost responses. A pinned actual producer's native shard layout/size also
needs end-to-end qualification.

[Search qualification](search_qualification.md) records local real-media sampler
evidence and the remaining temporal coverage, held-out distance/freshness, and
latency gates. Its [profile template](search_profile.json) is unqualified and
returns UNKNOWN. Upload ACK, publication, index-ready, first retrieval, and
consumer decision are separate measurements. No local fake test or query-stage
latency claim qualifies cloud actuation.
