# sdk_ros_robot — New features backlog (researcher workflow)

Date: 2026-10-10. Status: proposal / backlog. Nothing listed here is implemented
unless it says **exists**.

Goal: turn `sdk_ros_robot` from a durable uploader into a tool that robotics
engineers and researchers use every day to **collect** robot vision + telemetry,
**find** past moments with VModal search (SDK or MCP), **inspect** them with
aligned telemetry, and **curate** them into datasets for training and evaluation.

Each feature has an ID, a priority, the problem it solves for the researcher,
the proposed behavior, the data contract it depends on, and whether it needs only
local code, an existing VModal API, or a new backend capability. Read the
"Current baseline" section first: several features are blocked by the same
gates.


## 0. Current baseline (what exists today)

`lerobot/` package `vmodal-robotics` (CLI `vmodal-robot`) **exists**:
producer writes `*.ready.json` → LeRobot adapter validates bytes → durable
SQLite/WAL spool → independent video and aux lanes → `Transport.deliver /
reconcile / publish_revision` → revision published after every artifact ACK.
CLI: `run`, `status`, `flush`.

Hard limits that shape this backlog:

- Stock `VmodalTransport.from_env()` fails by design: no generic artifact +
  revision backend is qualified. Today nothing reaches a VModal collection
  through the stock CLI. See `lerobot/docs/implementation_v1.md`.
- Search qualification (`lerobot/docs/search_qualification.md`) showed the
  default sampler (1 candidate/s, dHash selection) dropped **all 12 terminal
  events** in short clips. Short robot events can be invisible to search.
- Cloud search is asynchronous memory. It must never sit in the control,
  safety, or recording loop. Every feature below keeps that boundary.

VModal surfaces available to build on (**exist** in `uinterface/sdk_python`
and `uinterface/mcp_python`):

- Search: `search_video` with `query_text`, `image_query` (base64),
  `query_metadata`, `start_date` / `end_date`, `offset` / `limit`,
  `version_lancedb`. MCP `find` adds `image_path`, `image_url`, `save_images`.
- Search hit fields (MCP trimmed row): `score`, `mode`, `group_name`,
  `stream_name`, `filename`, `source_path`, `ts_unix_13digits`, `text_snippet`.
- Collections: `collection_groups_list`, `collection_subcollections_list`,
  `collection_video_upload(_bulk)`, `collection_upload_metadata` (JSONL,
  `write_mode`, `allow_overlap`), `collection_description_update`
  (`description`, `tag_json`), `collection_add_assets`, `collection_delete`.
- Index: `index_create`, `index_status`, `index_jobs_list`, `index_delete`.
- Frames: `image_get_url(_bulk)`, `image_get_from_url`,
  `image_download_from_records`.

Contract rule: `uinterface/sdk_python` is the reference API. Every feature that
calls VModal goes through `vmodal` (sdk_python) — no raw HTTP in this package.


## 1. The researcher loop this backlog targets

collect on robot → mark interesting moments → upload + index → search
(text / image / metadata / time) → resolve hit to episode, frame, camera,
telemetry → inspect with aligned telemetry → label / annotate → curate subset →
export LeRobot dataset → train / evaluate policy → search new failures → repeat.

Today the loop breaks at three places:

- **Upload**: no qualified stock path to VModal (Gate G1).
- **Hit resolution**: a search hit is `filename + ts_unix_13digits`; nothing
  maps it back to `episode_index / frame_index / camera_key / telemetry row`.
- **Short events**: sampler policy can skip the moment the researcher wants.

Priorities: **P0** = unblocks the loop, **P1** = core researcher value,
**P2** = productivity / scale, **P3** = later.


## 2. Cross-cutting data contract: RobotFrameRef

Most features depend on one identity record. Define it once in
`vmodal_robot/contracts.py` and use it everywhere (CLI output, Python API, MCP
tool output, exported files).

RobotFrameRef fields:

- `dataset_key`, `source_id` (robot), `source_revision` — from the manifest.
- `artifact_id` — stable spool ID (dataset identity + relative path + SHA-256);
  also the remote video filename stem (`<artifact_id>.mp4`, **exists**).
