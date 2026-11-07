"""Intervention modes, escalation, failure handling (req 11), recovery (req 12)."""
import socket
import time

import pytest

from antiloop_watchdog.adapters import NullAdapter, WebhookAdapter
from antiloop_watchdog.config import DEFAULT_MESSAGE
from antiloop_watchdog.schema import ConflictError, NotFoundError, ValidationError
from conftest import RecordingAdapter, events_of
from synth import call, feed_text, gen, paragraph, prose


def fail3(eng, clock, sid="s1", cmd="make"):
    for _ in range(3):
        call(eng, clock, sid, "terminal", {"command": cmd}, "error", "boom")


def text_loop(eng, clock, sid="s1", seed=7, n=5):
    return feed_text(eng, clock, sid, " ".join([paragraph(seed)] * n), kind="reasoning")


def test_default_message_is_the_required_text():
    assert DEFAULT_MESSAGE.startswith("You appear to be stuck in a repetitive loop.")
    assert DEFAULT_MESSAGE.endswith("Do not restart the same failed sequence.")


def test_observe_mode_detects_and_logs_but_never_acts(make_engine, recorder):
    eng, clock, log = make_engine("observe", adapter=recorder)
    fail3(eng, clock)
    text_loop(eng, clock)
    assert events_of(log, "detection", "s1")
    assert events_of(log, "intervention") == [] and recorder.calls == []
    assert eng.get_directives("s1", None, True) == []


def test_warn_mode_exposes_directive_without_pushing(make_engine, recorder):
    eng, clock, log = make_engine("warn", adapter=recorder)
    fail3(eng, clock)
    assert recorder.calls == []
    ds = eng.get_directives("s1", None, peek=True)
    assert len(ds) == 1 and ds[0]["action"] == "redirect" and ds[0]["message"] == DEFAULT_MESSAGE
    assert ds[0]["status"] == "pending" and ds[0]["source"] == "warn"
    assert ds[0]["reason"]["detection_type"] == "tool_repeat_failed"
    ev = events_of(log, "intervention", "s1")[0]
    assert ev["intervention_result"] == "queued_for_pull" and ev["intervention_action"] == "redirect"


