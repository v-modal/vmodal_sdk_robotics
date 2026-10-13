"""Plain-assert temporal contract checks, including the complete worked sample.

# Usage:
  python -m tests.test_temporal test_all
"""
from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass, replace, asdict
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning
from src.utils.util_base import os_makedirs, os_path_cleanup

from fractions import Fraction
from vmodal_robot.contracts import ContractError
from vmodal_robot.reconciliation.temporal import ClockMap, SourceRecord, TemporalRequest, TemporalProfile
from vmodal_robot.reconciliation.temporal import temporal_map, temporal_video, temporal_telemetry, temporal_audio, temporal_action, temporal_reconcile, temporal_bundle
from tests.utils_datafake import pd_fake_temporal
from tests.utils_temporal_fake import ContractRaises

dirtest = "ztmp/ztests"


def test1():
    req,src,clocks,profile = pd_fake_temporal()
    result = temporal_reconcile(req,src,clocks,profile)
    assert result.status == "valid", result.reasons
    cam = result.selections["camera"]
    assert cam.skew_ns == -2_000_000
    assert cam.error_bound_ns == 2_800_000
    assert temporal_video(replace(req,uncertainty_ns=200_000),[src[0]],clocks,profile).error_bound_ns==3_000_000
    assert cam.sources[0].pts == 2940
    assert result.selections["state"].weight == Fraction(2,5)
    assert [s.ordinal for s in result.selections["state"].sources] == [10,11]
    audio = result.selections["sound"]
    assert audio.sample_ranges == (("sound-0",3840,4800),)
    assert audio.coverage == 1
    assert result.selections["action"].sources[0].source_id == "command-0"
    assert temporal_reconcile(req,list(reversed(src))+[src[0]],clocks,profile) == result
    bundle = dict(request=asdict(req),sources=[asdict(s) for s in src],clocks=[asdict(c) for c in clocks],profile=asdict(profile))
    recovered = temporal_bundle(bundle)
    assert recovered["selections"]["state"]["weight"] == {"numerator":2,"denominator":5}
    assert temporal_bundle(bundle) == recovered


def test2():
    req,src,clocks,profile = pd_fake_temporal()
    anchor = 1_700_000_000_000_000_000
    clock = replace(clocks[0],source_anchor_ns=anchor,ref_anchor_ns=anchor+7,valid_start_ns=anchor,valid_end_ns=anchor+1_000_000,scale_num=1_000_001,scale_den=1_000_000)
    result = temporal_map(clock,anchor+500_000,"epoch-0")
    assert result.time_ns == Fraction(2*(anchor+500_007)+1,2)
    assert temporal_map(clock,anchor-1,"epoch-0").reason == "outside_transform_interval"
    assert temporal_map(clock,anchor,"reset-1").reason == "clock_epoch_mismatch"
    with ContractRaises(ContractError,"invalid clock rate"):
        replace(clock,scale_num=0)
    with ContractRaises(ContractError,"must be an integer"):
        replace(clock,source_anchor_ns=float(anchor))
    with ContractRaises(ContractError,"qualification evidence"):
        replace(clock,evidence="")
    assert temporal_video(req,[src[0]],[],profile).reason == "unknown_clock_relation"
    assert temporal_video(req,[replace(src[0],epoch="reset-1")],clocks,profile).reason == "clock_epoch_mismatch"
    assert temporal_video(req,[replace(src[0],episode="other")],clocks,profile).reason == "missing_modality"


