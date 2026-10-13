# Worked S3 and MinIO integration

Install `python3 -m pip install '.[s3]'` from `lerobot/` in the public repository.
Create a bucket and a dedicated SDK-owned prefix. Credentials use aiobotocore's
standard AWS credential chain (profile, existing environment or workload identity).
No new SDK credential variables are needed. For MinIO, configure that same chain
with your MinIO credentials and pass its endpoint URL.

```bash
python3 examples/s3_upload.py run --bucket=robot-data \
  --ready_dir=/data/ready --spool_dir=/data/spool --prefix=fleet/robot-01
# For MinIO append --endpoint=http://storage.example:9000
# For native bags append --source_format=rosbag2
# Existing backlog only: append --flush=True --deadline_seconds=120
```

For average video scheduling, append `--video_bytes_per_second=524288` (512 KiB/s).
Each video attempt waits for its byte budget before PUT; auxiliary delivery stays
independent. This does not cap packet bursts, verification GETs, or metadata
traffic. Use the Linux traffic-shaping recipe in [fleet operations](fleet_operations.md)
when sharing a control interface. The example derives a request timeout large enough
for the configured maximum video wait; the signal drain deadline still wins.

For dock-only network activity, append `--pause_file=/data/upload.pause`. Create
that file while undocked and remove it while docked. Its presence gates new GET/PUT
requests; it does not abort an already running request. Admission continues so
the recorder must obey the capacity signal. Gate waits are cancelable; interrupted
or timed-out work retains its durable claim and reconciles on resume. The same
pause applies to `--flush=True`; choose its deadline to cover your intended drain.
Quota, video ceiling and retention flags match the core CLI and its existing
environment configuration.

`S3Transport` is a worked implementation of the existing Transport seam. The
stock `VmodalTransport.from_env()` gate remains; the example runs the core directly.
S3 operations are asynchronous and client ownership belongs to the surrounding
async context. Requests have bounded timeouts and runner cancellation shares the
signal drain budget. Local checksum hashing uses a worker thread. Supervisor hard
stops remain necessary for nonreturning kernel disk I/O.

| Capability | Implementation and evidence to check |
| --- | --- |
| All four native kinds | Unchanged video, telemetry, metadata and opaque bytes |
| Durable receipt | Successful conditional storage plus verified remote GET |
| Remote checksum | Stream downloaded bytes through SHA-256 and count length |
| Artifact idempotency | Destination hash + artifact ID key; `If-None-Match: *` |
| Artifact reconciliation | Descriptor plus verified payload; only NoSuchKey means absent |
| Revision publication | All original descriptors, source refs, clocks, paths and IDs |
| Revision idempotency | Conditional immutable revision ID key; conflict never overwrites |
| Revision reconciliation | Download revision; compare descriptors and reverify every payload |
| Bounded video size | 1 GiB example cap, enforced against local admission configuration |
| Recovery of originals | Receipt `s3://bucket/key` identifies unchanged bytes for SDK GET/download |

AWS documents [conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html).
Use an implementation that supports conditional PutObject; unsupported servers
are blocked. Precondition failure triggers byte comparison, never overwrite.
Concurrent conditional-write conflicts/unavailable requests remain uncertain and
can be replayed safely. Destination scopes separate objects; same-basename camera
files retain distinct stable IDs. Descriptors omit spool paths and preserve source
references. Interrupted payload-only uploads can replay; interrupted descriptor or
revision writes reconcile from durable remote content.

The example uses single PUT, bounded at 1 GiB for videos. Segment larger recordings
at the producer; publish independent complete manifests with an immutable metadata
snapshot. Discovery batches of 100 rotate through all eligible manifests, including
populations larger than 100. Keep telemetry in independent manifests when a large
video must not defer it. Revision JSON is capped at 16 MiB; segment large revisions.

Tests use both an in-memory conditional object server with faults and an actual
MinIO container. Repeated delivery/publication yields the same receipt and one
logical object. SHA/descriptor conflicts block; lost responses recover; remote
unavailability is distinct from confirmed absence. MinIO round-trip evidence does
not qualify every S3-compatible provider: validate conditional behavior, durability,
retention policy and fault recovery against your deployed bucket before production.
The prefix must stay exclusively SDK-owned; external overwrites/deletes or lifecycle
expiration invalidate receipts. This example supplies storage and dataset publication;
it does not submit indexing jobs or claim search readiness.

```bash
python3 tests/test_s3.py test_all
# Disposable MinIO with its documented default test credentials only:
python3 tests/test_s3.py test_live --endpoint=http://127.0.0.1:9000
```
