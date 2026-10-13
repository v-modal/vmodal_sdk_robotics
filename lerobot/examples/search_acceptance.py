"""Offline consumer policy example; qualification belongs to the consumer owner."""

from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

import math


SCOPE_FIELDS = (
    "collection", "model", "metric", "direction", "preprocessing",
    "camera_domain", "index_version", "sampling_policy", "query_kind",
    "search_combine_mode",
)


def accept_hit(hit: Dict[str, Any], scope: Dict[str, Any], profile: Dict[str, Any],
               evidence: Dict[str, Any], now_wall: float) -> Dict[str, Any]:
    """Evaluate one SDK model_dump() hit using qualified per-hit wire/clock evidence.

    Scope identifies the resolved collection/index/model, not request defaults.
    capture_wall_time is Unix seconds mapped from this hit's source capture time;
    clock_uncertainty_seconds bounds the total capture-to-now clock error.
    """
    result = {
        "status": "UNKNOWN", "reason": "unqualified profile",
        "raw_hit": hit, "scope": scope, "evidence": evidence,
        "calibration_version": None,
    }
    if not all(isinstance(value, dict) for value in (hit, scope, profile, evidence)):
        result["reason"] = "invalid policy input; expected JSON objects"
        return result
    result["calibration_version"] = profile.get("calibration_version")

    def finish(status, reason):
        result.update(status=status, reason=reason)
        return result

    def finite(value):
        return type(value) in (int, float) and abs(value) <= sys.float_info.max and math.isfinite(value)

    if (type(profile.get("schema_version")) is not int or profile["schema_version"] != 1
            or profile.get("qualified") is not True):
        return result
    if not profile.get("calibration_version") or not profile.get("calibration_record"):
        return finish("UNKNOWN", "missing held-out calibration record")
    budget = profile.get("false_accept_budget")
    if not finite(budget) or not 0 <= budget <= 1 or profile.get("consumer_approved") is not True:
        return finish("UNKNOWN", "missing consumer-approved false-accept budget")
    expected = profile.get("scope", {})
    if not isinstance(expected, dict) or any(expected.get(key) in (None, "") for key in SCOPE_FIELDS):
        return finish("UNKNOWN", "incomplete profile scope")
    if any(scope.get(key) in (None, "") for key in SCOPE_FIELDS):
        return finish("UNKNOWN", "incomplete observed scope")
    if any(scope[key] != expected[key] for key in SCOPE_FIELDS):
        return finish("REJECTED", "scope changed; recalibration required")
    if expected["direction"] != "lower_is_closer":
        return finish("UNKNOWN", "distance ceiling requires qualified lower-is-closer metric")
    ceiling = profile.get("max_distance")
    freshness = profile.get("freshness_seconds")
    uncertainty_max = profile.get("max_clock_uncertainty_seconds")
    if not all(finite(value) and value >= 0 for value in (ceiling, freshness, uncertainty_max)):
        return finish("UNKNOWN", "invalid distance/freshness/uncertainty budget")
    field = profile.get("distance_field")
    if (not isinstance(field, str) or not field or evidence.get("distance_field") != field
            or evidence.get("raw_distance_qualified") is not True
            or not evidence.get("wire_record")):
        return finish("UNKNOWN", "raw distance field not qualified for this hit")
    if not hit:
        return finish("UNKNOWN", "empty result")
    distance = hit.get(field)
    if distance is None:
        return finish("UNKNOWN", "missing raw distance")
    if not finite(distance):
        return finish("REJECTED", "invalid raw distance")
    if distance > ceiling:
        return finish("REJECTED", "raw distance exceeds absolute ceiling")
    if (evidence.get("clock_mapping_qualified") is not True
            or not evidence.get("clock_mapping_record")):
        return finish("UNKNOWN", "capture wall-time mapping not qualified for this hit")
    capture = evidence.get("capture_wall_time")
    uncertainty = evidence.get("clock_uncertainty_seconds")
    if not finite(capture) or not finite(now_wall) or not finite(uncertainty) or uncertainty < 0:
        return finish("UNKNOWN", "missing/invalid capture time or clock uncertainty")
    if uncertainty > uncertainty_max:
        return finish("UNKNOWN", "clock uncertainty exceeds qualified budget")
    age = now_wall - capture
    result.update(raw_distance=distance, age_seconds=age, age_upper_seconds=age + uncertainty)
    if age < -uncertainty:
        return finish("UNKNOWN", "capture lies in future beyond clock uncertainty")
    if age + uncertainty > freshness:
        return finish("REJECTED", "capture exceeds freshness budget")
    return finish("ACCEPTED", "qualified distance and freshness within budgets")


if __name__ == '__main__':
    fire.Fire()
