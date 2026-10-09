# Search qualification for LeRobot consumers

Cloud retrieval is asynchronous memory until the collection's temporal coverage,
distance ceiling, and capture-to-decision freshness have been qualified. Upload
ACK, revision publication, index readiness, and search visibility are separate
events. A successful query does not establish suitability for robot actuation.

## Temporal sampling: local evidence and remaining gates

Status on 2026-10-10: **local sampler exercised; deployed indexing and event
retrieval PENDING**. No production window length, overlap, or sampling interval
has been selected. Do not interpret this guide's experimental settings as defaults.

The upstream source inspected was `vmx_avideo` commit
`05c193f799fddd572133f8f748f28eb056fc1587`; the files below were clean in that
checkout. The deployed build and effective collection configuration remain
unknown. Paths in this table are relative to that upstream repository.

| Source | Observed behavior |
| --- | --- |
| `eglobals/src/data_model.py:283`, `DEFAULT_FRAME_CFG` | Default 1 candidate/second, `sampler_algo_new_v3_speed`, 360p, WebP quality 85. |
| `embed/src/processors/frame_sampler.py:133`, `FrameSamplerConfig`; `:968`, `frame_sample_video`; `:740`, `_frame_sample_cv2` | Dispatches to the default plugin, then applies single-color rejection before writing selected frames. Filename timestamps can be rebased by `start_ts_unix_user_ms`. |
| `embed/src/processors/samplers/sampler_algo_new_v3_speed.py:213`, `sample_mp4_frames` | Decodes frame indices in steps `max(1, int(source_fps / candidate_fps))`; timestamps are derived from frame index/source FPS. dHash selection uses previous-distance minimum 6 and next-distance minimum 35 through v3 pair selection. Nominal FPS timestamps need additional validation on variable frame rate input. |
| `infra/search_api_ui/routers/frontui_indexation.py:2130`, `dict_generate_vidfile_img_emb` | Uses `FrameSamplerConfig()`. If normal extraction yields no frame, retries interval sampling at 0.001 candidate/second with color filters relaxed. This is a one-representative fallback for ordinary clips. Existing WebP directories skip extraction unless explicitly reprocessed. |
| `infra/search_api_ui/routers/stream_indexation/core/stage_planner.py:111`, `list_units_vid` | Legacy static plan uses `floor(duration * candidate_fps)` timestamps `round(i*1000/candidate_fps)`; this is not the aggressive sampler. |
| `infra/search_api_ui/routers/stream_indexation/locals/embed/src/processors/frame_sampler.py:33`, `frame_sample_video_ts_list` | Legacy explicit-timestamp extraction reports `embedded`, `skipped_filter`, or `skipped_unreadable` per timestamp. |
| `infra/search_api_ui/routers/stream_indexation/adapter/adapter_frontui_indexation.py:538`, `dict_generate_vid_img_chunk`; `core/stream_data.py:184`, `list_discover_frame_rows_stats` | Progressive embedding consumes already extracted frame files and parses their timestamp references; it does not resample raw MP4. |

There is no one-minute sampling switch in the inspected default extraction
path. A one-frame result can instead arise from selection/filtering, the explicit
fallback, or reuse of old extracted frames. These are hypotheses for a deployed
incident, not a diagnosed deployed cause. Splitting MP4 files alone does not
change any of these policies. Confirm the active route/job and recorded
`frame_sampler_config`, `frame_sampler_fallback_config`, model and index version
from the actual job before proposing a backend patch.

Source SHA-256 values for comparison with the deployed source:

```text
frontui_indexation.py  96ce776d30fa6cbcfccaae879a76e05a1635f7c0dccb848c76a15db9649ff794
frame_sampler.py       41938a5d2e80283ab605d57874f5cb359e4df5628c79c4fb0ed668b47b01ae67
sampler_algo_new_v3_speed.py 00662d71e49fdba53a5480c51c6be34ececd2488bd6f611e79448768f125d360
eglobals/src/data_model.py 7741915ab90dd1a716ea3d6f16ce2a54e47eb7f2924bae1e7a4d4339f4066d36
```

### Reproduce local coverage with real media

Run from the `vmx_mobile` repository root in the existing environment. Requires
installed ffmpeg/ffprobe and the upstream sampler's existing dependencies; this
does not upload, embed, or create an index. Temporary files remain isolated from
producer data. Both synthetic views use unique filenames; real camera identity
must come from the manifest, not its basename.

