# VModal Robotics SDK v0.2.0 — would I put this on my AGV?

Tested on Ubuntu 22.04 / Python 3.10 / commit `fe16e8d` (tag `v0.2.0_run38217816021`)

I work on autonomous AGVs, so I evaluated this against a deployment I know: a robot
running an 8-hour warehouse shift with two Luxonis depth cameras (OAK-D Pro, OAK-D
Lite), detecting AprilTags for localization, and docking itself when the battery
runs low. New tags every 2–3 minutes. Warehouse WiFi that dropped at every
access-point handoff without exception, with dead zones in parts of the building.

**Short answer: not yet for a full shift, yes for short sessions.** Here's why, and
then what it would actually take to integrate it into a robot system.

---

## What's right about it

Network I/O stays out of the control loop. That's not a style choice on a moving
robot — an upload that blocks the loop stalls the vehicle mid-path.

And the failure mode it targets is the real one. A durable local spool with retry is
the correct answer to WiFi that drops at every handoff. Whoever designed this had met
the problem in person.

The failure paths are forced with absurd limits rather than mocked — a 100-byte spool
quota, a 5-byte video ceiling — so the deferral path is genuinely exercised. The
retry path survives twelve consecutive simulated outages on one artifact. And the
nastiest case is covered: a delivery whose outcome is unknown *after* the far side
already accepted it, which is what produces duplicate uploads in naive
implementations. Seeing that tested is why I'd track this rather than build from
scratch.

---

## The blocker: two ceilings that close off opposite directions

`fixes_v1.md` says *"Discovery stops after 100 manifests."* And `status` reports a
hard per-file video ceiling of 100 MiB (`max_video_bytes: 104857600`).

An 8-hour shift is 480 minutes. A tag every 2–3 minutes is **160–240 recording
events**. Two ways to segment that, both fail:

**One dataset per detection** → 160–240 manifests against a ceiling of 100.
Discovery stops around midday. The afternoon is never picked up, and
`rejected_handoffs` stays at zero because these aren't rejected — they're never
seen. Silent half-shift data loss, no alert.

**One long dataset per shift** → one manifest, ceiling irrelevant, but now the video
has to fit in 100 MiB. An OAK-D at 800p/30fps writes ~1.95 MiB per 16-bit disparity
frame, about 58 MiB/s — that's 100 MiB in **under two seconds** of raw depth. Even
compressed 1080p at ~4 Mbit/s fills it in three and a half minutes.

Fine-grained breaks on manifests, coarse breaks on video size, and nothing in
between clears both for a full shift.

Short clips are fine: ~10s of compressed 1080p is roughly 5 MB. **That's the
workload this version fits — short bounded sessions, well under a hundred of them.**

---

## Three more things that hit normal operation

**SIGTERM drain overrun fires daily.** `fixes_v1.md` lists *"SIGTERM exceeds drain
budget"* as a reliability item, which reads like an edge case. On a robot that docks
itself when the battery runs low, controlled shutdown is part of the duty cycle,
several times a day. I'd rank this higher than its placement suggests.

**No spool reclamation.** `"retention_seconds": 0.0` with `"last_cleanup": null`
confirms the `fixes_v1.md` item. There's a 10 GiB spool ceiling and no expiry to
keep it under that ceiling. Also worth sizing before committing: 10 GiB spool +
10 GiB headroom + 512 MiB aux reserve is over 20 GiB reserved, on compute modules
that often ship with 32–64 GB of eMMC.

**Stock transport can't upload — and says so.** `status` lists 10 missing
capabilities, including `deliver_artifact` and `publish_revision`, which are the two
operations the SDK exists for. README lines 92–94 state this openly: *"currently
`run` and `flush` report missing transport capabilities."* So v0.2.0 ships the
durability layer with the transport left as an integration point. A blocker for
deployment, not a defect.

---

## Install gotcha, relevant to robot image builds

Robot images are commonly Ubuntu 22.04 with ROS 2 Humble, and that distribution's
stock pip is 22.0.2. The README install command fails there:

```
WARNING: Generating metadata for package vmodal-robotics produced metadata
         for project name unknown.
ERROR: No matching distribution found for vmodal-robotics (unavailable)
```

