# V-Modal Robotics SDK

A small Python uploader for finalized LeRobot dataset v3 files. It watches for
explicit ready manifests, copies accepted bytes into a durable SQLite-backed
spool, and schedules video separately from telemetry and metadata.

The core runtime depends only on Python and Fire. It does not import LeRobot,
PyTorch, pandas, ROS, GStreamer, or a separate database service. Install the
reference V-Modal SDK only on robots that use the cloud transport:

```bash
python -m pip install 'vmodal-robotics[vmodal]'
```

Configure the existing `VMODAL_*` SDK variables, then run:

```bash
vmodal-robot run --ready_dir=/data/vmodal-ready --spool_dir=/var/lib/vmodal-robot
vmodal-robot status --spool_dir=/var/lib/vmodal-robot
vmodal-robot flush --spool_dir=/var/lib/vmodal-robot --deadline_seconds=120
```

The producer publishes a JSON file ending in `.ready.json` only after every
referenced file and metadata snapshot is closed and immutable:

```json
{
  "contract_version": 1,
  "source_format": "lerobot",
  "source_version": "v3",
  "source_id": "robot-01",
  "dataset_key": "factory/pick-place",
  "dataset_root": "../dataset",
  "destination": "robot-data/robot-01",
  "source_revision": "capture-0007",
  "complete": false,
  "artifacts": [
    {
      "path": "videos/chunk-000/cam-front.mp4",
      "kind": "video",
      "content_type": "video/mp4",
      "size_bytes": 1234,
      "sha256": "64-lowercase-hex-characters",
      "source_refs": {"camera_key": "observation.images.front", "episodes": [0, 1]},
      "timing": {"source_clock": "lerobot.timestamp", "start": 0.0, "end": 4.0}
    }
  ]
}
```

Each manifest must also include at least one `metadata` artifact. Original MP4,
Parquet, and metadata bytes are preserved. Paths must remain inside
`dataset_root`; symlinks, missing files, changing files, invalid checksums, and
unsupported dataset versions are rejected without deleting producer data.

The current public V-Modal Python SDK has a qualified upload operation for MP4
files only. Generic artifact reconciliation and revision-manifest endpoints are
still an upstream integration requirement. Until those operations exist, the
production transport explicitly blocks telemetry, metadata, and revision
publication instead of sending them to a video-description endpoint. The local
spool and transport interface are fully testable with the included fake-data
suite.
