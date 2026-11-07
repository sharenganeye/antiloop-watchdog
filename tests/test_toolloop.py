"""Requirements 6 and 7: tool-call repetition, repeated failed commands, legitimate repetition."""
import random

from conftest import events_of
from synth import call, prose


def types(log, sid="s1"):
    return [e["detection_type"] for e in events_of(log, "detection", sid)]


def test_repeated_failed_command_identical_error(make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    errs = ["FAILED tests/test_a.py::t - assert 1 == 2 at 0x7f1a (3.1s)",
            "FAILED tests/test_a.py::t - assert 1 == 2 at 0x7f9c (2.8s)",
            "FAILED tests/test_a.py::t - assert 1 == 2 at 0x7e00 (3.4s)"]
    for e in errs:
        call(eng, clock, "s1", "terminal", {"command": "pytest -x tests/", "request_id": e[-8:]}, "error", e)
    d = events_of(log, "detection", "s1")
    assert [x["detection_type"] for x in d] == ["tool_repeat_failed"]
    assert d[0]["confidence"] >= 0.85 and d[0]["tool_fingerprint"] and d[0]["repetitions"] == 3
    assert d[0]["details"]["evidence"]["identical_error"] is True
    assert [(c[0], c[1]) for c in recorder.calls] == [("s1", "redirect")]


def test_failed_command_with_changing_errors_is_only_a_warning(make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    for e in ["permission denied while opening socket", "no such host exists in resolver", "disk quota exceeded"]:
        call(eng, clock, "s1", "terminal", {"command": "deploy.sh"}, "error", e)
    d = events_of(log, "detection", "s1")
    assert d and d[0]["confidence"] < 0.85
    assert recorder.calls == []                                   # below auto threshold: queued only
    assert len(eng.get_directives("s1", None, True)) == 1


def test_whitespace_and_volatile_args_do_not_hide_repetition(make_engine):
    eng, clock, log = make_engine("observe")
    for i, cmd in enumerate(["pytest  -x", "pytest -x ", " pytest -x"]):
        call(eng, clock, "s1", "terminal", {"command": cmd, "timestamp": 1000 + i, "id": f"x{i}"}, "error", "boom")
    assert "tool_repeat_failed" in types(log)


def test_edit_then_rerun_is_not_a_loop(make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    for i in range(8):
        call(eng, clock, "s1", "write_file", {"path": "app.py", "content": f"version {i}"}, "ok", "written")
        call(eng, clock, "s1", "terminal", {"command": "pytest -x"}, "error", "1 failed")
    assert events_of(log, "detection", "s1") == [] and recorder.calls == []


def test_same_file_read_repeatedly_without_progress(make_engine):
    eng, clock, log = make_engine("observe")
    for _ in range(4):
        call(eng, clock, "s1", "read_file", {"path": "/src/main.py"}, "ok", "print('hi')")
    d = events_of(log, "detection", "s1")
    assert [x["detection_type"] for x in d] == ["tool_repeat_read"] and d[0]["repetitions"] == 4


def test_rereading_a_changing_file_is_legitimate(make_engine):
    eng, clock, log = make_engine("observe")
    for i in range(8):
        call(eng, clock, "s1", "read_file", {"path": "/var/log/app.log"}, "ok", f"line count {i}")
    assert events_of(log, "detection", "s1") == []


def test_read_edit_read_with_real_edits_is_progress(make_engine):
    eng, clock, log = make_engine("observe")
    for i in range(6):
        call(eng, clock, "s1", "read_file", {"path": "a.py"}, "ok", "same content every time")
        call(eng, clock, "s1", "edit_file", {"path": "a.py", "old": f"x{i}", "new": f"y{i}"}, "ok", "ok")
    assert events_of(log, "detection", "s1") == []


def test_identical_edit_repeated(make_engine):
    eng, clock, log = make_engine("observe")
    for _ in range(3):
        call(eng, clock, "s1", "edit_file", {"path": "a.py", "old": "foo", "new": "bar"}, "ok", "applied")
    d = events_of(log, "detection", "s1")
    assert [x["detection_type"] for x in d] == ["tool_repeat_edit"] and d[0]["confidence"] >= 0.8


def test_same_search_repeated(make_engine):
    eng, clock, log = make_engine("observe")
    for _ in range(3):
        call(eng, clock, "s1", "web_search", {"query": "llama.cpp slot context shift"}, "ok", "10 results ...")
    assert types(log) == ["tool_repeat_search"]


def test_action_response_cycle(make_engine):
    eng, clock, log = make_engine("observe")
    for _ in range(3):
        call(eng, clock, "s1", "read_file", {"path": "x.py"}, "ok", "content-A")
        call(eng, clock, "s1", "terminal", {"command": "ls"}, "ok", "listing-B")
    d = [e for e in events_of(log, "detection", "s1") if e["detection_type"] == "tool_cycle"]
    assert d and d[0]["details"]["evidence"]["period_calls"] == 2


def test_polling_tools_get_a_higher_threshold(make_engine):
    eng, clock, log = make_engine("observe")
    for _ in range(8):
        call(eng, clock, "s1", "sleep", {"seconds": 5}, "ok", "slept")
    assert events_of(log, "detection", "s1") == []
    for _ in range(5):
        call(eng, clock, "s1", "custom_probe", {"x": 1}, "ok", "same")
    assert "tool_repeat_call" in types(log)


def test_exempt_tools_are_never_flagged(make_engine):
    eng, clock, log = make_engine("observe", {"tool_loop": {"exempt_tools": ["^noisy_tool$"]}})
    for _ in range(10):
        call(eng, clock, "s1", "noisy_tool", {"a": 1}, "error", "boom")
    assert events_of(log, "detection", "s1") == []


def test_state_event_starts_a_new_epoch(make_engine):
    eng, clock, log = make_engine("observe")
    for _ in range(2):
        call(eng, clock, "s1", "terminal", {"command": "make build"}, "error", "boom")
    eng.ingest({"type": "state", "session_id": "s1", "changed": True})
    for _ in range(2):
        call(eng, clock, "s1", "terminal", {"command": "make build"}, "error", "boom")
    assert events_of(log, "detection", "s1") == []
    call(eng, clock, "s1", "terminal", {"command": "make build"}, "error", "boom")
    assert "tool_repeat_failed" in types(log)


def test_call_then_result_pairing_by_call_id(make_engine):
    eng, clock, log = make_engine("observe")
    for i in range(3):
        clock.advance(1)
        eng.ingest({"type": "tool_call", "session_id": "s1", "tool": "terminal", "args": {"command": "npm test"},
                    "call_id": f"c{i}"})
        assert events_of(log, "detection", "s1") == [] or i == 2
        clock.advance(1)
        eng.ingest({"type": "tool_result", "session_id": "s1", "call_id": f"c{i}", "status": "error",
                    "result_text": "ENOENT package.json"})
    assert types(log) == ["tool_repeat_failed"]


def test_tokens_attributed_to_repeated_calls_become_wasted_estimate(make_engine):
    eng, clock, log = make_engine("observe")
    for _ in range(3):
        clock.advance(1)
        eng.ingest({"type": "tool_call", "session_id": "s1", "tool": "terminal", "args": {"command": "x"},
                    "status": "error", "result_text": "e", "tokens": 400})
    d = events_of(log, "detection", "s1")[0]
    assert d["wasted_tokens_est"] == 800


def test_raw_arguments_are_not_logged(make_engine):
    eng, clock, log = make_engine("observe")
    for _ in range(3):
        call(eng, clock, "s1", "terminal", {"command": "echo SECRET-TOKEN-123"}, "error", "boom SECRET-TOKEN-123")
    import json
    assert "SECRET-TOKEN-123" not in json.dumps(log.events)


# ------------------------------------------------------------------ progress / stagnation

def test_exploration_of_many_different_files_is_not_stagnation(make_engine):
    eng, clock, log = make_engine("observe")
    for i in range(40):
        call(eng, clock, "s1", "read_file", {"path": f"/src/module_{i}.py"}, "ok", f"content {i}")
    assert events_of(log, "detection", "s1") == []


def test_long_run_of_different_failing_commands_is_stagnation(make_engine):
    eng, clock, log = make_engine("observe")
    for i in range(30):
        call(eng, clock, "s1", "terminal", {"command": f"try-variant-{i}"}, "error", "same fatal error")
    d = [e for e in events_of(log, "detection", "s1") if e["detection_type"] == "stagnation"]
    assert d and 0.6 <= d[0]["confidence"] <= 0.9
    ev = d[0]["details"]["evidence"]
    assert ev["error_streak"] >= 3 and ev["duplicate_results"] > 0


def test_stagnation_signals_visible_in_session_detail(make_engine):
    eng, clock, log = make_engine("observe")
    for i in range(10):
        call(eng, clock, "s1", "terminal", {"command": "same"}, "error", "same")
    assert eng.session_detail("s1")["progress"]["error_streak"] == 10