Cause: `pyproject.toml` uses the PEP 639 licence form (`license = "MIT"` plus
`license-files`), which requires setuptools ≥ 77, which emits
`Metadata-Version: 2.4`. pip 22.0.2 predates that, reads the project name as
`unknown`, and rejects on the mismatch. The metadata itself is correct — only the
reader is too old. Note `name` is static in `pyproject.toml`; only `version` is
dynamic, so this isn't a dynamic-version issue.

Adding `--no-build-isolation` to the same command installs cleanly, as does
upgrading pip first. Either works, but an image build has to pin one deliberately
rather than discover this during a rollout.

Worth noting CI can't catch this: `test.sh` line 18 installs with
`--no-build-isolation` and line 17 pre-installs modern setuptools, so every CI
install path uses the flag that masks it. CI tests Ubuntu 22.04 / Python 3.10 and
passes while the documented command on that same platform doesn't work.

---

# List of improvements needed to integrate the SDK in a real robot system

The SDK starts when a dataset is finalized and a `ready.json` manifest exists, and
ends when bytes are durably accepted downstream. That scope is correct, and it is
also why integration is most of the work. Rather than list improvements abstractly,
here are situations that actually came up on the AGV I worked on, and what each one
would need.

## The robot crosses between access points

This happened constantly. The WiFi link dropped at every single handoff without
exception, and there were sections of the building where it simply did not work at
all — not a brief flap, minutes with no coverage.

This is the case the SDK is built for, and the design is right: spool locally, retry,
never block the control loop. But three things are missing before that holds up on a
real floor.

**Backpressure toward the recorder.** Inside a dead zone the spool cannot drain, yet
the robot keeps recording into it. Nothing signals back to the recorder to stop,
downsample or drop. The spool eventually refuses new data and the loss is silent —
no operator finds out until someone goes looking for the datasets. The SDK needs a
documented way to tell the producer "stop, I am full".

**Upload bandwidth control.** When coverage returns, the SDK will push the whole
backlog at once — and on an AGV that moment is usually while the robot is driving,
competing with teleoperation and control traffic for the same link. There is no
visible setting to bind uploads to a specific interface or cap their bandwidth. In
practice that means handling it outside the SDK with OS-level traffic shaping, or
only uploading while docked. Either would be easier as a configuration option.

**Spool reclamation.** `retention_seconds` is `0.0` and `last_cleanup` is null, so
nothing is ever cleaned up automatically. After a long stretch out of coverage the
backlog has grown, and once delivery succeeds those records stay on disk anyway.
Until the SDK reclaims space itself, every deployment needs its own cleanup timer —
plus a safe definition of "delivered" so nothing is removed before the far side has
durably acknowledged it.

## The robot docks itself when the battery runs low

On the deployment I worked on this was routine. Low battery, drive to the docking
station, shut down, charge. Several times a day, every day.

`fixes_v1.md` lists *"SIGTERM exceeds drain budget"* among its reliability items,
which reads like an edge case for unexpected shutdowns. On a robot that docks itself,
it is not an edge case — it is the normal end of every work block. Whatever is still
in the spool at that moment is at risk at the most predictable time of the day.

Two things would close this. First, **document the worst-case drain time**, because
without a number there is no way to set `TimeoutStopSec` in a systemd unit, and
systemd will SIGKILL whatever has not finished. Second, **a stated recovery
procedure**: after a robot dies mid-upload, what does an operator check, and what
confirms the backlog survived intact. The suite already covers the hard case — a
delivery whose outcome is unknown after the far side durably accepted it — so the
behaviour exists. What is missing is the operational note that tells a team what to
do with it.

While we are on the service: **no systemd unit ships at all.** Every integration
writes its own, and it has to start after the data partition is mounted, before
recording begins, restart on failure, and run as a user that can write the spool.
A reference unit file would take an afternoon and save every integrator the same
afternoon.

## A full shift, a new tag every two or three minutes

Eight hours is 480 minutes, so that pattern produces 160 to 240 recording events in a
single shift. That number runs straight into two ceilings that close off opposite
directions, as described above — discovery stopping at 100 manifests on one side, the
100 MiB per-file video limit on the other.