```bash
q0_dir=$(mktemp -d /tmp/vmodal-q0.XXXXXX)
for dur in 5 15 30 59 60 61; do
  mid=$((dur / 2))
  late=$((dur - 1))
  for cam in front wrist; do
    flip='null'
    if [ "$cam" = wrist ]; then flip='hflip'; fi
    ffmpeg -hide_banner -loglevel error -f lavfi \
      -i "testsrc2=size=320x240:rate=10:duration=$dur" \
      -vf "$flip,drawbox=x=20:y=20:w=100:h=100:color=red:t=fill:enable='between(t,0,0.8)',drawbox=x=120:y=60:w=100:h=100:color=blue:t=fill:enable='between(t,$mid,$mid+0.8)',drawbox=x=200:y=120:w=100:h=100:color=yellow:t=fill:enable='between(t,$late,$late+0.8)'" \
      -c:v libx264 -preset ultrafast -crf 20 -pix_fmt yuv420p \
      "$q0_dir/${cam}_${dur}s.mp4" || exit 1
  done
done
for file in "$q0_dir"/*.mp4; do
  stem=$(basename "$file" .mp4)
  ffmpeg -hide_banner -loglevel error -i "$file" -f null - || exit 1
  ffprobe -v error -show_entries stream=duration,nb_frames,r_frame_rate \
    -of json "$file" > "$q0_dir/$stem.probe.json" || exit 1
  PYTHONPATH=../vmx_avideo/embed:../vmx_avideo:../vmx_avideo/eglobals \
    python ../vmx_avideo/embed/src/processors/samplers/sampler_algo_new_v3_speed.py \
    sample_mp4 --video_path "$file" --output_dir "$q0_dir/plugin/$stem" \
    --frame_per_sec 1 --resize_format 360p \
    > "$q0_dir/$stem.plugin.log" 2>&1 || exit 1
done
```

The `sample` output includes candidate count, selected count, selected indices,
source-time filenames, and sampler parameters. It does not include the full
extractor's subsequent single-color filter. To exercise that path for a fixture:

```bash
PYTHONPATH=../vmx_avideo/embed:../vmx_avideo:../vmx_avideo/eglobals \
  python ../vmx_avideo/embed/src/processors/frame_sampler.py sample_video \
  --video_path "$q0_dir/front_5s.mp4" --dirout "$q0_dir/full_sampler" \
  --frame_per_sec 1 --sampling_method sampler_algo_new_v3_speed
```

Executed locally with Python 3.11.13, ffmpeg 9.0.1, macOS arm64, and the pinned
upstream checkout on 2026-10-10. All 12 MP4s decoded without errors; ffprobe
reported 10 FPS and `10 * duration` frames. Plugin parameters were candidate
FPS 1, 360p, thumbnail 64, dHash thresholds 6/35, and one worker. Actual selected
source timestamps in seconds:

| Duration | Candidates per view | Front selection | Wrist selection |
| --- | ---: | --- | --- |
| 5 s | 5 | 0, 2, 3 | 0, 2, 3 |
| 15 s | 15 | 0, 7 | 0, 7, 8 |
| 30 s | 30 | 0, 15 | 0, 15, 16 |
| 59 s | 59 | 0, 29, 30 | 0, 29, 30 |
| 60 s | 60 | 0, 30, 31 | 0, 30, 31 |
| 61 s | 61 | 0, 30, 31 | 0, 30, 31 |

The full extractor also wrote three frames at 0/2/3 s for `front_5s.mp4`.
Selected timestamps intersected beginning and middle event intervals in all
12 fixtures, and intersected **none of the 12 terminal yellow-event intervals**.
This is 24/36 labeled intervals with a retained sample, not measured semantic
search recall. The late event started at `duration-1` and lasted 0.8 s; its
candidate frame existed but was discarded by selection. Therefore this local
default is unsuitable for any requirement that all those events remain
observable. This reproduces temporal loss, not the reported deployed
one-frame-under-a-minute incident. Indexed counts and retrieval recall remain
PENDING; keep raw job/search evidence when running live qualification.

### Separate the three temporal controls

Producer shard closure determines when immutable bytes can enter the spool.
Candidate frame interval and subsequent filters determine which moments enter
the index. Semantic window length and overlap group source references for
retrieval. Changing one control does not implicitly configure the others. Keep
native MP4 checksums and references; create semantic windows as metadata rather
than copying overlapping videos.

Evaluate all nine candidates: window length **2, 5, 10 seconds**, each with
overlap **0%, 25%, 50%**. Start with a candidate interval shorter than the
shortest event that must be recovered; vary the event's phase against the
sampling grid, then verify post-filter coverage. An interval less than event
duration is only a candidate-grid condition, not a guarantee of semantic recall.

