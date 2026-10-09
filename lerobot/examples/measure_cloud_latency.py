"""Offline trace accounting and explicitly invoked SDK-only live qualification.

The live path measures gateway operations; it cannot qualify robot admission,
generic revision publication, or durable storage from a gateway success alone.
"""

from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

import asyncio
import math
import platform
import time
import uuid

if __package__:
    from .search_acceptance import accept_hit
else:
    from search_acceptance import accept_hit
from vmodal_robot.utils import os_json_load, os_json_save


STAGES = {
    "capture_buffering": ("capture", "finalized"),
    "handoff": ("finalized", "manifest_ready"),
    "admission": ("manifest_ready", "accepted"),
    "queue_residence": ("accepted", "first_attempt"),
    "retry_residence": ("first_attempt", "upload_start"),
    "upload": ("upload_start", "durable_ack"),
    "gateway_upload": ("upload_start", "gateway_upload_returned"),
    "revision_publication": ("last_artifact_ack", "revision_published"),
    "index_processing": ("index_submit", "index_ready"),
    "search_visibility": ("index_ready", "first_verified_retrieval"),
    "query_decision": ("query_start", "decision"),
    "end_to_end": ("capture", "accepted_decision"),
}
VERIFIED = {
    "durable_ack": "durable_ack_verified", "last_artifact_ack": "durable_ack_verified",
    "revision_published": "revision_published_verified", "index_ready": "index_ready_verified",
    "first_verified_retrieval": "search_verified", "accepted_decision": "accepted_verified",
}
REQUIRED = set(STAGES) - {"gateway_upload", "retry_residence"}


def finite(value):
    return type(value) in (int, float) and abs(value) <= sys.float_info.max and math.isfinite(value)


def summarize(values):
    """Observed percentiles only; failed/missing samples are counted separately."""
    values = sorted(values)
    if not values:
        return dict(n=0, p50=None, p95=None, p99=None, max=None)

    def percentile(frac):
        pos = (len(values) - 1) * frac
        low = math.floor(pos)
        high = math.ceil(pos)
        return values[low] + (values[high] - values[low]) * (pos - low)

    return dict(n=len(values), p50=percentile(.5), p95=percentile(.95),
                p99=percentile(.99), max=values[-1])


def account_run(run):
    """All event times must share one monotonic clock domain, in seconds."""
    if not run.get("run_id") or not run.get("clock_domain") or not run.get("correlation"):
        raise ValueError("run_id, clock_domain and correlated identity are required")
    events = run.get("events", {})
    if any(not finite(value) for value in events.values()):
        raise ValueError("event times must be finite monotonic seconds")
    measured, missing = {}, {}
    for name, (start, end) in STAGES.items():
        absent = [key for key in (start, end) if key not in events
                  or (key in VERIFIED and run.get(VERIFIED[key]) is not True)]
        if absent:
            missing[name] = absent
            continue
        elapsed = events[end] - events[start]
        if elapsed < 0:
            raise ValueError(f"{name}: timestamps are reversed")
        measured[name] = elapsed
    freshness = None
    capture = run.get("capture_wall_time")
    decision = run.get("decision_wall_time")
    uncertainty = run.get("clock_uncertainty_seconds")
    if (run.get("clock_mapping_qualified") is True and run.get("clock_mapping_record")
            and run.get("accepted_verified") is True
            and all(finite(value) for value in (capture, decision, uncertainty))
            and uncertainty >= 0 and decision - capture >= -uncertainty):
        freshness = dict(seconds=decision - capture,
                         lower_seconds=max(0, decision - capture - uncertainty),
                         upper_seconds=max(0, decision - capture + uncertainty),
                         uncertainty_seconds=uncertainty)
    size = run.get("bytes")
    throughput = None
    if finite(size) and size >= 0 and measured.get("upload", 0) > 0:
        throughput = size / measured["upload"]
    return dict(run=run, stages_seconds=measured, missing_stages=missing,
                freshness=freshness, upload_bytes_per_second=throughput)


