"""Reconcile immutable sensor references on qualified rational clock maps.

# Usage:
  from vmodal_robot.reconciliation.temporal import temporal_bundle
  result = temporal_bundle({"request": request, "profile": profile,
                            "clocks": clocks, "sources": sources})

All times are nanoseconds; source and map uncertainty are separate guaranteed
bounds. Source uncertainty is scaled into reference units. Request uncertainty
includes any target quantization. No timestamp is rounded or fitted here.
Episode and clock-epoch IDs are nonempty strings, including native episode "0".
The core validates supplied acquisition maps; callers qualify decoder, coordinate
and signal semantics. Audio across a transform boundary requires caller-inventoried
subchunks so every selected chunk has one exact sample-rate transform.
"""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass, field, asdict, replace
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

from fractions import Fraction
from vmodal_robot.contracts import ContractError


@dataclass(frozen=True)
class ClockMap:
    transform_id: str
    source_clock: str
    source_epoch: str
    reference_clock: str
    reference_epoch: str
    source_anchor_ns: int
    ref_anchor_ns: int
    valid_start_ns: int
    valid_end_ns: int
    scale_num: int = 1
    scale_den: int = 1
    uncertainty_ns: int = 0
    evidence: str = ""

    def __post_init__(self):
        for key in ("source_anchor_ns", "ref_anchor_ns", "valid_start_ns", "valid_end_ns", "scale_num", "scale_den", "uncertainty_ns"):
            if type(getattr(self, key)) is not int:
                raise ContractError(f"clock {key} must be an integer")
        if self.scale_num <= 0 or self.scale_den <= 0 or self.valid_end_ns < self.valid_start_ns or self.uncertainty_ns < 0:
            raise ContractError("invalid clock rate, interval or uncertainty")
        if not all(isinstance(v,str) and v for v in (self.transform_id, self.source_clock, self.source_epoch, self.reference_clock, self.reference_epoch, self.evidence)):
            raise ContractError("clock identity and qualification evidence required")


@dataclass(frozen=True)
class TemporalProfile:
    profile_id: str
    version: str
    required_modalities: Tuple[str, ...]
    budgets_ns: Dict[str, int]
    policies: Dict[str, str]
    max_age_ns: Dict[str, int] = field(default_factory=dict)
    max_gap_ns: Dict[str, int] = field(default_factory=dict)
    pairwise_budgets_ns: Dict[str, int] = field(default_factory=dict)
    causal: bool = False
    require_availability: bool = False
    audio_window_ns: Dict[str, int] = field(default_factory=dict)
    allow_reuse: bool = False
    tie_policy: str = "unresolved"
    interpolation_error_ns: Dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        if not all(isinstance(v,str) and v for v in (self.profile_id,self.version)) or self.tie_policy not in ("unresolved", "source_id"):
            raise ContractError("invalid temporal profile identity or tie policy")
        if not isinstance(self.required_modalities,(tuple,list)) or not self.required_modalities or any(not isinstance(v,str) or not v for v in self.required_modalities) or len(set(self.required_modalities)) != len(self.required_modalities):
            raise ContractError("required modalities must be nonempty unique string keys")
        for vals in (self.budgets_ns,self.policies,self.max_age_ns,self.max_gap_ns,self.pairwise_budgets_ns,self.audio_window_ns,self.interpolation_error_ns):
            if not isinstance(vals,dict) or any(not isinstance(k,str) or not k for k in vals):
                raise ContractError("profile policies and bounds require string-keyed mappings")
        if any(type(v) is not bool for v in (self.causal,self.require_availability,self.allow_reuse)):
            raise ContractError("profile causality, availability and reuse flags must be booleans")
        if any(p not in ("nearest", "causal", "interpolate", "hold", "window", "active", "issued", "supervised") for p in self.policies.values()):
            raise ContractError("unsupported temporal policy")
        for vals in (self.budgets_ns, self.max_age_ns, self.max_gap_ns, self.pairwise_budgets_ns, self.audio_window_ns, self.interpolation_error_ns):
            if any(type(v) is not int or v < 0 for v in vals.values()):
                raise ContractError("temporal bounds must be nonnegative integer nanoseconds")