def test_automatic_mode_redirects_only_the_offending_session(make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    fail3(eng, clock, "s1")
    assert recorder.calls == [("s1", "redirect", DEFAULT_MESSAGE)]
    d = eng.get_directives("s1", "delivered", True)
    assert len(d) == 1 and d[0]["delivery"] == "pushed"
    ev = events_of(log, "intervention", "s1")[0]
    assert ev["intervention_result"] == "pushed" and ev["mode"] == "automatic"


def test_per_session_mode_overrides_default(make_engine, recorder):
    eng, clock, log = make_engine("observe", adapter=recorder)
    eng.ingest({"type": "state", "session_id": "s1"})
    eng.set_mode("s1", "automatic")
    fail3(eng, clock, "s1")
    fail3(eng, clock, "s2")
    assert [c[0] for c in recorder.calls] == ["s1"]
    assert eng.get_directives("s2", None, True) == []


def test_cooldown_prevents_directive_spam(make_engine):
    eng, clock, log = make_engine("warn")
    for _ in range(12):
        call(eng, clock, "s1", "terminal", {"command": "make"}, "error", "boom", dt=1.0)
    assert len(events_of(log, "intervention", "s1")) == 1
    for _ in range(5):
        call(eng, clock, "s1", "terminal", {"command": "make"}, "error", "boom", dt=10.0)
    assert len(events_of(log, "intervention", "s1")) >= 2           # after the 30 s cooldown


def test_speed_alone_never_intervenes_but_corroborates_a_real_detection(make_engine):
    # 3 repetitions alone: confidence 0.78 < auto threshold 0.85  -> queued only
    eng, clock, log = make_engine("automatic", adapter=RecordingAdapter())
    gen_calls = eng.adapter.calls
    text_loop(eng, clock, "plain", n=4)
    assert gen_calls == [] and eng.get_directives("plain", None, True)
    # same loop while the session shows a sustained critical speed -> corroborated, delivered
    eng2, clock2, log2 = make_engine("automatic", adapter=RecordingAdapter())
    for _ in range(10):
        gen(eng2, clock2, "fast", 340, 4.0)
    assert eng2.session_detail("fast")["speed"]["level"] == "critical" and eng2.adapter.calls == []
    text_loop(eng2, clock2, "fast", n=4)
    d = events_of(log2, "detection", "fast")[0]
    assert d["details"]["speed_level"] == "critical" and d["details"]["combined_confidence"] > d["confidence"]
    assert [c[0] for c in eng2.adapter.calls] == ["fast"]


def test_escalation_redirect_redirect_interrupt(make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    for round_ in range(3):
        fail3(eng, clock)
        clock.advance(45)                                           # beyond cooldown and grace
    assert [c[1] for c in recorder.calls] == ["redirect", "redirect", "interrupt"]
    assert all(c[0] == "s1" for c in recorder.calls)
    assert eng.session_detail("s1")["interrupts_total"] == 1


def test_escalation_waits_until_the_redirect_was_actually_delivered(make_engine):
    from antiloop_watchdog.adapters import QueueAdapter
    eng, clock, log = make_engine("automatic", adapter=QueueAdapter())      # pull model, nobody fetches
    for _ in range(4):
        fail3(eng, clock)
        clock.advance(45)
    actions = [e["intervention_action"] for e in events_of(log, "intervention", "s1")]
    assert actions and set(actions) == {"redirect"}                     # never an interrupt without delivery


def test_interrupt_can_be_disabled(make_engine, recorder):
    eng, clock, log = make_engine("automatic", {"intervention": {"allow_interrupt": False}}, adapter=recorder)
    for _ in range(4):
        fail3(eng, clock)
        clock.advance(45)
    assert {c[1] for c in recorder.calls} == {"redirect"}


def test_recovery_after_intervention(make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    fail3(eng, clock)
    assert eng.session_detail("s1")["in_intervention"] is True
    d = eng.get_directives("s1", None, False)
    eng.ack_directive("s1", d[0]["id"], "applied")
    for i in range(15):                                             # the model behaves again
        call(eng, clock, "s1", "terminal", {"command": f"step-{i}"}, "ok", f"output {i}", dt=2)
    clock.advance(61)
    call(eng, clock, "s1", "terminal", {"command": "final"}, "ok", "done")
    assert events_of(log, "recovered", "s1")
    s = eng.session_detail("s1")
    assert s["in_intervention"] is False
    # a later, unrelated problem starts from a clean slate: redirect, not interrupt
    fail3(eng, clock, cmd="other")
    assert recorder.calls[-1][1] == "redirect"


def test_detectors_are_reset_after_an_intervention(make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    fail3(eng, clock)
    n = len(recorder.calls)
    clock.advance(45)
    call(eng, clock, "s1", "terminal", {"command": "make"}, "error", "boom")       # old evidence is gone
    assert len(recorder.calls) == n


def test_hermes_integration_unavailable_null_adapter(make_engine):
    eng, clock, log = make_engine("automatic", adapter=NullAdapter())
    fail3(eng, clock)
    ev = events_of(log, "intervention", "s1")[0]
    assert ev["intervention_result"].startswith("adapter_unavailable")
    d = eng.get_directives("s1", None, True)
    assert len(d) == 1 and d[0]["status"] == "pending" and d[0]["delivery"] == "unavailable"
    assert eng.status()["adapter"]["verified_against_hermes"] is False


def test_failed_push_keeps_directive_and_retries_sooner(make_engine):
    ad = RecordingAdapter(fail=True)
    eng, clock, log = make_engine("automatic", adapter=ad)
    fail3(eng, clock)
    assert len(ad.calls) == 1
    assert events_of(log, "intervention", "s1")[0]["intervention_result"].startswith("push_failed")
    d = eng.get_directives("s1", None, True)
    assert len(d) == 1 and d[0]["delivery"] == "push_failed" and d[0]["status"] == "pending"
    assert eng.session_detail("s1")["in_intervention"] is False          # nothing was delivered
    ad.fail = False
    clock.advance(16)                                                    # failure_retry_s (15) < cooldown (30)
    fail3(eng, clock)
    assert len(ad.calls) == 2 and ad.calls[-1][1] == "redirect"
    assert eng.session_detail("s1")["in_intervention"] is True


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_unreachable_webhook_is_handled_off_thread(make_engine):
    port = _free_port()                                                  # nothing listens here
    ad = WebhookAdapter(f"http://127.0.0.1:{port}/redirect", f"http://127.0.0.1:{port}/interrupt", 1.0)
    eng, clock, log = make_engine("automatic", adapter=ad)
    t0 = time.time()
    fail3(eng, clock)
    assert time.time() - t0 < 0.5                                        # ingestion was not blocked
    assert events_of(log, "intervention", "s1")[0]["intervention_result"] == "dispatch_pending"
    for _ in range(100):
        if events_of(log, "intervention_result", "s1"):
            break
        time.sleep(0.05)
    res = events_of(log, "intervention_result", "s1")
    assert res and res[0]["intervention_result"].startswith("push_failed")
    assert eng.get_directives("s1", None, True)[0]["delivery"] == "push_failed"
    assert eng.status()["counters"]["adapter_failures"] == 1


def test_webhook_url_must_be_loopback():
    from antiloop_watchdog.schema import ConfigError
    with pytest.raises(ConfigError):
        WebhookAdapter("http://example.com/hook", "", 1.0)
    with pytest.raises(ConfigError):
        WebhookAdapter("https://127.0.0.1/hook", "", 1.0)


# ------------------------------------------------------------------ manual interventions and directives

def test_manual_intervene_rules(make_engine, recorder):
    eng, clock, log = make_engine("observe", adapter=recorder)
    eng.ingest({"type": "state", "session_id": "s1"})
    with pytest.raises(ConflictError):
        eng.intervene("s1", "redirect", None)                            # observe mode
    eng.set_mode("s1", "warn")
    d = eng.intervene("s1", "redirect", "Try a different approach.\x00")
    assert d["message"] == "Try a different approach." and d["source"] == "manual" and recorder.calls == []
    with pytest.raises(ConflictError):
        eng.intervene("s1", "interrupt", None)                           # needs automatic
    eng.set_mode("s1", "automatic")
    eng.intervene("s1", "interrupt", None)
    assert recorder.calls[-1][:2] == ("s1", "interrupt")
    with pytest.raises(ValidationError):
        eng.intervene("s1", "kill-gateway", None)
    with pytest.raises(ValidationError):
        eng.intervene("s1", "redirect", "x" * 2001)
    with pytest.raises(NotFoundError):
        eng.intervene("nope", "redirect", None)


def test_directive_fetch_ack_expiry(make_engine):
    eng, clock, log = make_engine("warn")
    fail3(eng, clock)
    peek = eng.get_directives("s1", None, peek=True)
    assert peek[0]["status"] == "pending"
    got = eng.get_directives("s1", None, peek=False)
    assert got[0]["status"] == "delivered" or eng.get_directives("s1", "delivered", True)
    acked = eng.ack_directive("s1", got[0]["id"], "applied")
    assert acked["status"] == "acked" and acked["ack_result"] == "applied"
    assert eng.get_directives("s1", None, True) == []                    # acked ones are no longer open
    with pytest.raises(NotFoundError):
        eng.ack_directive("s1", "d-unknown", "applied")
    with pytest.raises(ValidationError):
        eng.ack_directive("s1", got[0]["id"], "maybe")
    clock.advance(31)                                                   # past the cooldown
    fail3(eng, clock, cmd="again")
    assert eng.get_directives("s1", None, True)
    clock.advance(700)
    eng.housekeeping()
    assert eng.get_directives("s1", "expired", True)


def test_reset_clears_detectors_and_open_directives(make_engine):
    eng, clock, log = make_engine("warn")
    fail3(eng, clock)
    eng.reset_session("s1")
    assert eng.get_directives("s1", None, True) == []
    assert eng.session_detail("s1")["detections_total"] == 3 - 3 + 1
    assert events_of(log, "session_reset", "s1")