For an experimental window reference schedule, require finite `L > 0` and
finite overlap seconds `0 <= O < L`. Stride is `L-O`. For a source span `[0,D)`,
start at 0 and advance by stride while the previous window ends before `D`;
clip each end to `min(start+L,D)` and retain the final partial window. This avoids
an extra terminal window when a previous window already covers `D`. Interpret
all boundaries as half-open and require monotonically mapped source timestamps.
Count an event spanning several windows once by its labeled event identity.

Boundary policy for these experiments: never cross cameras or episode
boundaries. A window may cross a shard boundary only if metadata proves the same
camera and episode, continuous monotonic source time, and an explicit mapping to
both immutable artifact IDs and file offsets. Otherwise end the window at the
boundary and start a separate span. A storage file containing several episodes
must be split into episode reference spans, without altering its bytes. Report
events clipped or unobservable at boundaries as misses, not missing labels.

### Live qualification record and decision

Use a disposable collection and an explicitly invoked live run. For each
duration/camera and candidate policy, retain manifests, checksums, publication
receipts, index job IDs, active configuration, raw search responses, and actual
indexed rows/timestamps. A local selected WebP count is not an indexed row count.
Query known beginning/middle/end events only after recording readiness and then
measure first retrieval separately. Include no-event controls, events crossing
window/shard boundaries, a final partial window, multiple episodes per file,
variable frame rate, and real representative observations from both cameras.

Record these fields under `docs/` for each run:

| Evidence | Required values |
| --- | --- |
| Identity | Run ID, artifact/camera/episode IDs, revision, checksum, source offsets and clock mapping. |
| Pinning | Producer/LeRobot version, SDK version, backend build, embedding model/preprocessing, collection/index version, sampler/filter/fallback configuration. |
| Policy | Policy version, window length, overlap seconds, candidate interval, boundary/terminal rules. |
| Coverage | Decoded/candidate/selected/filtered/indexed counts, actual indexed source timestamps, index failures and skip reasons. |
| Retrieval | Labeled event count, unique events recovered/missed, held-out recall, no-event false matches, raw responses and deduplication rule. |
| Tradeoff | Queue units, stored/vector bytes, measured cloud cost, capture-to-searchable freshness, failure rate. |
| Decision | Consumer's minimum event duration and required recall/freshness; selected policy or explicit rejection with reason. |

Current decision: **PENDING**. Synthetic local decoding can reveal temporal
loss; it cannot establish robot-domain presence calibration, cloud recall,
queue cost, deployed configuration, or a safe production policy. Open a
coordinated upstream change only with the deployed reproducer and coverage
evidence. Qualify the patched/pinned configuration before accepting cloud
results for decisions.

## Per-collection distance acceptance: qualification remains pending

Status on 2026-10-10: **offline consumer policy checked; actual wire responses,
robot-domain labels, consumer-approved budgets, and held-out calibration PENDING**.
`docs/search_profile.json` is deliberately unqualified, with no numerical ceiling
or freshness budget. It returns UNKNOWN. None of the synthetic thresholds in
tests are production recommendations.

Local source observations at the upstream commit pinned above:

| Source | Observed mapping; deployed qualification still required |
| --- | --- |
| `infra/search_api_ui/routers/frontui_search.py:859`, `SearchResultItem_frontui`; `:957`, `map_backendresponse_to_frontui` | `score` is documented/mapped as per-source LanceDB `_distance`; `score_full` is a combined score; `score_ui` is display normalization. Missing/unparseable raw input can become `score=0.0`, so field existence alone cannot prove a measured distance. |
| `infra/search_api_ui/routers/utils_embed.py:17`, `emb_score_norm` | With unequal raw scores, `score_ui=1/(kactor+(score-min)/(max-min))`; with equal scores, `1/(kactor+0.5)`. The best hit can display 1.0 while remaining unrelated. Normalization depends on the result set and is not a calibrated probability. |
| `search_full/src/interface/pipe_search_frontui.py:1055`, `result_combine` | Weighted mode stores each source row's `score * source_weight / total_active_weight` as `score_full`. Do not assume it is a raw distance or a sum of all evidence for a hit; capture active sources/weights and actual transformations. |
| `search_full/src/acore_lancedb/lancedb_search_emb.py:382` | Search selects `options.metric`, falling back to `cosine`. This is a configurable local default, not proof of the deployed metric for every model/index. |
| `uinterface/sdk_python/src/vmodal/models.py:8`, `VBaseModel`; `:38`, `SearchResultItem_frontui`; `resources/searches.py`, `SearchesResource.search_video` | The reference SDK allows extra hit fields and preserves them through `model_dump()`. It does not establish metric, resolved model/index, score provenance, or capture-wall-clock semantics. |