@dataclass(frozen=True)
class TemporalRequest:
    sample_id: str
    revision: str
    episode: str
    reference_clock: str
    reference_epoch: str
    target_ns: int
    uncertainty_ns: int = 0
    window_start_ns: Optional[int] = None
    window_end_ns: Optional[int] = None
    source_ids: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if type(self.target_ns) is not int or type(self.uncertainty_ns) is not int or self.uncertainty_ns < 0:
            raise ContractError("request time and uncertainty must be integer nanoseconds")
        if not all(isinstance(v,str) and v for v in (self.sample_id, self.revision, self.episode, self.reference_clock, self.reference_epoch)):
            raise ContractError("request identity, string episode and reference epoch required")
        if (self.window_start_ns is None) != (self.window_end_ns is None):
            raise ContractError("window requires both boundaries")
        if self.window_start_ns is not None and (type(self.window_start_ns) is not int or type(self.window_end_ns) is not int or self.window_end_ns <= self.window_start_ns):
            raise ContractError("request window must be nonempty and half-open")
        if not isinstance(self.source_ids,dict) or any(not isinstance(k,str) or not k or not isinstance(v,str) or not v for k,v in self.source_ids.items()):
            raise ContractError("recorded source identities require string-keyed mappings")


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    revision: str
    episode: str
    modality: str
    kind: str
    clock: str
    epoch: str
    source_ns: int
    path: str = ""
    checksum: str = ""
    artifact_id: str = ""
    ordinal: int = 0
    uncertainty_ns: int = 0
    availability_ns: Optional[int] = None
    availability_clock: str = ""
    availability_epoch: str = ""
    availability_uncertainty_ns: int = 0
    valid: bool = True
    sequence: int = 0
    track: str = ""
    pts: Optional[int] = None
    time_base_num: int = 1
    time_base_den: int = 1
    exposure_meaning: str = "mid"
    exposure_duration_ns: int = 0
    reused: bool = False
    sample_rate: int = 0
    sample_count: int = 0
    origin_sample: int = 0
    channels: int = 1
    trim_start: int = 0
    trim_end: int = 0
    origin_known: bool = False
    trim_known: bool = False
    decoder: str = ""
    apply_start_ns: Optional[int] = None
    apply_end_ns: Optional[int] = None
    effective_end_ns: Optional[int] = None
    representation: str = ""

    def __post_init__(self):
        if self.kind not in ("video", "telemetry", "audio", "action"):
            raise ContractError("unsupported source modality kind")
        if not all(isinstance(v,str) and v for v in (self.source_id, self.revision, self.episode, self.modality, self.clock, self.epoch)):
            raise ContractError("source identity, string episode and clock epoch required")
        if any(not isinstance(v,str) for v in (self.path,self.checksum,self.artifact_id,self.availability_clock,self.availability_epoch)):
            raise ContractError("source references and availability clock must be strings")
        if bool(self.availability_clock) != bool(self.availability_epoch):
            raise ContractError("availability clock requires an explicit epoch")
        if not self.artifact_id and not (self.path and self.checksum):
            raise ContractError("source needs an artifact identity or original path and checksum")
        if type(self.source_ns) is not int or type(self.uncertainty_ns) is not int or self.uncertainty_ns < 0:
            raise ContractError("invalid source timestamp, ordinal or uncertainty")
        for key in ("ordinal", "sequence", "time_base_num", "time_base_den", "exposure_duration_ns", "sample_rate", "sample_count", "origin_sample", "channels", "trim_start", "trim_end", "availability_uncertainty_ns"):
            if type(getattr(self, key)) is not int:
                raise ContractError(f"source {key} must be an integer")
        if self.ordinal < 0 or self.sequence < 0 or self.availability_uncertainty_ns < 0:
            raise ContractError("source ordinal and sequence must be nonnegative")
        if any(type(v) is not bool for v in (self.valid,self.reused,self.origin_known,self.trim_known)):
            raise ContractError("source validity, reuse and audio-evidence flags must be booleans")
        for key in ("availability_ns", "apply_start_ns", "apply_end_ns", "effective_end_ns", "pts"):
            if getattr(self, key) is not None and type(getattr(self, key)) is not int:
                raise ContractError(f"source {key} must be integer nanoseconds or PTS")
        if self.time_base_num <= 0 or self.time_base_den <= 0 or self.exposure_duration_ns < 0:
            raise ContractError("invalid media time base or exposure")
        if self.kind == "video" and (self.pts is None or self.exposure_meaning not in ("start", "mid", "end")):
            raise ContractError("video requires integer PTS and exposure semantics")
        if self.kind in ("video","audio") and (not isinstance(self.track,str) or not self.track):
            raise ContractError("video and audio require an explicit nonempty media track")
        if self.kind == "audio" and (self.sample_rate <= 0 or self.sample_count <= 0 or self.channels <= 0 or not 0 <= self.origin_sample < self.sample_count or min(self.trim_start, self.trim_end) < 0 or self.trim_start + self.trim_end >= self.sample_count):
            raise ContractError("invalid audio rate, count, channels or trim")
        if self.apply_start_ns is not None and self.apply_end_ns is not None and self.apply_start_ns > self.apply_end_ns:
            raise ContractError("reversed action application interval")
        if self.apply_start_ns is not None and self.apply_start_ns < self.source_ns:
            raise ContractError("command application cannot precede issue time")


