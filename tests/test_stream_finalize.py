"""Regression tests for the stream-finalisation flush (final buffer analysis).

Style matches test_stream_detector.py: standalone checks, no Hermes, imports the plugin source.

Run:  <venv python> integration/test_stream_finalize.py
Exit code 0 = all checks passed.
"""
import importlib.util
import json
import sys
import time
from pathlib import Path

_here = Path(__file__).resolve().parent
_candidates = [_here / "hermes_plugin" / "antiloop_stream" / "__init__.py",
               _here.parent / "integration" / "hermes_plugin" / "antiloop_stream" / "__init__.py"]
SRC = next((c for c in _candidates if c.exists()), _candidates[0])
spec = importlib.util.spec_from_file_location("antiloop_stream_finalize_under_test", SRC)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failures = []
checks = 0


def check(name, ok, detail=""):
    global checks
    checks += 1
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(name)


SENTENCE = ("The careful technician checks every monitor before starting the scheduled evening "
            "maintenance. ")
THREE = SENTENCE * 3          # 285 chars: ends inside the last <CHECK_EVERY_CHARS window
FOUR = SENTENCE * 4           # 380 chars: crosses a cadence boundary, so delta already fires


def fresh():
    mod._turns.clear()
    mod._detections.clear()


def feed(sid, tid, text, chunk=40):
    for i in range(0, len(text), chunk):
        mod.on_stream_delta(session_id=sid, turn_id=tid, delta=text[i:i + chunk], kind="text")


def records_for(sid):
    if not mod.LOG.exists():
        return []
    out = []
    for line in mod.LOG.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("session_id") == sid and d.get("event") != "output_truncated":
            out.append(d)
    return out


# 1. the defect: three identical sentences must be detected when the stream ends
fresh()
sid = "flush-three-%d" % int(time.time())
feed(sid, "t1", THREE)
before = len(records_for(sid))
mod.on_stream_end(session_id=sid, turn_id="t1")
recs = records_for(sid)
check("three-sentence tail is analysed at stream end", len(recs) > before,
      "records=%d" % len(recs))
if recs:
    r = recs[-1]
    check("detection kind is sentence", r.get("kind") == "sentence", str(r.get("kind")))
    check("repeats == 3", r.get("repeats") == 3, str(r.get("repeats")))
    check("session attribution kept", r.get("session_id") == sid, str(r.get("session_id")))
    check("turn attribution kept", r.get("turn_id") == "t1", str(r.get("turn_id")))
    check("timestamp is current", time.time() - float(r.get("ts") or 0) < 60)

# 2. empty / unknown turn must be harmless
fresh()
mod.on_stream_end(session_id="never-started", turn_id="t0")
check("stream end with no buffered turn is a no-op", len(records_for("never-started")) == 0)

# 3. a turn already detected during streaming must not be recorded twice
fresh()
sid2 = "flush-dup-%d" % int(time.time())
feed(sid2, "t1", FOUR)                       # crosses a cadence boundary -> delta already fires
first = len(records_for(sid2))
mod.on_stream_end(session_id=sid2, turn_id="t1")
after = records_for(sid2)
check("delta detected the four-sentence case", first >= 1, "records=%d" % first)
check("no duplicate record from the final flush", len(after) == first, "%d -> %d" % (first, len(after)))

# 4. a repeated stream end must not duplicate either
fresh()
sid3 = "flush-idem-%d" % int(time.time())
feed(sid3, "t1", THREE)
mod.on_stream_end(session_id=sid3, turn_id="t1")
n1 = len(records_for(sid3))
mod.on_stream_end(session_id=sid3, turn_id="t1")
n2 = len(records_for(sid3))
check("second stream end adds nothing", n1 == n2 and n1 >= 1, "%d -> %d" % (n1, n2))

# 5. thresholds and cadence unchanged
check("SENTENCE_REPEATS unchanged", mod.SENTENCE_REPEATS == 3)
check("PHRASE_REPEATS unchanged", mod.PHRASE_REPEATS == 4)
check("CHECK_EVERY_CHARS unchanged", mod.CHECK_EVERY_CHARS == 120)

print("\n%d checks, %d failed" % (checks, len(failures)))
if failures:
    print("FAILED:", ", ".join(failures))


def test_stream_finalisation_checks():
    """pytest entry point: the standalone checks above must all have passed."""
    assert not failures, "failed checks: " + ", ".join(failures)


if __name__ == "__main__":
    sys.exit(1 if failures else 0)
