# Closed ROS 2 bags to ready manifests

Install `python3 -m pip install '.[rosbag]'` from the public `lerobot/` directory,
and source your Humble/Jazzy installation. `rclpy` comes from ROS, not the core SDK.
The node handles only completed bags. It preserves metadata.yaml and the exact
SQLite3/MCAP storage bytes; it does not transcode a bag into a LeRobot dataset.
`RosbagAdapter` returns the existing RevisionInput contract with source format
`rosbag2`, version `1`; LeRobot remains `lerobot`, version `v3`.

```bash
vmodal-ros2-manifest run --closed_dir=/data/closed --ready_dir=/data/ready \
  --source_id=robot-01 --dataset_key=robot-01/bags --destination=robot-data
# Recorder wrapper journals only successful closure, including SIGINT/SIGTERM:
bash examples/record_bag.sh record /data/bags/run-001 /data/closed /chatter
```

The wrapper forwards termination to `ros2 bag record`, waits for its successful
exit, then writes a durable `.closed` journal record. It uses Linux GNU `env` to
reset inherited background signal dispositions before launching the recorder.
A ROS subscription on
`/vmodal/closed_bag` also accepts absolute paths in `std_msgs/msg/String`, using
reliable/transient-local QoS. A recorder integration may publish that notification
after closing and fsyncing all storage files. Persist the completion journal at
the recorder boundary to survive lost messages or node downtime. Use
`vmodal-ros2-manifest notify --bag_dir=/data/bags/run-001 --closed_dir=/data/closed`
after a verified successful recorder exit when integrating another supervisor.

The timer scans durable completion records; a single worker performs hashing,
fsync and immutable ready publication off the ROS executor. Failures log a visible
reason and retry on later scans. Successful publication atomically records `.done`;
restart or duplicate notifications preserve one manifest. Keep both journals and
ready manifests on persistent storage until spool admission. Do not delete a ready
manifest behind `.done`; remove the corresponding `.done` deliberately if you need
to republish. Each completed bag directory is immutable and must never be reused.

Metadata lists exact relative storage paths. Path escape, symlinks, missing files,
unsupported storage suffixes, changing bytes and active SQLite WAL/SHM files are
rejected. A quiet file, metadata existence, or split event does not prove final
closure, so the node does not infer completion from those signals. Native ROS
recorders must exit gracefully and checkpoint their SQLite files. Compression of
native `.db3.zstd` / `.mcap.zstd` files is preserved. Timestamps remain in native
bag metadata; no UTC or LeRobot episode timestamps are invented.

Use `examples/s3_upload.py run --source_format=rosbag2` with the worked transport.
Core Python integration is `Runner(RosbagAdapter(ready_dir), spool, transport)`.

For fleet attribution, use a stable robot ID as `source_id` and a mission/session
key as `dataset_key`, for example `robot-01/mission-2026-10-13-am`. The bag handoff
can copy these values into every artifact's `source_refs.robot_id` and
`source_refs.mission_id`, so S3 descriptors retain readable attribution alongside
the dataset's stable hash. A shared destination is safe because dataset identity
includes source ID and dataset key. Never change identity or references after
admission; start a new mission key for a new mission. Enable `--fleet_refs=True`
only for new missions/captures; its default is false so historical handoffs replay
byte-for-byte. Use a new dataset key when adopting fleet refs rather than changing
the immutable descriptors of already admitted bags. LeRobot producers should use
the same convention and include both attribution values in their immutable
metadata and source refs. Existing version-1 manifests remain valid.

Public Humble/Jazzy CI records live `/chatter`, closes the recorder, publishes the
completion event to the actual node and admits its ready manifest into the spool.