@dataclass(frozen=True)
class MappedTime:
    status: str
    time_ns: Optional[Fraction] = None
    uncertainty_ns: Fraction = Fraction(0)
    transform_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class TemporalSelection:
    modality: str
    kind: str
    status: str
    reason: str = ""
    sources: Tuple[SourceRecord, ...] = ()
    mapped_ns: Tuple[Fraction, ...] = ()
    transform_ids: Tuple[str, ...] = ()
    skew_ns: Fraction = Fraction(0)
    uncertainty_ns: Fraction = Fraction(0)
    error_bound_ns: Fraction = Fraction(0)
    weight: Optional[Fraction] = None
    sample_ranges: Tuple[Tuple[str, int, int], ...] = ()
    boundary_times_ns: Tuple[Tuple[Fraction, Fraction], ...] = ()
    coverage: Fraction = Fraction(0)
    reused: bool = False
    policy: str = ""
    effective_interval_ns: Optional[Tuple[Fraction, Fraction]] = None


@dataclass(frozen=True)
class TemporalResult:
    status: str
    selections: Dict[str, TemporalSelection]
    reasons: Tuple[str, ...]
    profile_id: str
    profile_version: str
    sample_id: str
    revision: str


def temporal_map(clock, source_ns, source_epoch):
    """Map an anchored difference exactly, with closed transform validity bounds."""
    if not isinstance(source_ns, (int, Fraction)) or isinstance(source_ns, bool):
        raise ContractError("source time must be integer or exact rational")
    if source_epoch != clock.source_epoch:
        return MappedTime("unresolved", reason="clock_epoch_mismatch")
    if not clock.valid_start_ns <= source_ns <= clock.valid_end_ns:
        return MappedTime("unresolved", reason="outside_transform_interval")
    val = Fraction(clock.ref_anchor_ns) + Fraction(clock.scale_num, clock.scale_den) * (source_ns - clock.source_anchor_ns)
    return MappedTime("valid", val, Fraction(clock.uncertainty_ns), clock.transform_id)


def _maps_validate(clocks):
    ids = {}
    for clock in clocks:
        if clock.transform_id in ids and clock != ids[clock.transform_id]:
            raise ContractError("conflicting clock transform identity")
        ids[clock.transform_id] = clock
    vals = sorted(ids.values(), key=lambda c: (c.source_clock, c.source_epoch, c.reference_clock, c.reference_epoch, c.valid_start_ns, c.transform_id))
    for left, right in zip(vals, vals[1:]):
        if (left.source_clock, left.source_epoch, left.reference_clock, left.reference_epoch) != (right.source_clock, right.source_epoch, right.reference_clock, right.reference_epoch):
            continue
        if right.valid_start_ns < left.valid_end_ns:
            raise ContractError("overlapping clock transform intervals")
        if right.valid_start_ns == left.valid_end_ns:
            a = temporal_map(left, right.valid_start_ns, left.source_epoch)
            b = temporal_map(right, right.valid_start_ns, right.source_epoch)
            if a.time_ns != b.time_ns:
                raise ContractError("discontinuous piecewise clock map within epoch")
    return vals