def test3():
    req,src,clocks,profile = pd_fake_temporal()
    req = replace(req,uncertainty_ns=0)
    left=replace(src[0],uncertainty_ns=0)
    right=replace(left,source_id="frame-1",source_ns=102_000_000,pts=3060)
    assert temporal_video(req,[left,right],clocks,profile).reason == "ambiguous_frame"
    assert temporal_video(req,[left,right],clocks,replace(profile,tie_policy="source_id")).sources==(left,)
    assert temporal_video(replace(req,source_ids={"camera":"frame-1"}),[left,right],clocks,profile).sources==(right,)
    assert temporal_video(req,[left,right],clocks,replace(profile,causal=True)).sources==(left,)
    assert temporal_video(req,[right],clocks,replace(profile,causal=True)).reason == "future_evidence"
    overlap=replace(right,source_ns=103_000_000,uncertainty_ns=2_000_000)
    assert temporal_video(req,[left,overlap],clocks,profile).reason == "ambiguous_frame"
    near=replace(right,source_ns=104_000_000)
    far=replace(right,source_id="uncertain-far-frame",source_ns=200_000_000,uncertainty_ns=100_000_000)
    assert temporal_video(req,[left,near,far],clocks,profile).reason=="ambiguous_frame"
    assert temporal_video(req,[replace(left,reused=True)],clocks,profile).reason == "reuse_forbidden"
    assert temporal_video(req,[replace(left,availability_ns=102_000_000)],clocks,replace(profile,require_availability=True)).reason == "future_availability"
    hostclock=replace(clocks[0],transform_id="host-map",source_clock="host",ref_anchor_ns=10_000_000)
    arrived=replace(left,availability_ns=91_000_000,availability_clock="host",availability_epoch="epoch-0")
    assert temporal_video(req,[arrived],clocks+[hostclock],replace(profile,require_availability=True)).reason=="future_availability"
    assert temporal_video(req,[replace(left,source_ns=99_000_000,exposure_meaning="start",exposure_duration_ns=4_000_000)],clocks,replace(profile,causal=True)).reason == "future_evidence"
    # A per-camera causal policy also excludes exposures finishing after target.
    camera_causal = replace(profile, policies={"camera": "causal"})
    exposed = replace(left, source_ns=99_000_000, exposure_meaning="start", exposure_duration_ns=4_000_000)
    assert temporal_video(req,[exposed],clocks,camera_causal).reason == "future_evidence"
    assert temporal_video(req,[left],clocks,replace(profile,budgets_ns={"camera":1_000_000})).reason == "uncertainty_exceeds_budget"
    assert temporal_video(req,[left],clocks,replace(profile,max_age_ns={})).reason == "missing_age_budget"


def test4():
    req,src,clocks,profile = pd_fake_temporal()
    rows=[s for s in src if s.kind=="telemetry"]
    assert temporal_telemetry(req,rows,clocks,profile).weight == Fraction(2,5)
    assert temporal_telemetry(req,rows,clocks,replace(profile,causal=True)).reason == "future_evidence"
    causal=replace(profile,causal=True,policies={"state":"hold"},budgets_ns={"state":3_000_000},max_age_ns={"state":3_000_000})
    assert temporal_telemetry(req,rows,clocks,causal).sources==(rows[0],)
    assert temporal_telemetry(req,rows,clocks,replace(profile,max_gap_ns={"state":1_000_000})).reason=="telemetry_gap"
    bad=replace(rows[0],source_id="bad-row",source_ns=99_000_000,valid=False)
    assert temporal_telemetry(req,rows+[bad],clocks,profile).reason == "telemetry_gap"
    assert temporal_telemetry(req,rows+[bad],clocks,causal).reason=="telemetry_gap"
    assert temporal_telemetry(req,rows+[replace(rows[0],source_id="duplicate-time")],clocks,profile).reason=="ambiguous_sample"
    assert temporal_telemetry(req,rows,clocks,replace(profile,interpolation_error_ns={})).reason == "missing_interpolation_budget"
    assert temporal_telemetry(req,[rows[0],replace(rows[1],epoch="reset-1")],clocks,profile).reason=="telemetry_gap"
    assert temporal_telemetry(req,rows,clocks,replace(profile,interpolation_error_ns={"state":2_000_000})).reason=="uncertainty_exceeds_budget"


