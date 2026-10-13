# Running the uploader on a robot

The worked `examples/s3_upload.py` transport delivers immutable artifacts and
revision publications to an existing S3-compatible bucket. The core VModal gateway
transport still requires its declared capabilities; using the S3 example does not
make the gateway transport available. This guide describes the S3 service and
the read-only spool metrics endpoint.

## Image provisioning and service startup

Use Python 3.10 or newer. Install the wheel with the image's `python3`; the
package supports Ubuntu 22.04 pip 22.0.2 through its setup.cfg build metadata.
Pin a released package version in the image build after qualifying that release.
The example service expects a matching source checkout at
`/opt/vmodal_sdk_robotics/lerobot` because examples are not installed in the wheel.

```sh
sudo python3 -m pip install '/opt/vmodal_sdk_robotics/lerobot[s3,rosbag]'
sudo useradd --system --user-group --home-dir /var/lib/vmodal-robot --shell /usr/sbin/nologin vmodal-robot
sudo install -d -o vmodal-robot -g vmodal-robot -m 0750 /var/lib/vmodal-robot
sudo install -d -o vmodal-robot -g vmodal-robot -m 0750 /srv/robot-data/ready
sudo install -d -o root -g vmodal-robot -m 0750 /etc/vmodal-robot
sudo install -m 0644 examples/systemd/vmodal-robot.service /etc/systemd/system/
sudo install -m 0644 examples/systemd/vmodal-robot-metrics.service /etc/systemd/system/
sudo install -o root -g vmodal-robot -m 0640 examples/systemd/vmodal-robot.env /etc/vmodal-robot/
```