def _mapped(request, source, clocks, stamp=None):
    vals = [c for c in clocks if c.source_clock == source.clock and c.reference_clock == request.reference_clock]
    if not vals:
        return MappedTime("unresolved", reason="unknown_clock_relation")
    vals = [c for c in vals if c.source_epoch == source.epoch and c.reference_epoch == request.reference_epoch]
    if not vals:
        return MappedTime("unresolved", reason="clock_epoch_mismatch")
    time = source.source_ns if stamp is None else stamp
    vals = [c for c in vals if c.valid_start_ns <= time <= c.valid_end_ns]
    if not vals:
        return MappedTime("unresolved", reason="outside_transform_interval")
    # At a continuous piece boundary, choose a stable transform identity.
    clock = sorted(vals, key=lambda c: c.transform_id)[0]
    val = temporal_map(clock, time, source.epoch)
    unc = val.uncertainty_ns + Fraction(clock.scale_num, clock.scale_den) * source.uncertainty_ns
    return MappedTime("valid", val.time_ns, unc, val.transform_id)


def _sources(request, sources):
    ids = {}
    for src in sources:
        if src.source_id in ids and src != ids[src.source_id]:
            raise ContractError(f"conflicting source identity: {src.source_id}")
        ids[src.source_id] = src
    return sorted((s for s in ids.values() if s.revision == request.revision and s.episode == request.episode), key=lambda s: s.source_id)


def _interval_qualified(request, src, clocks, start, end):
    vals = sorted((c for c in clocks if c.source_clock == src.clock and c.source_epoch == src.epoch and c.reference_clock == request.reference_clock and c.reference_epoch == request.reference_epoch),key=lambda c:c.valid_start_ns)
    cursor = start
    for c in vals:
        if c.valid_end_ns < cursor:
            continue
        if c.valid_start_ns > cursor:
            return False
        cursor = c.valid_end_ns
        if cursor >= end:
            return True
    return False


def _fail(key, kind, reason, policy="", **details):
    return TemporalSelection(key, kind, "unresolved", reason, policy=policy, **details)


def _setup(request, sources, clocks, profile, kind):
    clocks = _maps_validate(clocks)
    vals = [s for s in _sources(request, sources) if s.kind == kind]
    keys = {s.modality for s in vals}
    if len(keys) > 1:
        raise ContractError("selector requires one modality key")
    key = next(iter(keys), "")
    if not key:
        return key, [], _fail(key, kind, "missing_modality")
    policy = profile.policies.get(key, "")
    if key not in profile.budgets_ns:
        return key, [], _fail(key, kind, "missing_budget", policy)
    rows, reasons = [], []
    for src in vals:
        val = _mapped(request, src, clocks)
        if val.status != "valid":
            if kind == "action" and policy == "active" and src.valid:
                # An unplaced competing command cannot be proven inactive.
                return key, [], _fail(key, kind, val.reason, policy)
            reasons.append(val.reason)
            continue
        if not src.valid:
            continue
        if profile.causal or policy in ("causal", "hold"):
            stamp = val.time_ns
            if src.kind == "video":
                clock = next(c for c in clocks if c.transform_id == val.transform_id)
                tail = {"start": Fraction(1), "mid": Fraction(1, 2), "end": Fraction(0)}[src.exposure_meaning]
                stamp += tail * src.exposure_duration_ns * Fraction(clock.scale_num, clock.scale_den)
            if kind not in ("audio", "action") and stamp + val.uncertainty_ns > request.target_ns - request.uncertainty_ns:
                reasons.append("future_evidence")
                continue
        if profile.require_availability:
            if src.availability_ns is None:
                reasons.append("availability_unknown")
                continue
            # Recorder delivery can use a different clock than acquisition.
            avail_src = replace(src,clock=src.availability_clock or src.clock,epoch=src.availability_epoch or src.epoch,uncertainty_ns=src.availability_uncertainty_ns)
            avail = _mapped(request, avail_src, clocks, src.availability_ns)
            if avail.status != "valid" or avail.time_ns + avail.uncertainty_ns > request.target_ns - request.uncertainty_ns:
                reasons.append("future_availability")
                continue
        if src.reused and not profile.allow_reuse:
            reasons.append("reuse_forbidden")
            continue
        rows.append((src, val))
    rows.sort(key=lambda item: (item[1].time_ns, item[0].source_id))
    return key, rows, None if rows else _fail(key, kind, sorted(reasons)[0] if reasons else "missing_modality", policy)