- `camera_key` — e.g. `observation.images.wrist`, from `source_refs`.
- `episode_index` — LeRobot episode.
- `frame_index` — frame within the episode.
- `t_episode_s` — seconds from episode start (LeRobot `timestamp`).
- `t_file_s` — seconds from start of the physical MP4 (v3 shards hold several
  episodes; `source_refs.video_offsets` give per-episode offsets).
- `ts_unix_ms` — wall clock used by VModal (`ts_unix_13digits`).
- `group_name` / `stream_name` — VModal collection / sub-collection.
- `index_version` — `version_lancedb` the hit came from (reproducibility).

Mapping rules (to verify against live data, see Open questions):

- remote `filename` → strip `.mp4` → `artifact_id` → spool row → manifest
  `source_refs` (camera, episodes, video_offsets) and `timing`.
- `ts_unix_13digits` → `t_file_s` using the file's `capture_start_unix_ms`
  (new manifest field, F-03).
- `t_file_s` → `episode_index` = last offset ≤ `t_file_s`; `t_episode_s` =
  `t_file_s − offset`; `frame_index` = `round(t_episode_s × fps)` from
  `meta/info.json`.
- `episode_index + t_episode_s` → telemetry Parquet row via
  `source_refs.row_ranges`.

Integrity constraints to enforce:

- Resolution fails closed: unknown `artifact_id`, missing offsets, or a
  timestamp outside the file span returns an explicit `unresolved` reason —
  never a guessed episode.
- Spool history keeps artifact IDs and manifests after payload cleanup
  (**exists** for IDs/receipts; manifest `source_refs` must also be retained).
- Variable frame rate input is flagged; `frame_index` from nominal FPS is
  approximate there.


## 3. Capture and ingestion

### F-01 (P0) Qualified VModal stock transport — Gate G1

- Problem: researchers cannot get robot data into a searchable collection with
  the shipped CLI.
- Feature: wire `VmodalTransport.from_env()` to a qualified backend. Video lane
  → `collection_video_upload` (collection/stream from manifest `destination`,
  **exists** as `collection/stream`). Telemetry Parquet + metadata →
  generic artifact storage (backend gate). Revision publish → metadata JSONL
  index record (F-04) + revision marker.
- Contract: receipts must carry remote verified checksum and stable remote
  ref (current `_receipt` rules stay unchanged).
- Dependency: **needs backend** — generic artifact storage and idempotent
  revision publication; until then only video + metadata JSONL paths exist.
- Done when: `vmodal-robot run` delivers one LeRobot revision end to end to a
  disposable collection, survives kill-after-upload-before-ACK, and the video is
  returned by `search_video`.

### F-02 (P0) Index trigger and "searchable" state

- Problem: upload ACK ≠ indexed ≠ searchable. Researchers search too early and
  conclude the data is missing.
- Feature: after revision publish, optionally call `index_create` for the
  collection (batched, debounced), poll `index_status`, store `index_job_id`,
  `indexed_at`, `version_lancedb` per revision in the spool. `status` shows
  per revision: `ACKNOWLEDGED → PUBLISHED → INDEXING → SEARCHABLE` plus lag.
- Dependency: **existing API** (`index_create`, `index_status`).
- Done when: `vmodal-robot status` reports capture→searchable latency per
  revision; a search issued before `SEARCHABLE` prints a warning.

### F-03 (P0) Wall-clock anchor in the manifest

- Problem: VModal hits use unix ms; LeRobot uses episode-relative seconds. No
  anchor = no way back to the frame.
- Feature: additive manifest fields per video artifact:
  `capture_start_unix_ms`, `clock_source` (`ntp`, `ptp`, `ros_time`,
  `monotonic_unsynced`), optional `clock_offset_ms` uncertainty. Pass
  `capture_start_unix_ms` as the upload start timestamp so the server sampler
  rebases frame timestamps (server already supports `start_ts_unix_user_ms`
  in `frame_sampler.py` — verify the upload route exposes it).
- Contract: `contract_version` stays 1 if fields are optional; resolver marks
  hits from manifests without the anchor as `unresolved: no_clock_anchor`.

### F-04 (P0) Per-frame telemetry metadata sidecar

- Problem: search finds pixels only; researchers want "gripper closed and
  force > 20 N" or "task = pick_cube and success = false".
