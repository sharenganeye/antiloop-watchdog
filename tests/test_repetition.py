"""Requirements 5 and 8: text repetition detection and its false-positive guards."""
import json
import random

import pytest

from antiloop_watchdog.config import ConfigManager
from antiloop_watchdog.repetition import RepetitionDetector, TOKEN_RE
from antiloop_watchdog.schema import ConfigError
from conftest import events_of
from synth import feed_text, paragraph, prose


def detector(tmp_path, **rep):
    cm = ConfigManager(tmp_path / "c.json", base_dir=tmp_path, overrides={"repetition": rep} if rep else None)
    return RepetitionDetector(lambda: cm.data)


def run(det, text, kind="answer", chunk=57):
    out = []
    for i in range(0, len(text), chunk):
        out.extend(det.feed(text[i:i + chunk], kind))
    out.extend(det.feed("", kind, final=True))
    return out


def test_long_repeated_text_is_detected(make_engine):
    eng, clock, log = make_engine("observe")
    para = paragraph(7)
    feed_text(eng, clock, "s1", " ".join([para] * 5), kind="reasoning")
    dets = events_of(log, "detection")
    rep = [d for d in dets if d["detection_type"] == "text_repeat"]
    assert rep, "loop not detected"
    d = rep[0]
    assert d["repetitions"] >= 3 and d["confidence"] >= 0.78
    assert d["sequence_fingerprint"] and d["wasted_tokens_est"] > 0
    assert "sample" not in d["details"]                    # no text stored by default


def test_fingerprint_is_stable_across_rotations(tmp_path):
    det = detector(tmp_path)
    dets = run(det, " ".join([paragraph(11)] * 6))
    fps = {d.sequence_fp for d in dets if d.type == "text_repeat"}
    assert len(dets) >= 2 and len(fps) == 1


def test_short_period_loop_is_detected(tmp_path):
    det = detector(tmp_path)
    dets = run(det, "I should check the file again. " * 40)
    assert any(d.type == "text_repeat" and d.repetitions >= 3 for d in dets)


def test_detection_survives_arbitrary_chunk_boundaries(tmp_path):
    for chunk in (1, 3, 7, 50):
        det = detector(tmp_path)
        dets = run(det, " ".join([paragraph(5)] * 5), chunk=chunk)
        assert any(d.type == "text_repeat" for d in dets), chunk


def test_normal_prose_never_triggers(tmp_path):
    det = detector(tmp_path)
    for seed in range(5):
        assert run(det, prose(random.Random(seed), 4000), kind="reasoning") == []
    assert det.tokens_total > 20000


def test_legitimate_repeated_instruction_is_not_an_alert(make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    rng = random.Random(9)
    rule = "Always run the full test suite before you report that the task is finished to the user."
    text = " ".join([prose(rng, 60), rule, prose(rng, 60), rule, prose(rng, 60), rule, prose(rng, 60)])
    feed_text(eng, clock, "s1", text)
    assert not [e for e in events_of(log, "detection") if e["detection_type"] == "text_repeat"]
    assert recorder.calls == [] and eng.get_directives("s1", None, True) == []     # at most an info-level log


def test_json_with_varying_values_is_not_flagged(tmp_path):
    det = detector(tmp_path)
    rows = [{"id": i, "name": f"item-{i}", "tags": ["a", "b"], "ok": True, "score": i * 3} for i in range(80)]
    assert run(det, json.dumps(rows, indent=2)) == []


def test_legitimate_repeated_code_structures_are_not_flagged(tmp_path):
    det = detector(tmp_path)
    funcs = "\n\n".join(
        f"def handle_{name}(request):\n    payload = parse(request)\n    result = service.{name}(payload, retries=3)\n"
        f"    log.info('handled {name}', extra={{'n': {i}}})\n    return respond(result, status=200)\n"
        for i, name in enumerate(["create", "update", "delete", "fetch", "list", "sync", "export", "import"]))
    assert run(det, "```python\n" + funcs + "\n```") == []


def test_identical_boilerplate_inside_code_fence_needs_far_more_repeats(tmp_path):
    det = detector(tmp_path)
    block = ("def test_case():\n    value = compute(1, 2, mode='fast', retries=3, timeout=10)\n"
             "    assert value == expected(1, 2) and value.ok and not value.errors and value.latency < 5\n"
             "    cleanup(value, force=True, quiet=True)\n")
    assert run(det, "```python\n" + block * 3 + "```") == []     # fenced: threshold x3, 3 copies are not a loop


@pytest.mark.parametrize("kind", ["tool_output", "file_content"])
def test_structured_tool_output_and_file_content_are_ignored(tmp_path, kind):
    det = detector(tmp_path)
    assert run(det, " ".join([paragraph(3)] * 10), kind=kind) == []
    assert det.tokens_total == 0


def test_separator_lines_are_ignored(tmp_path):
    det = detector(tmp_path)
    assert run(det, ("-" * 100 + "\n") * 8 + ("=" * 90 + "\n") * 6) == []


def test_ignore_patterns_suppress_known_repeats(tmp_path):
    det = detector(tmp_path, ignore_patterns=[r"\bimportant\b"])
    para = "important " + paragraph(21)
    assert [d for d in run(det, " ".join([para] * 6)) if d.type == "text_repeat"] == []


def test_capture_text_is_opt_in_and_truncated(make_engine):
    eng, clock, log = make_engine("observe", {"logging": {"capture_text": True, "capture_chars": 40}})
    feed_text(eng, clock, "s1", " ".join([paragraph(7)] * 5))
    d = [e for e in events_of(log, "detection") if e["detection_type"] == "text_repeat"][0]
    assert 0 < len(d["details"]["sample"]) <= 40


def test_impossible_window_configuration_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        ConfigManager(tmp_path / "c.json", base_dir=tmp_path,
                      overrides={"repetition": {"window_tokens": 128, "min_sequence_tokens": 64, "threshold": 3}})


def test_per_token_cost_is_small(tmp_path):
    import time
    det = detector(tmp_path)
    text = prose(random.Random(0), 20000)
    t0 = time.perf_counter()
    run(det, text)
    per_token_us = (time.perf_counter() - t0) / max(1, det.tokens_total) * 1e6
    assert per_token_us < 50, per_token_us        # generous bound; real value is far lower