def _point(request, rows, profile, key, kind, policy):
    if policy not in ("nearest", "causal", "hold", "issued"):
        raise ContractError(f"unsupported {kind} point policy: {policy}")
    if policy in ("causal", "hold") or (kind == "action" and policy == "issued" and profile.causal):
        rows = [r for r in rows if r[1].time_ns + r[1].uncertainty_ns <= request.target_ns - request.uncertainty_ns]
    if not rows:
        return _fail(key, kind, "future_evidence", policy)
    chosen_id = request.source_ids.get(key)
    if chosen_id:
        rows = [r for r in rows if r[0].source_id == chosen_id]
        if not rows:
            return _fail(key, kind, "recorded_source_missing", policy)
    rows.sort(key=lambda r: (abs(r[1].time_ns - request.target_ns), r[0].source_id))
    src, val = rows[0]
    dist = abs(val.time_ns - request.target_ns)
    if not chosen_id and profile.tie_policy == "unresolved" and len(rows) > 1:
        # Distances overlap once source and common target bounds are included.
        if any(abs(other.time_ns-request.target_ns)-other.uncertainty_ns-request.uncertainty_ns <= dist+val.uncertainty_ns+request.uncertainty_ns for _,other in rows[1:]):
            return _fail(key, kind, "ambiguous_frame" if kind == "video" else "ambiguous_sample", policy,sources=tuple(r[0] for r in rows),mapped_ns=tuple(r[1].time_ns for r in rows),transform_ids=tuple(r[1].transform_id for r in rows))
    unc = val.uncertainty_ns + request.uncertainty_ns
    if dist + unc > profile.budgets_ns[key]:
        return _fail(key, kind, "uncertainty_exceeds_budget", policy)
    if key not in profile.max_age_ns:
        return _fail(key, kind, "missing_age_budget", policy)
    if dist + unc > profile.max_age_ns[key]:
        return _fail(key, kind, "stale_sample", policy)
    return TemporalSelection(key, kind, "valid", sources=(src,), mapped_ns=(val.time_ns,), transform_ids=(val.transform_id,), skew_ns=val.time_ns-request.target_ns, uncertainty_ns=unc, error_bound_ns=dist+unc, coverage=Fraction(1), reused=src.reused, policy=policy)


def temporal_video(request, frames, clocks, profile):
    key, rows, fail = _setup(request, frames, clocks, profile, "video")
    if fail:
        return fail
    policy = profile.policies.get(key, "")
    if policy not in ("nearest", "causal"):
        raise ContractError("unsupported video policy")
    return _point(request, rows, profile, key, "video", policy)