- Feature: at admission (off the control loop), join telemetry Parquet rows to
  the sampling timestamps and emit a metadata JSONL per revision: one line per
  frame-time with `ts_unix_13digits`, `filename`, `episode_index`,
  `frame_index`, `camera_key`, `task`, `success`, selected state / action
  scalars, `robot_id`, `policy_version`, `operator`, `source_kind` (real/sim).
  Upload with `collection_upload_metadata` (`write_mode=append`,
  `allow_overlap=false`).
- Config: a column allow-list (`--meta_columns observation.state.gripper,
  action.*`) and downsampling to bound JSONL size. Vectors are summarized
  (min/max/mean or named components), never uploaded whole.
- Dependency: **existing API** for upload; **verify** which `query_metadata`
  operators the backend supports (equality only vs range).
- Integrity: JSONL keys mirror RobotFrameRef; re-upload of the same revision
  must not duplicate rows (idempotent key = `artifact_id + ts`).

### F-05 (P1) Episode annotations and task language

- Problem: LeRobot already stores task instructions (`meta/tasks`) and often a
  success flag; they are lost after upload.
- Feature: map episode-level fields to `collection_description_update`
  (`description` = task instruction, `tag_json` = `{success, robot_id,
  policy_version, scene, objects}`) and to F-04 rows. CLI
  `vmodal-robot annotate --episode 41 --tag success=false --note "slipped"`
  writes a local annotation record that ships as metadata (never edits producer
  files).
- Dependency: **existing API**.

### F-06 (P1) Event markers (operator and code)

- Problem: the interesting moment (grasp failed, collision, e-stop, human
  intervention) is known at capture time but lost.
- Feature: three marker sources, all writing an append-only local event file
  that ships as a metadata artifact:
  - CLI / hotkey: `vmodal-robot mark "grasp slipped" --tag failure`.
  - ROS 2 node subscribing to a configurable topic (e.g. `/vmodal/mark`,
    `std_msgs/String`) and to standard signals (e-stop, controller errors,
    `diagnostics` level ERROR).
  - Python call `vmodal_robot.mark(label, tags)` usable from teleop scripts.
- Markers get `ts_unix_ms` and are attached to frames within a window.
  They also raise upload priority (F-21).
- Constraint: marker write is a single local append; no network call, safe to
  call inside a recording process.

### F-07 (P1) Event-aware sampling policy

- Problem: Q0 showed short terminal events are dropped by the default sampler.
- Feature: per collection/revision sampling hints sent at upload: candidate
  FPS, `keep_timestamps` (force frames at marker times and at episode
  start/end), and "dense around markers" (e.g. 5 fps ±2 s). Default for robot
  collections: dense at episode boundaries + markers.
- Dependency: **needs backend** — upload route must accept a sampler config
  and forced timestamps (`frame_sample_video_ts_list` exists upstream for
  explicit timestamps; not exposed via the public upload).
- Done when: the Q0 fixture set retains 36/36 labeled intervals in deployed
  indexed rows, verified by `index_status` + search.

### F-08 (P1) ROS 2 rosbag2 / MCAP adapter

- Problem: the package is named `sdk_ros_robot` but only reads LeRobot v3. Most
  ROS labs record rosbag2 (MCAP).
- Feature: `RosbagArtifactAdapter` per `docs/03_interface_rev3.md` §4.1: detect
  finalized bag splits, keep the MCAP native, extract camera topics to MP4
  (optional converter behind the adapter boundary), extract selected scalar
  topics (`/joint_states`, `/tf`, `/wrench`, `/diagnostics`) to Parquet for
  F-04. `format_id = rosbag2/mcap`, `group_id = bag id`.
- Constraint: no ROS import in the core package; adapter is an optional extra
  (`vmodal-robotics[ros2]`). Core spool/transport untouched (ADP-06).

### F-09 (P2) Trossen / generic MCAP / proprietary adapters

- Same boundary as F-08 for TrossenMCAP and vendor bundles (rev3 §4.3–4.4).
  Priority set by user demand.

### F-10 (P2) Simulation data ingestion

- Problem: researchers compare sim vs real; sim rollouts (Isaac, MuJoCo,
  LeRobot sim envs) are separate silos.