def build_report(runs, metadata, deadline_seconds=None, max_clock_uncertainty_seconds=None):
    rows = [account_run(run) for run in runs]
    failures = sum(row["run"].get("status") != "success" for row in rows)
    timeouts = sum(row["run"].get("status") == "timeout" for row in rows)
    stats = {}
    for name in STAGES:
        values = [row["stages_seconds"][name] for row in rows if name in row["stages_seconds"]]
        stats[name] = dict(summarize(values), missing=len(rows) - len(values))
    fresh = [row["freshness"]["upper_seconds"] for row in rows if row["freshness"] is not None]
    stats["freshness_upper"] = dict(summarize(fresh), missing=len(rows) - len(fresh))
    requests = [entry for run in runs for entry in run.get("requests", [])]
    request_stats = {}
    for name in {entry["operation"] for entry in requests}:
        entries = [entry for entry in requests if entry["operation"] == name]
        elapsed = [entry["elapsed_seconds"] for entry in entries
                   if entry.get("status") == "success" and finite(entry.get("elapsed_seconds"))]
        request_stats[name] = dict(summarize(elapsed), attempts=len(entries),
                                  failures=sum(entry.get("status") != "success" for entry in entries))
    qualified_budget = (finite(deadline_seconds) and deadline_seconds >= 0
                        and finite(max_clock_uncertainty_seconds) and max_clock_uncertainty_seconds >= 0)
    complete = bool(rows) and all(not (REQUIRED & set(row["missing_stages"]))
                                 and row["freshness"] is not None for row in rows)
    suitability = "UNKNOWN"
    if qualified_budget and complete:
        suitability = "OBSERVED_WITHIN_BUDGET" if failures == 0 and all(
            row["freshness"]["upper_seconds"] <= deadline_seconds
            and row["freshness"]["uncertainty_seconds"] <= max_clock_uncertainty_seconds
            and row["stages_seconds"]["end_to_end"] <= deadline_seconds for row in rows
        ) else "OBSERVED_UNSUITABLE"
    return dict(schema_version=1, metadata=metadata, n=len(rows), failures=failures, timeouts=timeouts,
                failure_rate=failures / len(rows) if rows else None,
                timeout_rate=timeouts / len(rows) if rows else None,
                stages_seconds=stats, deadline_seconds=deadline_seconds,
                request_seconds=request_stats,
                max_clock_uncertainty_seconds=max_clock_uncertainty_seconds,
                suitability=suitability, runs=rows,
                limitation="Observed samples do not certify deterministic latency or cloud actuation.")


def report(trace_file, output):
    data = os_json_load(trace_file)
    result = build_report(data["runs"], data["metadata"], data.get("deadline_seconds"),
                          data.get("max_clock_uncertainty_seconds"))
    os_json_save(output, result)
    return dict(output=output, n=result["n"], suitability=result["suitability"])


def known_event(hit, config):
    """Match qualified source identity AND a labeled source-time interval."""
    expected = config["expected_hit"]
    stamp = hit.get(config["event_time_field"])
    start, end = config["event_interval"]
    return (bool(expected) and all(hit.get(key) == value for key, value in expected.items())
            and finite(stamp) and start <= stamp <= end)


async def measure_sdk(client, config, run):
    events = run["events"]

    async def request(name, operation):
        entry = dict(operation=name, start=time.monotonic(), status="pending")
        run["requests"].append(entry)
        try:
            result = (await operation).model_dump(mode="json")
            entry.update(status="success", response=result)
            return result
        except (Exception, asyncio.CancelledError) as exc:
            entry.update(status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                         error=type(exc).__name__)
            raise
        finally:
            entry["elapsed_seconds"] = time.monotonic() - entry["start"]

    events["upload_start"] = time.monotonic()
    upload = dict(config["upload"], max_part_attempts=1, max_concurrency=1)
    run["upload_response"] = await request("upload", client.collections.video_upload(**upload))
    events["gateway_upload_returned"] = time.monotonic()
    events["index_submit"] = time.monotonic()
    submit = await request("index_submit", client.indexes.create_index(**config["index"]))
    job_id = submit.get("job_id")
    if not job_id:
        raise ValueError("index submission returned no correlated job_id")
    run["correlation"]["index_job_id"] = job_id
    while True:
        status = await request("index_status", client.indexes.index_status(job_id=job_id))
        if status.get("job_id") != job_id:
            raise ValueError("index status belongs to another job")
        if status.get("status") in config["index_failed_states"]:
            run.update(status="failed", failure="indexing failed")
            return
        if status.get("status") in config["index_ready_states"]:
            events["index_ready"] = time.monotonic()
            run["index_ready_verified"] = True
            break
        await asyncio.sleep(config["poll_seconds"])
    while True:
        events["query_start"] = time.monotonic()
        response = await request("search", client.searches.search_video(**config["search"]))
        for hit in response.get("data", []):
            if not known_event(hit, config):
                continue
            if "first_verified_retrieval" not in events:
                events["first_verified_retrieval"] = time.monotonic()
            run["search_verified"] = True
            evidence = dict(config["evidence"], capture_wall_time=config["source_wall_anchor"]
                            + hit[config["event_time_field"]] * config["event_time_unit_seconds"])
            decision = accept_hit(hit, config["scope"], config["profile"], evidence, time.time())
            events["decision"] = time.monotonic()
            run["decision"] = decision
            if decision["status"] == "ACCEPTED":
                run.update(status="success", accepted_verified=True, decision_wall_time=time.time())
                events["accepted_decision"] = events["decision"]
                return
            run.update(status="failed", failure="known retrieval did not pass acceptance")
            return
        await asyncio.sleep(config["poll_seconds"])