def temporal_telemetry(request, sources, clocks, profile):
    key, rows, fail = _setup(request, sources, clocks, profile, "telemetry")
    if fail:
        return fail
    policy = profile.policies.get(key, "")
    if policy != "interpolate":
        sel = _point(request, rows, profile, key, "telemetry", policy)
        if policy == "hold" and sel.status == "valid":
            for src in _sources(request,sources):
                if src.modality == key and not src.valid:
                    val = _mapped(request,src,clocks)
                    if val.status == "valid" and sel.mapped_ns[0] <= val.time_ns <= request.target_ns:
                        return _fail(key,"telemetry","telemetry_gap",policy,sources=sel.sources+(src,))
        return sel
    exact = [r for r in rows if r[1].time_ns == request.target_ns]
    if exact:
        return _point(request, rows, profile, key, "telemetry", "nearest")
    left = [r for r in rows if r[1].time_ns < request.target_ns]
    right = [r for r in rows if r[1].time_ns > request.target_ns]
    if not left or not right:
        return _fail(key, "telemetry", "future_evidence" if profile.causal else "telemetry_gap", policy)
    candidates_a = [r for r in left if r[1].time_ns == left[-1][1].time_ns]
    candidates_b = [r for r in right if r[1].time_ns == right[0][1].time_ns]
    if profile.tie_policy == "unresolved" and (len(candidates_a)>1 or len(candidates_b)>1):
        return _fail(key,"telemetry","ambiguous_sample",policy,sources=tuple(r[0] for r in candidates_a+candidates_b))
    a,b = min(candidates_a,key=lambda r:r[0].source_id),min(candidates_b,key=lambda r:r[0].source_id)
    if a[0].clock != b[0].clock or a[0].epoch != b[0].epoch:
        return _fail(key, "telemetry", "clock_epoch_mismatch", policy)
    if not _interval_qualified(request,a[0],clocks,a[0].source_ns,b[0].source_ns):
        return _fail(key,"telemetry","outside_transform_interval",policy)
    gap = b[1].time_ns - a[1].time_ns
    if key not in profile.max_gap_ns or key not in profile.interpolation_error_ns:
        return _fail(key, "telemetry", "missing_interpolation_budget", policy)
    if gap+a[1].uncertainty_ns+b[1].uncertainty_ns > profile.max_gap_ns[key]:
        return _fail(key, "telemetry", "telemetry_gap", policy)
    # Invalid measurements are barriers, rather than holes silently bridged.
    for src in _sources(request, sources):
        if src.modality == key and not src.valid:
            val = _mapped(request, src, clocks)
            if val.status == "valid" and a[1].time_ns <= val.time_ns <= b[1].time_ns:
                return _fail(key, "telemetry", "telemetry_gap", policy)
    unc = max(a[1].uncertainty_ns, b[1].uncertainty_ns) + request.uncertainty_ns + profile.interpolation_error_ns[key]
    if a[1].time_ns + a[1].uncertainty_ns >= b[1].time_ns - b[1].uncertainty_ns:
        return _fail(key, "telemetry", "ambiguous_sample", policy)
    if unc > profile.budgets_ns[key]:
        return _fail(key, "telemetry", "uncertainty_exceeds_budget", policy)
    weight = Fraction(request.target_ns-a[1].time_ns, gap)
    return TemporalSelection(key, "telemetry", "valid", sources=(a[0], b[0]), mapped_ns=(a[1].time_ns, b[1].time_ns), transform_ids=(a[1].transform_id,b[1].transform_id), uncertainty_ns=unc, error_bound_ns=unc, weight=weight, coverage=Fraction(1), policy=policy)


