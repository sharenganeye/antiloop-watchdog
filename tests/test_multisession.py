"""Requirements 9 and 10: sessions are tracked independently; a loop never touches another session."""
import random

import pytest

from antiloop_watchdog.schema import ConflictError
from conftest import events_of
from synth import block, call, feed_text, gen, paragraph, prose


def test_two_concurrent_sessions_keep_independent_state(make_engine):
    eng, clock, log = make_engine("observe")
    rng = random.Random(5)
    for i in range(30):                      # both sessions generate concurrently (6 s blocks)
        clock.advance(6.0)
        for sid, phase in (("A", "thinking"), ("B", "tool_call")):
            lo, hi = {"thinking": (40, 55), "tool_call": (50, 71)}[phase]
            eng.ingest({"type": "generation", "session_id": sid, "phase": phase,
                        "tokens": int(rng.uniform(lo, hi) * 6.0), "duration_s": 6.0})
    sess = {s["session_id"]: s for s in eng.list_sessions()}
    assert set(sess) == {"A", "B"}
    assert sess["A"]["speed"]["accepted_blocks"] == 30 and sess["B"]["speed"]["accepted_blocks"] == 30
    assert sess["A"]["speed"]["rolling_tps"] < sess["B"]["speed"]["rolling_tps"]


def test_loop_in_one_session_while_other_generates_normally(make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    rng = random.Random(6)
    looping = " ".join([paragraph(7)] * 6)
    normal = prose(rng, 700)
    step_a = 80
    # interleave chunks from both sessions like two live slots
    for i in range(0, max(len(looping), len(normal)), step_a):
        clock.advance(0.3)
        if i < len(looping):
            eng.ingest({"type": "text_delta", "session_id": "A", "text": looping[i:i + step_a], "kind": "reasoning"})
        if i < len(normal):
            eng.ingest({"type": "text_delta", "session_id": "B", "text": normal[i:i + step_a], "kind": "reasoning"})
        tokens, dur, _ = block(rng, "answering")
        gen(eng, clock, "B", tokens, dur, "answering")
    eng.ingest({"type": "text_delta", "session_id": "A", "text": "", "final": True})
    eng.ingest({"type": "text_delta", "session_id": "B", "text": "", "final": True})
    assert events_of(log, "detection", "A")
    assert events_of(log, "detection", "B") == []
    assert [c[0] for c in recorder.calls] == ["A"]                      # only A was ever targeted
    assert eng.get_directives("B", None, True) == []
    b = eng.session_detail("B")
    assert b["in_intervention"] is False and b["detections_total"] == 0 and b["speed"]["accepted_blocks"] > 0


def test_same_tool_calls_in_two_sessions_do_not_add_up(make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    for _ in range(2):                                                  # each session below threshold
        call(eng, clock, "A", "terminal", {"command": "make"}, "error", "boom")
        call(eng, clock, "B", "terminal", {"command": "make"}, "error", "boom")
    assert events_of(log, "detection") == [] and recorder.calls == []


def test_intervention_in_one_session_does_not_reset_the_other(make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    for _ in range(2):
        call(eng, clock, "B", "terminal", {"command": "make"}, "error", "boom")
    for _ in range(3):
        call(eng, clock, "A", "terminal", {"command": "make"}, "error", "boom")
    assert [c[0] for c in recorder.calls] == ["A"]
    call(eng, clock, "B", "terminal", {"command": "make"}, "error", "boom")   # B's own 3rd failure
    assert [c[0] for c in recorder.calls] == ["A", "B"]


def test_events_without_session_id_never_trigger_automatic_intervention(make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    for _ in range(5):
        call(eng, clock, None, "terminal", {"command": "make"}, "error", "boom")
    assert events_of(log, "detection", "__unattributed__")
    assert recorder.calls == []                                         # dispatch impossible by design
    s = eng.session_detail("__unattributed__")
    assert s["identified"] is False and s["mode"] == "warn" and s["requested_mode"] == "automatic"
    d = events_of(log, "intervention", "__unattributed__")
    assert d and "no explicit session id" in d[0]["details"]["note"]
    with pytest.raises(ConflictError):
        eng.set_mode("__unattributed__", "automatic")


def test_unattributed_events_never_mix_with_named_sessions(make_engine):
    eng, clock, log = make_engine("observe")
    call(eng, clock, None, "terminal", {"command": "x"}, "error", "e")
    call(eng, clock, "A", "terminal", {"command": "x"}, "error", "e")
    ids = {s["session_id"] for s in eng.list_sessions()}
    assert ids == {"__unattributed__", "A"}


def test_require_session_id_rejects_anonymous_events(make_engine):
    from antiloop_watchdog.schema import ValidationError
    eng, clock, log = make_engine("observe", {"require_session_id": True})
    with pytest.raises(ValidationError):
        eng.ingest({"type": "state"})


def test_slot_pseudo_sessions_cannot_be_forged_or_targeted(make_engine, recorder):
    from antiloop_watchdog.schema import ValidationError
    eng, clock, log = make_engine("automatic", adapter=recorder)
    with pytest.raises(ValidationError):
        eng.ingest({"type": "state", "session_id": "slot-0"})
    eng.ingest_speed_sample("0", "decode", 200, 4.0)
    s = eng.session_detail("slot-0")
    assert s["kind"] == "slot" and s["automatic_eligible"] is False and s["mode"] != "automatic"
    with pytest.raises(ConflictError):
        eng.set_mode("slot-0", "automatic")


def test_session_end_and_eviction(make_engine):
    eng, clock, log = make_engine("observe", {"sessions": {"max_sessions": 2}})
    for sid in ("A", "B", "C"):
        clock.advance(1)
        eng.ingest({"type": "state", "session_id": sid})
    assert {s["session_id"] for s in eng.list_sessions()} == {"B", "C"}
    eng.ingest({"type": "session_end", "session_id": "B"})
    assert {s["session_id"] for s in eng.list_sessions()} == {"C"}