def test5():
    req,src,clocks,profile = pd_fake_temporal()
    audio=next(s for s in src if s.kind=="audio")
    assert temporal_audio(req,[audio],clocks,profile).sample_ranges==(("sound-0",3840,4800),)
    assert temporal_audio(replace(req,uncertainty_ns=200_000),[audio],clocks,profile).reason=="future_evidence"
    a=replace(audio,sample_count=4320)
    b=replace(audio,source_id="sound-1",source_ns=90_000_000,sample_count=5280)
    result=temporal_audio(req,[b,a],clocks,profile)
    assert result.status=="valid",result.reason
    assert result.sample_ranges==(("sound-0",3840,4320),("sound-1",0,480))
    assert result.coverage==1
    assert temporal_audio(req,[a,replace(b,source_ns=91_000_000)],clocks,profile).reason=="audio_gap"
    assert temporal_audio(req,[a,replace(b,source_ns=89_000_000)],clocks,profile).reason=="audio_overlap"
    overlapping=replace(audio,source_id="overlapping-audio",source_ns=1_000_000)
    assert temporal_audio(req,[audio,overlapping],clocks,profile).reason=="audio_overlap"
    assert temporal_audio(req,[replace(audio,origin_known=False)],clocks,profile).reason=="unknown_audio_origin"
    assert temporal_audio(req,[replace(audio,trim_known=False)],clocks,profile).reason=="unknown_audio_origin"
    assert temporal_audio(req,[replace(audio,trim_start=4000)],clocks,profile).reason=="audio_gap"
    assert temporal_audio(req,[replace(audio,trim_start=100)],clocks,profile).sample_ranges==(("sound-0",3840,4800),)
    assert temporal_audio(req,[audio], [replace(clocks[0],valid_end_ns=100_000_000)],profile).reason=="outside_transform_interval"
    assert temporal_audio(replace(req,window_start_ns=90_000_000,window_end_ns=110_000_000),[audio],clocks,profile).reason=="future_evidence"
    assert temporal_audio(replace(req,window_start_ns=80_000_001,window_end_ns=100_000_001),[audio],clocks,replace(profile,policies={"sound":"window"})).coverage==1


def test6():
    req,src,clocks,profile = pd_fake_temporal()
    cmds=[s for s in src if s.kind=="action"]
    assert temporal_action(req,cmds,clocks,profile).sources==(cmds[0],)
    assert temporal_action(req,[cmds[1]],clocks,profile).reason=="no_active_action"
    assert temporal_action(req,[replace(cmds[0],apply_start_ns=None)],clocks,profile).reason=="action_apply_unknown"
    unknown=replace(cmds[1],apply_start_ns=99_000_000,apply_end_ns=101_000_000)
    assert temporal_action(req,[unknown],clocks,profile).reason=="action_apply_unknown"
    # An old active command cannot prove which command applies during a switch.
    assert temporal_action(req,[cmds[0],unknown],clocks,profile).reason=="action_apply_unknown"
    unqualified=replace(cmds[1],apply_start_ns=None,apply_end_ns=None)
    assert temporal_action(req,[cmds[0],unqualified],clocks,profile).reason=="action_apply_unknown"
    slow_switch=replace(cmds[0],source_id="slow-switch",source_ns=90_000_000,apply_start_ns=90_000_000,apply_end_ns=98_000_000)
    assert temporal_action(req,[cmds[0],slow_switch],clocks,profile).reason=="uncertainty_exceeds_budget"
    assert temporal_action(req,[slow_switch,cmds[0]],clocks,profile).reason=="uncertainty_exceeds_budget"
    expired=replace(unqualified,source_ns=90_000_000,effective_end_ns=95_000_000)
    assert temporal_action(req,[cmds[0],expired],clocks,profile).sources==(cmds[0],)
    ending=replace(cmds[0],source_id="ending-command",effective_end_ns=100_000_000,uncertainty_ns=100_000)
    assert temporal_action(req,[cmds[0],ending],clocks,profile).reason=="action_effective_end_unknown"
    assert temporal_action(req,[cmds[0],replace(ending,epoch="unmapped-epoch")],clocks,profile).reason=="clock_epoch_mismatch"
    assert temporal_action(req,[cmds[0],replace(cmds[0],source_id="duplicate-active")],clocks,profile).reason=="ambiguous_action"
    supervised=replace(profile,policies={"action":"supervised"},budgets_ns={"action":2_000_000})
    assert temporal_action(req,cmds,clocks,supervised).sources==(cmds[1],)
    assert temporal_action(req,cmds,clocks,replace(supervised,causal=True)).reason=="future_evidence"
    issued=replace(profile,causal=True,policies={"action":"issued"},budgets_ns={"action":20_000_000},max_age_ns={"action":20_000_000})
    future=replace(cmds[1],source_ns=105_000_000,apply_start_ns=106_000_000,apply_end_ns=106_000_000)
    assert temporal_action(req,[future],clocks,issued).reason=="future_evidence"
    interval_left=replace(clocks[0],valid_end_ns=95_000_000)
    interval_right=replace(clocks[0],transform_id="later",valid_start_ns=97_000_000)
    assert temporal_action(req,[cmds[0]],[interval_left,interval_right],profile).reason=="outside_transform_interval"
    with ContractRaises(ContractError,"cannot precede issue"):
        replace(cmds[1],apply_start_ns=96_000_000)