def _ceil(val):
    return -(-val.numerator // val.denominator)


def temporal_audio(request, chunks, clocks, profile):
    """Invert exact sample maps; never join across chunk gaps, overlaps or resets."""
    key, rows, fail = _setup(request, chunks, clocks, profile, "audio")
    if fail:
        return fail
    policy = profile.policies.get(key, "")
    if policy not in ("causal", "window"):
        raise ContractError("unsupported audio policy")
    if request.window_start_ns is None:
        width = profile.audio_window_ns.get(key)
        if not width:
            return _fail(key, "audio", "missing_window", policy)
        start, end = Fraction(request.target_ns-width), Fraction(request.target_ns)
    else:
        start, end = Fraction(request.window_start_ns), Fraction(request.window_end_ns)
    if (profile.causal or policy == "causal") and end > request.target_ns-request.uncertainty_ns:
        return _fail(key, "audio", "future_evidence", policy)
    spans, origins = [], []
    for src, val in rows:
        if not src.origin_known or not src.trim_known or not src.decoder:
            return _fail(key, "audio", "unknown_audio_origin", policy)
        clock = next(c for c in clocks if c.transform_id == val.transform_id)
        step = Fraction(1_000_000_000 * clock.scale_num, src.sample_rate * clock.scale_den)
        low, high = src.trim_start, src.sample_count-src.trim_end
        first = src.source_ns + Fraction((low-src.origin_sample)*1_000_000_000,src.sample_rate)
        last = src.source_ns + Fraction((high-src.origin_sample)*1_000_000_000,src.sample_rate)
        mfirst, mlast = _mapped(request,src,clocks,first), _mapped(request,src,clocks,last)
        if mfirst.status != "valid" or mlast.status != "valid" or mfirst.transform_id != mlast.transform_id:
            return _fail(key,"audio","outside_transform_interval",policy)
        a, b = mfirst.time_ns, mlast.time_ns
        if b <= start or a >= end:
            continue
        # Decoded sample cells cover the entire window; boundary overshoot is
        # separately bounded instead of reporting a fractional uncovered tail.
        first_index = Fraction(src.origin_sample)+(start-val.time_ns)/step
        i = max(low,first_index.numerator // first_index.denominator)
        j = min(high,_ceil(Fraction(src.origin_sample)+(end-val.time_ns)/step))
        if j <= i:
            continue
        sa, sb = val.time_ns+(i-src.origin_sample)*step,val.time_ns+(j-src.origin_sample)*step
        if (profile.causal or policy == "causal") and sb-step+val.uncertainty_ns > request.target_ns-request.uncertainty_ns:
            return _fail(key,"audio","future_evidence",policy)
        spans.append((a,b,src,val,i,j,sa,sb))
        origins.append((src.clock,src.epoch))
    if not spans:
        return _fail(key,"audio","audio_gap",policy)
    spans.sort(key=lambda s: (s[0],s[2].source_id))
    if len(set(origins)) != 1:
        return _fail(key,"audio","clock_epoch_mismatch",policy)
    cursor = start
    for a,b,*rest in spans:
        if a > cursor:
            return _fail(key,"audio","audio_gap",policy)
        if cursor > start and a < cursor:
            return _fail(key,"audio","audio_overlap",policy)
        cursor = b
    if cursor < end:
        return _fail(key,"audio","audio_gap",policy)
    unc = max(s[3].uncertainty_ns for s in spans)+request.uncertainty_ns
    boundary = max(abs(spans[0][6]-start),abs(spans[-1][7]-end))
    if boundary+unc > profile.budgets_ns[key]:
        return _fail(key,"audio","uncertainty_exceeds_budget",policy)
    covered = sum(max(Fraction(0),min(end,s[7])-max(start,s[6])) for s in spans)
    return TemporalSelection(key,"audio","valid",sources=tuple(s[2] for s in spans),mapped_ns=tuple(s[3].time_ns for s in spans),transform_ids=tuple(s[3].transform_id for s in spans),uncertainty_ns=unc,error_bound_ns=boundary+unc,sample_ranges=tuple((s[2].source_id,s[4],s[5]) for s in spans),boundary_times_ns=tuple((s[6],s[7]) for s in spans),coverage=covered/(end-start),policy=policy)


def temporal_action(request, commands, clocks, profile):
    key, rows, fail = _setup(request, commands, clocks, profile, "action")
    if fail:
        return fail
    policy = profile.policies.get(key, "")
    if policy == "issued":
        return _point(request,rows,profile,key,"action",policy)
    if policy not in ("active", "supervised"):
        raise ContractError("unsupported action policy")
    candidates, reasons = [], []
    for src,val in rows:
        if policy == "active":
            # A command definitely issued in the future cannot be active yet.
            if val.time_ns-val.uncertainty_ns > request.target_ns+request.uncertainty_ns:
                continue
        if src.apply_start_ns is None or src.apply_end_ns is None or src.effective_end_ns is None:
            if policy == "active":
                if src.effective_end_ns is not None:
                    expiry = _mapped(request,src,clocks,src.effective_end_ns)
                    if expiry.status == "valid" and expiry.time_ns+expiry.uncertainty_ns <= request.target_ns-request.uncertainty_ns:
                        continue
                return _fail(key,"action","action_apply_unknown",policy)
            reasons.append("action_apply_unknown")
            continue
        if not _interval_qualified(request,src,clocks,src.apply_start_ns,src.effective_end_ns):
            if policy == "active":
                return _fail(key,"action","outside_transform_interval",policy)
            reasons.append("outside_transform_interval")
            continue
        a,b,e = (_mapped(request,src,clocks,t) for t in (src.apply_start_ns,src.apply_end_ns,src.effective_end_ns))
        if any(v.status != "valid" for v in (a,b,e)):
            if policy == "active":
                return _fail(key,"action","outside_transform_interval",policy)
            reasons.append("outside_transform_interval")
            continue
        if e.time_ns <= b.time_ns:
            raise ContractError("action effective end must follow latest application")
        unc = max(a.uncertainty_ns,b.uncertainty_ns,e.uncertainty_ns)+request.uncertainty_ns
        if policy == "active":
            if a.time_ns-unc <= request.target_ns < b.time_ns+unc:
                return _fail(key,"action","action_apply_unknown",policy)
            if b.time_ns+unc <= request.target_ns and e.time_ns-unc <= request.target_ns < e.time_ns+unc:
                return _fail(key,"action","action_effective_end_unknown",policy)
            eligible = b.time_ns+unc <= request.target_ns < e.time_ns-unc
        else:
            if profile.causal:
                return _fail(key,"action","future_evidence",policy)
            eligible = a.time_ns >= request.target_ns
        if not eligible:
            continue
        apply_unc = b.time_ns-a.time_ns+unc
        if apply_unc > profile.budgets_ns[key]:
            if policy == "active":
                return _fail(key,"action","uncertainty_exceeds_budget",policy)
            reasons.append("uncertainty_exceeds_budget")
            continue
        candidates.append((src,val,b,e,apply_unc))
    if not candidates:
        return _fail(key,"action",sorted(reasons)[0] if reasons else "no_active_action",policy)
    if policy == "supervised":
        candidates.sort(key=lambda c:(c[2].time_ns,c[0].source_id))
        candidates=[c for c in candidates if c[2].time_ns == candidates[0][2].time_ns]
    if len(candidates)>1:
        return _fail(key,"action","ambiguous_action",policy)
    src,val,b,e,unc = candidates[0]
    skew = Fraction(0) if policy == "active" else b.time_ns-request.target_ns
    if abs(skew)+unc > profile.budgets_ns[key]:
        return _fail(key,"action","uncertainty_exceeds_budget",policy)
    return TemporalSelection(key,"action","valid",sources=(src,),mapped_ns=(val.time_ns,),transform_ids=(val.transform_id,b.transform_id,e.transform_id),skew_ns=skew,uncertainty_ns=unc,error_bound_ns=abs(skew)+unc,coverage=Fraction(1),policy=policy,effective_interval_ns=(b.time_ns,e.time_ns))


def temporal_reconcile(request, sources, clocks, profile):
    """Select each declared modality, then enforce required and pairwise budgets."""
    vals = _sources(request,sources)
    clocks = _maps_validate(clocks)
    keys = sorted(set(profile.required_modalities) | set(profile.policies) | {s.modality for s in vals})
    selectors = {"video":temporal_video,"telemetry":temporal_telemetry,"audio":temporal_audio,"action":temporal_action}
    selected, reasons = {}, []
    for key in keys:
        rows = [s for s in vals if s.modality == key]
        kinds = {s.kind for s in rows}
        if len(kinds)>1:
            raise ContractError("modality key has conflicting kinds")
        if not rows:
            sel = _fail(key,"","missing_modality",profile.policies.get(key,""))
        else:
            sel = selectors[rows[0].kind](request,rows,clocks,profile)
        if sel.status != "valid" and not sel.sources:
            sel = replace(sel,sources=tuple(rows))
        selected[key]=sel
        if key in profile.required_modalities and sel.status != "valid":
            reasons.append(f"{key}:{sel.reason}")
    for pair,budget in sorted(profile.pairwise_budgets_ns.items()):
        parts=pair.split("|")
        if len(parts)!=2 or parts[0]==parts[1]:
            raise ContractError("pairwise budget key must be modalityA|modalityB")
        a,b=(selected.get(key) for key in parts)
        if a is None or b is None or a.status != "valid" or b.status != "valid":
            reasons.append(f"{pair}:pairwise_selection_missing")
        # The same target appears in both signed skews and cancels. Each
        # selection uncertainty includes it once, so remove both copies here.
        elif abs(a.skew_ns-b.skew_ns)+max(Fraction(0),a.uncertainty_ns-request.uncertainty_ns)+max(Fraction(0),b.uncertainty_ns-request.uncertainty_ns) > budget:
            reasons.append(f"{pair}:pairwise_budget_exceeded")
    return TemporalResult("unresolved" if reasons else "valid",selected,tuple(sorted(reasons)),profile.profile_id,profile.version,request.sample_id,request.revision)


def temporal_bundle(bundle):
    """JSON adapter shared by producer and workstation; rationals stay lossless."""
    def encode(val):
        if isinstance(val,Fraction):
            return {"numerator":val.numerator,"denominator":val.denominator}
        if isinstance(val,dict):
            return {key:encode(item) for key,item in val.items()}
        if isinstance(val,(list,tuple)):
            return [encode(item) for item in val]
        return val
    profile = dict(bundle["profile"])
    profile["required_modalities"] = tuple(profile["required_modalities"])
    result = temporal_reconcile(TemporalRequest(**bundle["request"]),[SourceRecord(**src) for src in bundle["sources"]],[ClockMap(**clock) for clock in bundle["clocks"]],TemporalProfile(**profile))
    return encode(asdict(result))