Provision each persistent filesystem mount before creating these directories.
Provision the recorder's group permissions to let the uploader read finalized
artifacts and manifests. Edit the uploader unit's bucket, endpoint, source format
(`lerobot` or `rosbag2`) and paths. Create the bucket beforehand. Configure the
credentials described below before starting the service.

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now vmodal-robot.service vmodal-robot-metrics.service
sudo systemctl status vmodal-robot.service
sudo journalctl -u vmodal-robot.service -n 50 --no-pager
```

`RequiresMountsFor` orders startup after the data mounts. `Before=robot-recorder.service`
is an example ordering relationship: replace it with your recorder unit name and
add `Requires=vmodal-robot.service` and `After=vmodal-robot.service` in that recorder's
unit when startup depends on the uploader. Ordering alone does not prove spool
headroom; recorder admission must also obey the pressure status below. One uploader
owns a spool at a time; concurrent monitoring uses read-only connections.

## Credentials and rotation

The S3 example calls aiobotocore's normal credential provider chain. The supplied
EnvironmentFile contains provider paths and a profile name, without keys. Deliver
the actual profile through your image provisioning or fleet credential agent to
`/etc/vmodal-robot/aws-credentials`, readable by the service account. The profile
name is `[uploader]`; the fields are `aws_access_key_id`, `aws_secret_access_key`
and, for temporary credentials, `aws_session_token`. The optional config file
uses `[profile uploader]` and can select a supported role/process provider.

For refreshable role/container/instance/process credentials, refresh belongs to the
installed SDK provider and the fleet's provider integration; qualify that provider
on your target platform. Static environment or shared-file credentials already
loaded by a client are not promised to reload automatically. For those credentials,
replace the credential file atomically, then run
`sudo systemctl restart vmodal-robot.service`. Keep the existing spool and ready
directory: the restarted client loads the replacement credentials and recovers
uncertain deliveries. SDK provider support is described in the
[AWS credential provider documentation](https://docs.aws.amazon.com/sdkref/latest/guide/standardized-credentials.html).

## Docking, shutdown and recovery

The unit passes a 30-second SDK shutdown deadline and sets `TimeoutStopSec=45`.
SIGTERM stops admission, cancels admission copying cooperatively, and drains
delivery/publication within the shared SDK deadline, including transport close.
The remaining 15 seconds covers process scheduling and exit. No backlog size or
network outage is guaranteed to drain in 30 seconds: expiry leaves durable work
for restart and can produce a nonzero exit. A second stop signal forces expiry.
Blocking filesystem calls and uninterruptible kernel I/O can exceed the cooperative
budget; qualify the device and set the robot supervisor's power-off allowance.
Reference semantics are in the [systemd service manual](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html).

For routine docking, stop the recorder first, publish its final immutable handoff,
then stop the uploader. Leave power on until the stop completes or the platform's
qualified shutdown allowance expires. With coverage unavailable, preserve the spool
across power-off and resume at the next boot.

For an uploader that runs throughout driving, add
`--pause_file=/run/vmodal-robot-upload.paused` to the S3 command. Have the docking
supervisor create that file while undocked and remove it once docked. Its presence
pauses new S3 GET/PUT requests; an already in-flight request continues. Admission
and disk pressure still operate during the pause, so producers must respect
backpressure. Startup state is part of the docking integration: create the file
before uploader startup when the robot starts undocked. Shutdown cancels waiting
requests within the same drain deadline.

After an interrupted upload:

1. Check that `/var/lib/vmodal-robot` is the original mounted data partition, and
   that `state.sqlite3` and `objects/` survived. Never replace a missing mount with
   an empty spool to make the service start.
2. Check free bytes, permissions, service logs and metrics. Stop the writer before
   copying a recovery backup of the whole spool, including SQLite WAL/SHM files;
   do not copy only a live `state.sqlite3` file.
3. Restart the uploader against the same spool, source manifests and destination.
   Recovery reconciles uncertain remote acceptance before deciding whether to
   resend. Do not delete pending payloads or reset rows manually.
4. Watch lane pending bytes and age decline, and inspect blocked counts/rejection
   reasons if they do not. Capacity deferral preserves the producer handoff; the
   recorder must keep its immutable files until admission is confirmed.
5. Confirm revision acknowledgement as well as artifact acknowledgement. Automatic
   cleanup removes only eligible retained local payloads after durable downstream
   evidence; historical records remain. Retention is not a pending-data expiry.

## Fleet metrics and producer pressure

```sh
python3 -m vmodal_robot.monitoring serve --spool_dir=/var/lib/vmodal-robot --host=127.0.0.1 --port=9108
curl --fail http://127.0.0.1:9108/metrics
```

The installed entry point is `vmodal-robot-metrics serve` with the same arguments.
Each scrape opens its own read-only SQLite snapshot; it neither initializes a
missing spool nor runs migration/recovery. HTTP 503 plus `vmodal_robot_spool_up 0`
means the database is missing, unreadable or incompatible. A readable spool gives
HTTP 200 and `spool_up 1`; this is not proof the uploader process or transport is
healthy. The exporter stays running so the next scrape can recover automatically.

Metrics include `vmodal_robot_spool_bytes`, `disk_free_bytes`, `disk_headroom_bytes`,
`retry_count`, `blocked_count`, `rejected_handoffs`, `deferred_handoffs` and
`cleanup_eligible_count`. `vmodal_robot_lane_pending_bytes`, `lane_pending_count`
and `lane_oldest_age_seconds` each have a `lane="video"` or `lane="aux"` label.
These values are gauges over retained state, including retry counts, rather than
process-lifetime monotonic counters. Quotas and retention come from the persisted
uploader configuration when available.

Discovery gauges expose `manifests_present`, `unseen`, `pending`, `deferred`,
`rejected` and `admitted` from the last completed adapter observation.
`vmodal_robot_discovery_age_seconds` makes stale observations visible. Counts
are emitted once `vmodal_robot_discovery_observed` changes from 0 to 1; a new spool
without a completed adapter observation reports 0 and no discovery counts.
The counts describe the adapter's current manifest directory and its current lifetime; they
are not an all-time producer ledger. Alert on stale discovery, nonzero unseen or
deferred manifests, old lane age, rising backlog and blocked work. Also monitor
the systemd service separately and keep the recorder's own production count.

`vmodal_robot_pause_recording` and `vmodal_robot_pause_video_recording` represent
the spool admission recommendation. Detailed JSON `admission_pressure` includes
the action and reasons. The SDK exposes this advice; the recorder must implement
stop/downsample policy and acknowledge admission before removing source files.
HTTP is bound to localhost by default; use a local Prometheus agent or explicitly
select a reachable bind address for your fleet collector.

Use the manifest `source_id` as the stable robot identity and `dataset_key` as
the mission or dataset scope. ROS bag handoffs with `--fleet_refs=True` carry those values as
`robot_id` and `mission_id` in artifact source references. Preserve them across
retries and keep destinations stable; a different robot must have a different
source identity even when it observes the same mission. Enable fleet refs for a new
mission/dataset key; leave historical handoffs unchanged so replay retains their
existing identities and immutable references.

## Upload interface and bandwidth policy

The SDK does not select a network interface or provide a wire-rate guarantee.
The S3 example's optional `--video_bytes_per_second=500000` delays each video PUT
attempt by its byte length divided by that rate. This bounds average scheduled
video attempts on that client; auxiliary requests continue independently, retries
pay the budget again, and GET requests and instantaneous wire bursts are not
capped. The example derives a request timeout large enough for its configured
maximum video size and pacing delay. Use the OS policy below for a wire-rate cap.
Use a dedicated upload interface and Linux routing/shaping. These commands are a
worked deployment example: `eth1` has `192.0.2.10/24` with gateway `192.0.2.1`;
replace all three with your provisioned interface/address/gateway. The example
dedicates that interface to uploader traffic and caps its entire egress to 4 Mbit/s.

```sh
robot_uid=$(id -u vmodal-robot)
sudo ip route replace table 199 192.0.2.0/24 dev eth1 src 192.0.2.10
sudo ip route replace table 199 default via 192.0.2.1 dev eth1
sudo ip rule add priority 10990 uidrange "${robot_uid}-${robot_uid}" lookup 199
sudo tc qdisc replace dev eth1 root tbf rate 4mbit burst 64kb latency 200ms
ip rule show
ip route show table 199
sudo tc -s qdisc show dev eth1
```

That qdisc caps egress only. Verification downloads need an ingress cap as well.
On this dedicated uplink, redirect ingress to a dedicated IFB device and shape it:

```sh
sudo modprobe ifb numifbs=1
sudo ip link set ifb0 up
sudo tc qdisc replace dev eth1 handle ffff: ingress
sudo tc filter replace dev eth1 parent ffff: protocol all u32 match u32 0 0 action mirred egress redirect dev ifb0
sudo tc qdisc replace dev ifb0 root tbf rate 4mbit burst 64kb latency 200ms
```

Reserve `ifb0` for this policy or substitute an unused provisioned IFB device.
Ingress shaping reduces delivery to local sockets; it cannot prevent packets
already arriving over the radio. Qualify link congestion behavior on the robot.

The policy routes locally generated service-account traffic (including its DNS
requests); other users keep their existing routing. Configure endpoint reachability
and DNS on the dedicated link. The TBF limit applies to every process using `eth1`,
so this exact example belongs on a dedicated uplink. If control traffic shares an
interface, integrate a classful policy into the robot's existing qdisc instead of
replacing it with this interface-wide cap. Persist the policy through the image's
network manager before uploader startup. Routing and shaping semantics are in the
[ip rule manual](https://man7.org/linux/man-pages/man8/ip-rule.8.html) and
[tc TBF manual](https://man7.org/linux/man-pages/man8/tc-tbf.8.html).
The ingress redirect uses [the iproute2 mirred action](https://man7.org/linux/man-pages/man8/tc-mirred.8.html).

To remove only this example policy:

```sh
robot_uid=$(id -u vmodal-robot)
sudo ip rule del priority 10990 uidrange "${robot_uid}-${robot_uid}" lookup 199
sudo ip route flush table 199
sudo tc qdisc del dev eth1 root
sudo tc qdisc del dev eth1 ingress
sudo tc qdisc del dev ifb0 root
sudo ip link set ifb0 down
```