The part worth calling out separately is **how quietly the first one fails**.
Datasets past the hundredth are not rejected; they are never discovered. So
`rejected_handoffs` stays at zero and `deferred_handoffs` stays at zero, and every
counter the SDK exposes looks healthy while half a shift goes missing. Any monitoring
built on those counters alone will miss it entirely.

Until the ceiling moves, a deployment has to **count manifests produced against
manifests admitted from outside the SDK** and alert on the gap. It would be better
for the SDK to surface an undiscovered-manifest count itself, since it is the only
component that can see both sides.

## Getting data off the robot without me standing there

On that project I pulled the data off the robot myself, from the terminal. That works
for one robot being debugged by the person who built it. It does not work for a fleet
in a warehouse.

**The single biggest gap is a ROS 2 node that emits `ready.json`.** Nothing currently
bridges a recorder to the SDK. On a real robot there is no operator to run a command
when an episode ends, so the manifest has to be written by something that watches the
recorder's lifecycle — bag close, episode boundary — and writes it once the last byte
is flushed. `fixes_v1.md` lists this as planned. Until it exists, every team writes
the same node.

Alongside it, **the handoff contract needs documenting**. A manifest that becomes
visible a moment too early means the SDK admits a partial dataset. The rule is
probably "write to a temporary name, `fsync`, then `rename`", but an integrator
should not have to infer it from reading the source.

And for more than one robot, **the manifest needs to carry robot and mission
identity**. Ten AGVs uploading into one bucket produce datasets nobody can attribute
afterwards. This is a convention to settle before the first robot ships rather than
retrofit later.

## Actually sending the data somewhere

The stock transport reports ten missing capabilities, two of which — `deliver_artifact`
and `publish_revision` — are the operations the SDK exists to perform. The README says
this plainly, so it is a documented integration point rather than a defect. But it
does mean the transport is the largest piece of code an integrating team writes.

**A reference transport against ordinary object storage would be the highest-leverage
addition to this project.** Most teams are targeting S3, GCS or MinIO. One worked
implementation against any of them turns a week of work into a day, and it would also
serve as the executable specification of the ten capabilities.

Two smaller things in the same area: **credential delivery and rotation** is not
addressed anywhere, and secrets have to reach a robot somehow and rotate without
reflashing an image. And **`status` needs a path into fleet monitoring** — it emits a
complete JSON snapshot on demand, which is a good foundation, but what a fleet needs
is continuous metrics. A bridge from `status` to ROS 2 `diagnostic_msgs`, or a small
Prometheus exporter, would make the useful fields visible: `oldest_age_seconds` per
lane, `pending_bytes`, `retry_count`, `spool_bytes`.

## Putting it on the robot image in the first place

Robot images are commonly Ubuntu 22.04 with ROS 2 Humble, which is where the install
problem above bites — the documented command fails on that distribution's stock pip.
An image build has to pin either `--no-build-isolation` or a pip upgrade, chosen
deliberately rather than discovered during a rollout.

Two more at the same layer. The shipped scripts call `python` rather than `python3`,
and robot images rarely provide `/usr/bin/python`. And the wheel installs a package
literally named `src` alongside `vmodal_robot` — on an image where dozens of
unrelated packages share `site-packages`, that generic name will eventually collide
with something, and install order decides which one wins.

Finally, the spool root defaults to `/var/lib/vmodal-robot`, which an unprivileged
account cannot create. That belongs in the image build — a Dockerfile line, an Ansible
task, a package postinst — not a manual step someone has to remember on first boot.

---

## Verdict

Architecture is sound and the hard part is done. What's missing is scale and the
surrounding integration surface — every limit I hit is one a single 8-hour shift
crosses, and the input side has no automated producer while the output side has no
shipped transport.

Good fit for short bounded sessions: development, bench testing, a demo, one
recording run. Not for continuous operation. `fixes_v1.md` (dated 2026-10-10)
already names most of it.

Worth waiting for rather than rebuilding. I'd re-evaluate once the two ceilings move,
the drain budget is closed, and there's a reference transport to build against.

**Repo:** https://github.com/v-modal/vmodal_sdk_robotics/tree/main/lerobot