- Feature: same manifest contract with `source_kind=sim`, `sim_engine`,
  `seed`, `scene_config_hash`. Sim and real can share one collection and be
  filtered by metadata.

### F-11 (P3) Google Intrinsic connector

- Implement the design in `google_intrinsic/readme.md` (RGB-D, robot state,
  skill provenance). Out of scope until F-01 and F-08 land.


## 4. Search and retrieval

### F-12 (P0) Hit → RobotFrameRef resolver

- Problem: a search hit is useless to a researcher until it says "episode 41,
  wrist camera, frame 312, t=10.4 s".
- Feature: `vmodal_robot.resolve(hit) -> RobotFrameRef` and batch version, using
  the local spool database (and an exported manifest index, F-13, when run off
  the robot). Every search command and MCP tool returns resolved refs plus the
  raw hit.
- Dependency: **local only** + F-03.
- Done when: for a disposable collection with known events, every returned
  hit resolves, and frame pixels at `frame_index` match the remote frame
  (pixel hash within tolerance).

### F-13 (P0) Portable manifest index

- Problem: researchers search from a laptop, not from the robot; the spool lives
  on the robot.
- Feature: `vmodal-robot index-export --out robot_index.parquet` dumps
  `artifact_id → dataset_key, source_revision, relative_path, camera_key,
  episodes, video_offsets, row_ranges, capture_start_unix_ms, fps, remote_ref`.
  Also upload it as a metadata artifact per revision so any workstation can
  rebuild it from the collection.
- Integrity: append-only, keyed by `artifact_id`; rebuilding from remote
  must equal local export (checksum compare).

### F-14 (P1) `vmodal-robot search` CLI + Python `RobotSearch`

- Thin wrapper over `vmodal` `search_video`:
  - text: `--query "gripper drops the red cube"`
  - image by example from a local dataset:
    `--like episode=12,frame=340,camera=wrist` (extracts the frame locally,
    sends base64 `image_query`)
  - image file: `--image ./failure.jpg`
  - metadata: `--where task=pick_cube --where success=false`
  - time: `--since 2026-10-01 --until 2026-10-07`, or `--revision capture-000042`
  - scope: `--collection`, `--sub_collection` (robot/session)
- Output: JSON / table / Parquet of resolved RobotFrameRef + score +
  `index_version`; `--all` pages through `offset/limit` with dedupe.
- Dependency: **existing API**.

### F-15 (P1) Moment grouping (frames → segments)

- Problem: search returns many near-identical adjacent frames and the same
  moment from several cameras.
- Feature: group resolved hits by `(source_revision, episode_index)` and merge
  frames within a gap threshold (default 1.0 s) into a segment
  `{episode, t_start, t_end, cameras[], best_score, n_hits}`. Rank segments by
  best score. Option `--per_episode 1` for "one result per episode".
- Dependency: **local only**.

### F-16 (P1) Telemetry context fetch

- Problem: after finding a moment, researchers want the joint states, actions,
  forces around it.
- Feature: `RobotSearch.context(ref, before_s=2, after_s=2)` returns a pandas
  DataFrame of telemetry rows for that window, from the local LeRobot dataset
  if present, otherwise from the remote telemetry artifact (needs F-01 storage).
  CLI: `vmodal-robot context --ref <json> --out ctx.parquet`.
- Dependency: **local only** for local datasets; **needs backend** for remote
  Parquet retrieval.

### F-17 (P1) Frame and clip download

- Feature: download hit frames (`image_get_url_bulk` / `image_download_from_records`,
  **existing API**) and cut short local clips around a segment from the local
  MP4 (ffmpeg, optional dependency). Contact sheet output: one image grid per
  query, frames labeled with episode / t / camera / score.

### F-18 (P2) Multi-camera synchronized view

- For a segment, return frames from every camera at the same `ts_unix_ms`
  (nearest frame per camera within tolerance), so wrist + front views are
  compared side by side. Requires F-03 on every camera.

### F-19 (P2) Search provenance record ("search receipt")

- Problem: research results must be reproducible; the index changes over time.
- Feature: every search writes a JSON receipt: query, filters, collection,
  `version_lancedb`, SDK version, timestamp, returned refs and scores.
  `vmodal-robot search --replay receipt.json` re-runs and diffs results.