Capture actual text and image responses with backend/SDK/model builds pinned.
For each supported collection and query path, record raw distance source, metric,
direction, preprocessing, resolved index version, active source/combination policy,
and response transformations. Correlate the hit with its actual indexed row so a
synthetic fallback zero cannot be mistaken for a measurement. Do not rename wire
fields or treat `ts_unix`/`ts_unix_13digits` as capture wall time until their source
clock mapping has been verified. If required metadata cannot be established,
leave the profile unqualified and coordinate an upstream contract change.

### Calibration record and version invalidation

Build representative positives, hard negatives, empty scenes, camera/lighting
variants, and stale captures. Partition by episode/session before creating
overlapping windows so related frames cannot leak into held-out evaluation.
The consumer owner must choose and approve the false-accept budget and freshness
deadline. Derive an absolute maximum distance from calibration, then report the
held-out positive/negative counts, false accepts, misses, uncertainty, and failed
or missing observations. Retain labels, partition IDs, raw distances, pinned
configuration, selected ceiling, and the owner's decision under `docs/`.
An observed error fraction alone does not certify a reliability guarantee.

Each versioned profile names a `calibration_record`, `calibration_version`,
approved `false_accept_budget`, raw `distance_field`, `max_distance`,
`freshness_seconds`, and `max_clock_uncertainty_seconds`. Its exact scope includes
collection (including owner/mode/group/stream identity), embedding model, metric,
direction, preprocessing, camera domain, index version, sampling/segmentation
policy version, text versus image query kind, and source combination policy.
Pin the resolved versions; request defaults such as "latest" are insufficient.
Record active sources and weights in the combination policy identifier.
Any scope change requires a new calibration record/profile; unchanged numerical
ceilings cannot be silently reused. Overlapping/repeated hits are correlated,
so count unique labeled events rather than multiplying display confidence.

### Small consumer example

`examples/search_acceptance.py:accept_hit` operates outside `vmodal_robot` and
adds no search facade, ML library, or network operation. A consumer already using
the reference Python SDK supplies `hit.model_dump()`, the qualified resolved
scope, its versioned JSON profile, and **per-hit** evidence. The caller must bind
the wire/index evidence and clock mapping to that exact hit/artifact/camera/
episode; evidence from another hit cannot be reused. `wire_record` and
`clock_mapping_record` identify those retained records. The flags
`raw_distance_qualified` and `clock_mapping_qualified` express that verification;
setting them does not perform or replace qualification.

`capture_wall_time` is the mapped source capture time in Unix seconds.
`clock_uncertainty_seconds` bounds the total relative error between capture and
the supplied consumer `now_wall`, including anchor, synchronization, and mapping
error. The function rejects when worst-case age `now_wall-capture+uncertainty`
exceeds the freshness budget, and returns UNKNOWN for absent mapping, excessive
uncertainty, or timestamps in the future beyond that uncertainty. LeRobot
episode-relative seconds require an explicit episode wall-time anchor.

The result contains ACCEPTED, REJECTED, or UNKNOWN, a reason, the raw hit/scope/
evidence, and calibration version. Only finite raw distance at or below the
absolute ceiling and qualified fresh scope can be accepted. Missing qualification
is UNKNOWN; non-finite distance, mismatched scope, excessive distance, or stale
capture cannot be promoted by `score_ui`, a top confidence of 1.0, or top-k size.
Empty responses have no accepted hit. UNKNOWN and REJECTED cannot drive actuation;
even ACCEPTED here establishes only this retrieval policy, not robot safety.

Offline validation from the development `lerobot/` directory:

```bash
PYTHONPATH="$PWD" python tests/test_search_acceptance.py test_all
```

The nine assertion checks cover absolute-boundary behavior, confidence 1.0,
non-finite/missing scores, all scope changes, stale/missing/future capture times,
uncertainty, absent wire/clock evidence, an empty result, the unqualified template,
profile invalidation, and top-k independence. All inputs are explicitly synthetic.
The production exit gate remains held-out evidence and explicit ceiling/freshness
budgets for each qualified collection after temporal sampling is selected.

## Capture-to-searchable latency accounting