def validate_live(config):
    for key in ("upload", "index", "search", "scope", "profile", "evidence", "environment", "expected_hit"):
        if not isinstance(config.get(key), dict):
            raise ValueError(f"{key} must be a JSON object")
    group = config["upload"]["collection_name"]
    if config.get("disposable") is not True or not isinstance(group, str) or not group.startswith("robot-latency-"):
        raise ValueError("explicit disposable robot-latency-* collection required")
    if config["index"]["group_name"] != group or config["search"]["group_name"] != group:
        raise ValueError("upload, index and search must target the same disposable collection")
    if len({config[section].get("mode", "vid_file") for section in ("upload", "index", "search")}) != 1:
        raise ValueError("upload, index and search modes must agree")
    stream = config["upload"]["sub_collection_name"]
    if config["index"].get("stream_name") != stream or config["search"].get("stream_name") != stream:
        raise ValueError("upload, index and search must target the same camera stream")
    if config["upload"].get("reduce_size") or config["upload"].get("start_datetime_user"):
        raise ValueError("preserve native bytes and source-relative clocks")
    for key in ("timeout_seconds", "poll_seconds"):
        if not finite(config[key]) or config[key] <= 0:
            raise ValueError(f"{key} must be positive finite seconds")
    if not config.get("expected_hit") or not config.get("event_time_field"):
        raise ValueError("qualified known-source fields and event_time_field required")
    start, end = config["event_interval"]
    if not finite(start) or not finite(end) or end < start:
        raise ValueError("finite ordered event interval required in wire time units")
    if not config.get("index_ready_states") or not config.get("readiness_record"):
        raise ValueError("documented qualified index-ready states required")
    for key in ("index_ready_states", "index_failed_states"):
        if not isinstance(config.get(key), list) or any(not isinstance(value, str) or not value for value in config[key]):
            raise ValueError(f"{key} must be a list of explicit wire state strings")
    if set(config["index_ready_states"]) & set(config["index_failed_states"]):
        raise ValueError("ready and failed states must be disjoint")
    if not config.get("wire_mapping_record"):
        raise ValueError("known source/time wire mapping must be qualified")
    if (not finite(config.get("source_wall_anchor")) or not finite(config.get("capture_source_time"))
            or not finite(config.get("event_time_unit_seconds")) or config["event_time_unit_seconds"] <= 0):
        raise ValueError("explicit source wall anchor, capture source time and wire time units required")
    if not finite(config["source_wall_anchor"] + config["capture_source_time"] * config["event_time_unit_seconds"]):
        raise ValueError("mapped capture time must be finite")


def live(config_file, output, live=False):
    """Explicit upload/index/search subset; no deletion of retained qualification evidence."""
    if live is not True:
        raise ValueError("pass --live=True to explicitly invoke cloud operations")
    config = os_json_load(config_file)
    validate_live(config)
    from vmodal import Client, SdkConfig, __version__
    from vmodal.errors import SdkError

    # Disable SDK retries so measured request attempts are not hidden.
    cfg = SdkConfig.from_env(resolve_identity=False, max_retries=0,
                             timeout=config["timeout_seconds"])
    run = dict(run_id=uuid.uuid4().hex, clock_domain="client_monotonic", events={},
               correlation=dict(collection=config["upload"]["collection_name"],
                                expected_hit=config["expected_hit"]), status="pending",
               bytes=os.path.getsize(config["upload"]["filepath_local"]), retries=0, requests=[])
    for key in ("capture_wall_time", "clock_uncertainty_seconds", "clock_mapping_qualified", "clock_mapping_record"):
        run[key] = config["evidence"].get(key)
    run["capture_wall_time"] = (config["source_wall_anchor"]
                                + config["capture_source_time"] * config["event_time_unit_seconds"])

    async def execute():
        async with Client(cfg=cfg) as client:
            try:
                await asyncio.wait_for(measure_sdk(client, config, run), config["timeout_seconds"])
            except asyncio.TimeoutError:
                run.update(status="timeout", failure="overall upload/index/visibility deadline")
            except SdkError as exc:
                run.update(status="failed", failure=type(exc).__name__, status_code=exc.status_code)
            except ValueError as exc:
                run.update(status="failed", failure=str(exc))
                raise
            finally:
                for entry in run["requests"]:
                    if entry["status"] == "pending":
                        entry["status"] = run["status"]

    try:
        asyncio.run(execute())
    finally:
        metadata = dict(config["environment"], sdk_version=__version__, python_version=platform.python_version(),
                        platform=platform.platform(), readiness_record=config["readiness_record"],
                        wire_mapping_record=config["wire_mapping_record"], scope=config["scope"],
                        qualification="gateway subset; robot handoff/durable receipt/revision unmeasured")
        result = build_report([run], metadata, config.get("deadline_seconds"),
                              config.get("max_clock_uncertainty_seconds"))
        os_json_save(output, result)
    return dict(output=output, status=run["status"], suitability=result["suitability"])


if __name__ == '__main__':
    fire.Fire({"report": report, "live": live})