def test7():
    req,src,clocks,profile = pd_fake_temporal()
    noaudio=[s for s in src if s.kind!="audio"]
    result=temporal_reconcile(req,noaudio,clocks,profile)
    assert result.status=="unresolved"
    assert "sound:missing_modality" in result.reasons
    optional=replace(profile,required_modalities=("camera","state","action"))
    result=temporal_reconcile(req,noaudio,clocks,optional)
    assert result.status=="valid"
    assert result.selections["sound"].reason=="missing_modality"
    pair=replace(profile,pairwise_budgets_ns={"camera|state":2_000_000})
    assert temporal_reconcile(req,src,clocks,pair).reasons==("camera|state:pairwise_budget_exceeded",)
    with ContractRaises(ContractError,"conflicting source identity"):
        temporal_reconcile(req,src+[replace(src[0],checksum="b"*64)],clocks,profile)
    with ContractRaises(ContractError,"unsupported temporal policy"):
        replace(profile,policies={"camera":"guess"})


def test8():
    req,src,clocks,profile = pd_fake_temporal()
    left=replace(clocks[0],valid_end_ns=100_000_000)
    right=replace(clocks[0],transform_id="map-1",source_anchor_ns=100_000_000,ref_anchor_ns=100_000_000,valid_start_ns=100_000_000)
    rows=[s for s in src if s.kind=="telemetry"]
    assert temporal_telemetry(req,rows,[right,left],profile).status=="valid"
    with ContractRaises(ContractError,"discontinuous piecewise"):
        temporal_telemetry(req,rows,[left,replace(right,ref_anchor_ns=100_000_001)],profile)
    with ContractRaises(ContractError,"overlapping clock"):
        temporal_telemetry(req,rows,[left,replace(right,valid_start_ns=99_000_000)],profile)
    gap=replace(right,valid_start_ns=101_000_000)
    assert temporal_telemetry(req,rows,[left,gap],profile).reason=="outside_transform_interval"
    reset=replace(right,source_epoch="reset-1",reference_epoch="epoch-1")
    assert temporal_telemetry(req,[replace(rows[1],epoch="reset-1")],[left,reset],profile).reason=="clock_epoch_mismatch"
    corrected=replace(clocks[0],transform_id="map-corrected",ref_anchor_ns=100_000)
    result=temporal_video(req,[src[0]],[corrected],profile)
    assert result.transform_ids==("map-corrected",)
    assert result.skew_ns==-1_900_000
    assert temporal_video(req,[src[0]],clocks,profile).skew_ns==-2_000_000


def test9():
    req,src,clocks,profile = pd_fake_temporal()
    with ContractRaises(ContractError,"window"):
        replace(req,window_start_ns=100,window_end_ns=99)
    with ContractRaises(ContractError,"integer"):
        replace(src[0],pts=1.0)
    with ContractRaises(ContractError,"invalid audio"):
        replace(src[3],sample_rate=0)
    with ContractRaises(ContractError,"original path and checksum"):
        replace(src[0],path="",checksum="")
    with ContractRaises(ContractError,"conflicting clock transform identity"):
        temporal_reconcile(req,src,clocks+[replace(clocks[0],uncertainty_ns=10)],profile)
    with ContractRaises(ContractError,"nonempty unique"):
        replace(profile,required_modalities=())
    with ContractRaises(ContractError,"nonempty unique"):
        replace(profile,required_modalities=("camera","camera"))
    with ContractRaises(ContractError,"qualification evidence"):
        replace(clocks[0],source_epoch=1)
    with ContractRaises(ContractError,"string episode"):
        replace(req,episode=0)
    with ContractRaises(ContractError,"integer"):
        replace(src[3],sample_count=True)
    with ContractRaises(ContractError,"booleans"):
        replace(profile,causal="false")
    with ContractRaises(ContractError,"nonempty media track"):
        replace(src[0],track="")
    with ContractRaises(ContractError,"nonempty media track"):
        replace(src[3],track="")
    # Missing budgets never quietly become unlimited defaults.
    assert temporal_video(req,[src[0]],clocks,replace(profile,budgets_ns={})).reason=="missing_budget"


def test_all():
    test1()
    test2()
    test3()
    test4()
    test5()
    test6()
    test7()
    test8()
    test9()
    log_info("temporal contract checks passed")


if __name__ == "__main__":
    fire.Fire()