- Dependency: **local only**; index model/version fields need backend exposure
  (see Open questions).


## 5. Analysis and curation

### F-20 (P1) Export search results to a LeRobot sub-dataset

- Problem: the main reason researchers search is to build training or eval
  sets ("all failed grasps on transparent objects").
- Feature: `vmodal-robot export --refs hits.parquet --out ./ds_failures
  --unit episode|segment`. Episode mode copies whole episodes via LeRobot
  tooling; segment mode writes trimmed episodes with re-indexed frames. Writes a
  provenance file listing source revision, artifact IDs, checksums, and the
  search receipt (F-19).
- Constraint: read-only on source datasets; output is a new dataset dir;
  refuses to overwrite an existing non-empty output.
- Dependency: **local only**; LeRobot as optional extra.

### F-21 (P1) Priority upload for flagged data

- Problem: on a limited link, a whole day of nominal runs blocks the three
  failure episodes the researcher needs now.
- Feature: manifest `priority` (0–9), auto-raised by markers (F-06) or failure
  tags; video lane picks higher priority first, FIFO within a priority. No
  change to the ACK/publication invariants.
- Dependency: **local only** (spool scheduler change; keep aux lane
  independence).

### F-22 (P1) Notebook helpers

- `vmodal_robot.nb`: `show(refs)` renders a frame grid inline; `plot(ref,
  columns=[...])` plots telemetry with a vertical line at the hit time and the
  frame thumbnail; `to_df(hits)` returns a tidy DataFrame. Optional extra
  (`[notebook]`: pandas, matplotlib, pillow). Core stays Fire-only.

### F-23 (P1) Labeling loop: confirm / reject hits

- Problem: researchers verify hits by eye; that judgement is lost.
- Feature: `vmodal-robot label --refs hits.parquet` (simple terminal or
  notebook UI) records `relevant / not relevant / label` per segment; labels
  are saved locally and uploaded as metadata (F-04 schema, `label_source=human`)
  so later searches can filter on them.
- Dependency: **existing API** (metadata upload).

### F-24 (P1) Retrieval evaluation harness

- Problem: "can I trust search to find all failures?" — no number exists.
- Feature: extend `examples/search_acceptance.py` into
  `vmodal-robot eval-search --labels events.jsonl --collection X`: for each
  labeled event (episode, t_start, t_end, query), run search and report
  recall@k, first-hit rank, temporal coverage, and misses by reason (not
  sampled / sampled not ranked / unresolved). Uses F-12 resolver and F-23
  labels. Output feeds the Q1 collection acceptance profile.

### F-25 (P2) Dataset report card

- `vmodal-robot report --revision X` (or per collection): episodes, duration,
  cameras, FPS, success rate, markers per type, frames indexed vs expected,
  upload / index lag, unresolved hits. Markdown + JSON for papers and lab
  logs.

### F-26 (P2) Policy comparison by scenario

- Search the same scenario query across `policy_version` values (metadata
  filter), group by episode, report success rate and failure examples per
  version. Built from F-04, F-14, F-15.

### F-27 (P2) Failure mining / similar-failure search

- From one failure frame, find visually similar moments across all robots
  (image query, F-14 `--like`), group by robot / policy / scene. Optional
  clustering of hit frames needs embeddings returned by the API — **needs
  backend** (embedding vectors are not exposed today).

### F-28 (P3) Dataset hygiene: near-duplicate and dead-frame detection

- Flag episodes with frozen cameras, black frames, or near-duplicate content
  before training. Local-first (frame hashes); cross-dataset dedupe needs
  backend embeddings.


## 6. Agent / MCP workflow

### F-29 (P1) Robot-aware MCP tools

- Problem: MCP `find` returns filename + timestamp; an agent cannot answer
  "which episode and what was the gripper doing".
- Feature: robot tools registered next to (not inside) `mcp_python`, reusing
  its registry pattern and the same `vmodal` client:
  - `robot_find(query_text|image, collection, where, since, until)` →
    resolved segments (F-12, F-15).
  - `robot_context(ref, before_s, after_s)` → telemetry summary + frames (F-16).
  - `robot_export(refs, out_dir, unit)` → local dataset (F-20), confirmation
    required.
  - `robot_label(refs, label)` → metadata write (F-23).