Status on 2026-10-10: **offline accounting/fault checks passed; live robot/cloud
latency and consumer suitability PENDING**. No production latency numbers are
claimed. The 40–60 ms observation describes a query; it excludes capture shard
closure, admission, backlog, durable storage, publication and indexing.

`examples/measure_cloud_latency.py` provides offline reporting and a separately
invoked live reference-SDK experiment. The live path uploads a native video,
submits an index, polls a qualified ready state, and searches until it retrieves
the labeled source event. It calls the distance/freshness policy above before
recording an accepted decision. It preserves failed requests/runs and raw SDK
responses, including server `execution_time_ms`, separately from client elapsed
request times. A gateway upload response is recorded as `gateway_upload`; it
does **not** establish the durable-storage receipt or revision publication that
the generic robot transport must qualify.

### Offline trace format and commands

Use this from the development `lerobot/` directory; these commands perform no
network request:

```bash
PYTHONPATH="$PWD" python tests/test_measure_cloud_latency.py test_all
PYTHONPATH="$PWD" python examples/measure_cloud_latency.py report \
  --trace_file=docs/robot_latency_trace.json --output=docs/robot_latency_report.json
```

The first command generates **synthetic** accounting inputs and output under the
test output directory. The second consumes an explicitly supplied real trace;
`robot_latency_trace.json` is a qualification artifact to collect, not bundled
production evidence. The JSON object has `runs`, `metadata`, `deadline_seconds`
and `max_clock_uncertainty_seconds`. Each run contains `run_id`, `clock_domain`,
`correlation` (artifact/revision/camera/episode/event/job IDs), `status` (`success`,
`failed`, `timeout`), `bytes`, `retries` (null when unobserved), and `events`.
Never combine timestamps from separate monotonic domains by subtraction. Map
distributed intervals onto one clock with bounded uncertainty, or leave the
stage unmeasured and retain its service-local trace separately.

| Event keys in `events` | Measured interval |
| --- | --- |
| `capture`, `finalized` | Capture buffering, including shard/window closure. |
| `finalized`, `manifest_ready` | Finalization-to-durable handoff. |
| `manifest_ready`, `accepted` | Durable spool admission, including hashing/copy. |
| `accepted`, `first_attempt` | Initial queue residence, including outage backlog. |
| `first_attempt`, `upload_start` | Retry/backoff residence before the successful attempt. |
| `upload_start`, `durable_ack` | Upload through verified durable storage/finalization ACK. |
| `last_artifact_ack`, `revision_published` | Publication after the final referenced artifact ACK. |
| `index_submit`, `index_ready` | Index submission, queueing and documented readiness. |
| `index_ready`, `first_verified_retrieval` | Visibility of the labeled known event after readiness. |
| `query_start`, `decision` | Client request/response and acceptance evaluation for the matching query. |
| `capture`, `accepted_decision` | Direct observed end-to-end interval; never a sum of stage percentiles. |

Timestamps are finite monotonic seconds; paired intervals must be ordered.
Set `durable_ack_verified`, `revision_published_verified`,
`index_ready_verified`, `search_verified` and `accepted_verified` only with the
corresponding qualified receipt/identity/readiness/acceptance evidence. Missing
events or unverified boundaries appear in `missing_stages`; they are not zero.
For publication timing, `last_artifact_ack` must identify the last ACK across
**all** revision members, not merely the uploaded video.

Absolute freshness additionally requires `capture_wall_time` and
`decision_wall_time` in Unix seconds, `clock_mapping_qualified`,
`clock_mapping_record`, and a nonnegative `clock_uncertainty_seconds` bounding
the entire capture-to-consumer relative clock error. Record an explicit episode
wall-time anchor for LeRobot relative timestamps. Freshness reports nominal,
lower and upper ages; suitability compares the upper age and uncertainty with
the consumer budgets. Clock synchronization alone does not anchor an episode.

Reports retain every input run, per-stage missing counts, n/p50/p95/p99/max,
failure/timeout counts and rates, transfer bytes/effective upload throughput,
retry counts, and environment/version metadata. Request summaries keep attempts
and failures alongside successful-request percentiles. A successful stage in a
failed run remains visible; an unavailable end-to-end sample is counted missing.
Record failed attempts individually in `requests` using `operation`, `status`,
`elapsed_seconds`, and the correlated raw response when available.

### Explicit live SDK subset

Use the existing SDK credentials/configuration, a disposable collection named
`robot-latency-*`, and a pinned configuration JSON prepared from actual wire
qualification. No live operation runs in the ordinary test suite:

```bash
PYTHONPATH="$PWD" python examples/measure_cloud_latency.py live \
  --config_file=docs/robot_latency_live_config.json \
  --output=docs/robot_latency_live_run.json --live=True
```

Required configuration fields:

| Field | Required content |
| --- | --- |
| `disposable` | Boolean true; explicitly selects a disposable destination. |
| `upload` | Public `collections.video_upload` arguments: native `filepath_local`, `collection_name`, `sub_collection_name`, `mode`; no byte reduction or invented CCTV wall timestamp. |
| `index` | Public `indexes.create_index` arguments: matching `group_name`, `stream_name`, `mode`, qualified modality/model/index version and fixed sampling policy. |
| `search` | Public `searches.search_video` arguments: same group/stream/mode, known-event query, pinned `version_lancedb`, qualified sources/combination and thresholds. |
| `expected_hit` | Nonempty exact wire-field/value mapping uniquely identifying the newly uploaded source/camera/episode. Confirm it binds to this upload receipt; a camera name or any top hit alone is insufficient. |
| `event_time_field`, `event_interval` | Qualified source-time wire field and labeled `[start,end]` interval in that field's units. Retrieval must match both source identity and event interval. |
| `wire_mapping_record` | Retained source/episode/camera/receipt-to-hit mapping evidence. Field aliases must come from the actual response. |
| `index_ready_states`, `index_failed_states`, `readiness_record` | Disjoint exact job-state strings and retained deployed evidence that the ready states mean the requested index is usable. Mere job submission or successful job completion is insufficient without qualification. |
| `source_wall_anchor`, `event_time_unit_seconds`, `capture_source_time` | Unix-second anchor for source time zero, positive conversion from wire time units, and the known event's capture time in those units. The matching hit's capture wall time is calculated separately for per-hit acceptance. |
| `scope`, `profile`, `evidence` | Resolved scope, qualified Q1 profile, and wire/clock evidence described above. The unqualified template cannot produce an accepted result. |
| `timeout_seconds`, `poll_seconds` | Positive finite overall experiment bound and polling interval. The experiment stops on index failure, rejected/unknown known retrieval, or deadline expiry. |
| `environment` | Backend build, producer version, network/fault profile, warm/cold index, backlog size, cameras, model/preprocessing, collection/index/sampling/window versions. Unknown values remain explicit. |
| `deadline_seconds`, `max_clock_uncertainty_seconds` | Consumer-defined integration/freshness budget and clock uncertainty allowance. Omit to report UNKNOWN suitability. |

HTTP retries and multipart retry/concurrency are fixed to zero retries/one
attempt/one lane for this subset, so failed request attempts are visible. Search
polls are repeated observations, not upload retries or independent evidence.
Retain the destination and result for review; the tool performs no deletion.
Qualified transport traces are still needed for durability, all artifact kinds,
spool admission, backlog/retries and publication. Their absence leaves the
full-chain suitability UNKNOWN even after a successful live video query.

### Evidence checked and suitability decision

The offline nine-check suite covers explicit stage timestamps, outage queue
residence, failed indexing, missing readiness, upload success followed by delayed
known-event visibility, correlated identity/time filtering, clock uncertainty,
stale acceptance, retained failures/timeouts, atomic JSON reporting and direct
end-to-end percentile accounting. A fake SDK also exercises polling before
visibility and cancellation before readiness. These checks validate accounting,
not the deployed backend or robot capture pipeline.

Collect actual runs for 5/15/30/59/60/61-second clips and pinned native larger
shards; warm/cold indexes; one/two cameras; healthy Wi-Fi, bandwidth restriction,
outage/reconnect and a pre-existing backlog. Fix sampling/window policy per run.
Combine all successful and failed runs for each comparable environment using
the offline report command; preserve retries, bytes, receipts and raw responses.
Do not combine unrelated conditions into an unexplained latency percentile.

Current suitability: **UNKNOWN; integration gate remains closed**. No consumer
deadline, deployed readiness/receipt mapping or robot-to-decision timing record
has been provided. Complete traces within a declared budget produce only
`OBSERVED_WITHIN_BUDGET`; budget violations/failures produce
`OBSERVED_UNSUITABLE`, and missing stages/clock evidence/budgets remain UNKNOWN.
No finite set of measurements certifies deterministic cloud latency. Keep
retrieval asynchronous for memory/review/planning and the local control loop
independent until the intended use case's evidence and failure behavior are
reviewed.
