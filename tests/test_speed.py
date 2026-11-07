"""Requirements 1-4 (+ prefill): the speed monitor is an indicator and never a reason to interrupt."""
import random

from conftest import events_of
from synth import block, gen, RANGES


def test_normal_generation_40_55_never_alerts(make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    rng = random.Random(1)
    for i in range(80):
        phase = "thinking" if i % 2 == 0 else "answering"
        tokens, dur, _ = block(rng, phase)
        gen(eng, clock, "s1", tokens, dur, phase)
    snap = eng.session_detail("s1")["speed"]
    assert snap["level"] == "normal"
    assert 34 <= snap["rolling_tps"] <= 56
    assert snap["ignored_blocks"] == {"tiny_block": 0, "short_block": 0, "prefill": 0}
    assert not events_of(log, "detection") and not events_of(log, "speed_indicator")
    assert recorder.calls == []


def test_legitimate_tool_call_generation_50_71_stays_normal(make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    rng = random.Random(2)
    for _ in range(60):
        tokens, dur, tps = block(rng, "tool_call")
        assert 50 <= tps <= 71
        gen(eng, clock, "s1", tokens, dur, "tool_call")
    snap = eng.session_detail("s1")["speed"]
    assert snap["level"] == "normal" and snap["rolling_tps"] < 70
    assert not events_of(log, "speed_indicator") and recorder.calls == []


def test_short_80_plus_block_is_ignored_by_rolling_window(make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    rng = random.Random(3)
    for _ in range(12):
        tokens, dur, _ = block(rng, "answering")
        gen(eng, clock, "s1", tokens, dur, "answering")
    gen(eng, clock, "s1", 100, 1.2, "answering")                      # 83 tok/s block
    snap = eng.session_detail("s1")["speed"]
    assert snap["peak_block_tps"] >= 80
    assert snap["level"] == "normal" and snap["rolling_tps"] < 70
    assert not events_of(log, "speed_indicator") and recorder.calls == []


def test_single_fast_block_on_a_fresh_session_has_insufficient_data(make_engine):
    eng, clock, log = make_engine("observe")
    gen(eng, clock, "s1", 100, 1.2)
    snap = eng.session_detail("s1")["speed"]
    assert snap["rolling_tps"] is None and snap["level"] == "normal"


def test_sustained_critical_speed_is_only_an_indicator(make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    for _ in range(12):
        gen(eng, clock, "s1", 340, 4.0)                               # 85 tok/s, sustained
    snap = eng.session_detail("s1")["speed"]
    assert snap["level"] == "critical"
    ind = events_of(log, "speed_indicator")
    assert ind and ind[-1]["details"]["level"] == "critical"
    assert not events_of(log, "detection") and not events_of(log, "intervention")
    assert recorder.calls == [] and eng.get_directives("s1", None, True) == []


def test_tiny_block_artifacts_are_ignored(make_engine):
    eng, clock, log = make_engine("observe")
    for tokens, dur in [(3, 0.02), (4, 0.03), (2, 0.01), (1, 0.005)]:
        gen(eng, clock, "s1", tokens, dur)
    for tokens, dur in [(60, 0.3), (30, 0.2), (90, 0.5)]:             # >=5 tokens but <1 s
        gen(eng, clock, "s1", tokens, dur)
    snap = eng.session_detail("s1")["speed"]
    assert snap["accepted_blocks"] == 0
    assert snap["ignored_blocks"]["tiny_block"] == 4 and snap["ignored_blocks"]["short_block"] == 3
    assert snap["rolling_tps"] is None and snap["level"] == "normal"
    assert not events_of(log, "speed_indicator")


def test_prefill_throughput_is_not_generation_speed(make_engine):
    eng, clock, log = make_engine("observe")
    rng = random.Random(4)
    for _ in range(10):
        tokens, dur, tps = block(rng, "prefill")
        assert 1500 <= tps <= 1800
        gen(eng, clock, "s1", tokens, dur, "prefill")
    for _ in range(5):                                                # prefill mislabelled as decode
        tokens, dur, _ = block(rng, "prefill")
        gen(eng, clock, "s1", tokens, dur, "decode")
    snap = eng.session_detail("s1")["speed"]
    assert snap["accepted_blocks"] == 0 and snap["ignored_blocks"]["prefill"] == 15
    assert snap["level"] == "normal" and snap["last_prefill_tps"] >= 1500
    assert not events_of(log, "speed_indicator")


def test_baseline_ranges_are_those_measured():
    from antiloop_watchdog.config import DEFAULTS
    b = DEFAULTS["speed"]["baseline"]
    assert b["thinking"] == list(RANGES["thinking"]) and b["tool_call"] == list(RANGES["tool_call"])
    assert DEFAULTS["speed"]["warning_tps"] == 70 and DEFAULTS["speed"]["critical_tps"] == 80