- Coupling: tool schemas must follow `mcp_python` conventions
  (`collection_name`, `sub_collection_name`, `version_id`); decide packaging
  (plugin entrypoint vs separate server) before implementation.

### F-30 (P1) `vmodal-robot-find` skill

- Like the existing `vmodal-find` skill: agent searches, resolves, downloads
  frames, and **verifies each hit on the actual frame + telemetry** before
  reporting. Report includes episode, camera, time, telemetry snippet, and a
  confidence note.

### F-31 (P2) Agent-assisted annotation

- Agent proposes labels for segments (e.g. "grasp failure: object slipped"),
  human confirms in F-23; only confirmed labels are written with
  `label_source=agent_confirmed`. Unconfirmed proposals stay local.


## 7. Fleet and operations (engineer side)

### F-32 (P1) Fleet status

- `vmodal-robot fleet-status --hosts robots.txt` (or each robot pushes its
  `status` JSON as a small metadata artifact) → one table: backlog bytes,
  oldest item age, blocked count, disk headroom, last searchable revision.

### F-33 (P2) Upload windows and bandwidth cap

- `--upload_window 22:00-06:00`, `--only_when_docked` (file/command probe),
  `--max_upload_bps` (rev3 scheduler contract). Markers with priority ≥ N may
  bypass the window.

### F-34 (P2) Live preview path (separate from durable path)

- Optional low-rate live JPEG/WebRTC preview of one camera for remote
  monitoring (rev3 StreamTransport). Never replays backlog; never shares the
  spool. Lower priority than the durable/search loop.

### F-35 (P2) Episode-level delete and retention

- `vmodal-robot forget --episode 41 --revision X`: removes remote video and
  metadata rows for that episode (collection API) and records a tombstone in the
  spool so it is never re-uploaded. Needed for bad takes and consent requests.
- Dependency: **needs backend** for per-file / per-row delete granularity;
  `collection_delete` is collection-level today.

### F-36 (P3) Privacy filter before upload

- Optional face / screen blur on video before admission (adapter-side
  converter, keeps the original local). Off by default.


## 8. Suggested delivery order

- Milestone M1 "searchable robot data": F-01, F-02, F-03, F-04, F-12, F-13,
  F-14. Exit test: upload one revision to a disposable collection, search a
  known event by text and by metadata, resolve every hit to episode/frame,
  verify pixels.
- Milestone M2 "researcher loop": F-05, F-06, F-15, F-16, F-17, F-20, F-22,
  F-24. Exit test: from a query, export a LeRobot sub-dataset of failure
  segments and report recall on a labeled event set.
- Milestone M3 "agents + ROS": F-07, F-08, F-29, F-30, F-21, F-23.
- Milestone M4 "scale": F-18, F-19, F-25–F-28, F-31–F-36, F-09–F-11.


## 9. Open questions (verify before implementing)

- What exactly is `ts_unix_13digits` on a video hit: unix ms rebased by an
  upload start timestamp, or file-relative ms? Does the public upload route
  accept a start timestamp? (Blocks F-03, F-12.)
- Which `query_metadata` operators does the backend support (equality, `in`,
  range)? Are metadata rows joined to frames by `filename + ts` exactly, or by
  nearest timestamp? (Blocks F-04, F-14 `--where`.)
- Can the upload route accept a per-file sampler config / forced timestamps?
  (Blocks F-07.)
- Does the search response expose model name and index version per hit?
  (F-19 reproducibility.)
- Is there per-file / per-row delete in a collection? (F-35.)
- Collection naming convention for robot data: proposed
  `collection = project or task family`, `sub_collection = robot_id` (or
  `robot_id__session`). Needs product decision before F-01 hardcodes it.
- Packaging of robot MCP tools: plugin into `mcp_python` or separate server.
  (F-29; keep `sdk_python` as the single API contract either way.)


## 10. Non-goals

- No cloud call, upload, hashing, or search inside the robot control, safety,
  or recording loop.
- No mandatory format conversion; native LeRobot / MCAP bytes stay the source
  of truth.
- No local reimplementation of VModal search or embeddings; this package
  resolves, groups, and curates around the VModal API.
- No claim that search is complete for an event type until F-24 measures it
  on that collection.
