"""Harness for the Antiloop streaming detector (layer A) and truncation (layer B).

No Hermes needed: imports the plugin source directly. Run with the project venv.
"""
import importlib.util
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "hermes_plugin" / "antiloop_stream" / "__init__.py"
spec = importlib.util.spec_from_file_location("antiloop_stream_under_test", SRC)
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


UNIT = "the patient has no pain and the findings are unremarkable"
PHRASE = "and the ward is documented and the shift is closed and the corridor is quiet, "
WORD_LOOP = ("Summary of the ward round. " + PHRASE * 5).strip()
SENT = "All departments reported exactly the same result again today."
SENT_LOOP = ("The ward round started at eight. " + (SENT + " ") * 4).strip()
LIST_TEXT = "\n".join(f"{i}. Station {i}: the ward is documented and the shift is closed."
                      for i in range(1, 41))
TABLE_TEXT = "\n".join(f"| patient {i} | room {i} | stable | reviewed |"
                       for i in range(1, 30))
CODE_TEXT = "```python\n" + "\n".join("result = compute(value)" for _ in range(20)) + "\n```"
NORMAL_TEXT = ("The discharge letter needs three corrections: the admission date, the "
               "attending physician and the medication list. I checked the first two against "
               "the record; the medication list still has to be verified against the pharmacy "
               "export before the letter is finalised. After that the letter can be signed.")

# ---- A: detection -----------------------------------------------------------
det = mod.analyse(WORD_LOOP)
check("word loop detected", bool(det) and det["kind"] == "phrase", str(det)[:80])
check("word loop confidence high", bool(det) and det["confidence"] >= 0.7, str(det and det["confidence"]))
det2 = mod.analyse(SENT_LOOP)
check("sentence loop detected", bool(det2) and det2["kind"] == "sentence", str(det2)[:80])
check("normal prose not flagged", mod.analyse(NORMAL_TEXT) is None, str(mod.analyse(NORMAL_TEXT)))
check("numbered list not flagged", mod.analyse(LIST_TEXT) is None, str(mod.analyse(LIST_TEXT))[:60])
check("table rows not flagged", mod.analyse(TABLE_TEXT) is None, str(mod.analyse(TABLE_TEXT))[:60])
check("code block not flagged", mod.analyse(CODE_TEXT) is None, str(mod.analyse(CODE_TEXT))[:60])
check("empty/short text safe", mod.analyse("") is None and mod.analyse("short") is None)

# ---- A: session/turn isolation + dedup --------------------------------------
mod.on_stream_start(session_id="s1", turn_id="t1")
mod.on_stream_start(session_id="s2", turn_id="t1")
for chunk in [WORD_LOOP[i:i + 40] for i in range(0, len(WORD_LOOP), 40)]:
    mod.on_stream_delta(session_id="s1", turn_id="t1", delta=chunk)
    mod.on_stream_delta(session_id="s2", turn_id="t1", delta=chunk)
s1 = [d for d in mod._detections.get("s1", [])]
s2 = [d for d in mod._detections.get("s2", [])]
check("both sessions detected independently", len(s1) == 1 and len(s2) == 1, f"{len(s1)}/{len(s2)}")
mod.on_stream_delta(session_id="s1", turn_id="t1", delta=WORD_LOOP[-60:])
check("one detection per episode (dedup)", len(mod._detections.get("s1", [])) == 1,
      str(len(mod._detections.get("s1", []))))
mod.on_stream_start(session_id="s1", turn_id="t2")
for chunk in [WORD_LOOP[i:i + 40] for i in range(0, len(WORD_LOOP), 40)]:
    mod.on_stream_delta(session_id="s1", turn_id="t2", delta=chunk)
check("new turn is a new episode", len(mod._detections.get("s1", [])) == 2,
      str(len(mod._detections.get("s1", []))))

# ---- A: detection survives on_stream_end ------------------------------------
mod.on_stream_end(session_id="s1", turn_id="t2")
check("detection survives stream end", bool(mod._recall("s1", WORD_LOOP)), "")

# ---- B: truncation ----------------------------------------------------------
LOOPED_ANSWER = ("Discharge summary prepared. " + PHRASE * 8).strip()
out = mod.transform_llm_output(response_text=LOOPED_ANSWER, session_id="s1", turn_id="t2")
check("transform truncates a confirmed loop", bool(out) and "Antiloop: output truncated" in out,
      str(out)[-90:] if out else "None")
check("prefix preserved", bool(out) and out.startswith("Discharge summary prepared."), "")
check("tail removed", bool(out) and out.count(UNIT) <= 2, str(out.count(UNIT) if out else "n/a"))
check("never empty", bool(out) and len(out.strip()) > 40, str(len(out) if out else 0))
check("undetected response untouched",
      mod.transform_llm_output(response_text=NORMAL_TEXT, session_id="s9", turn_id="t9") is None)
check("no detection for this session -> untouched",
      mod.transform_llm_output(response_text=LOOPED_ANSWER, session_id="unknown", turn_id="x") is None)
check("undetected text in a session WITH a detection -> untouched",
      mod.transform_llm_output(response_text=NORMAL_TEXT, session_id="s1", turn_id="t2") is None)

# ---- fail-open on hostile input --------------------------------------------
for bad in [None, "", 12345, {"a": 1}]:
    try:
        r = mod.transform_llm_output(response_text=bad, session_id="s1", turn_id="t2")
        check(f"hostile response_text {type(bad).__name__} fails open", r is None)
    except Exception as exc:
        check(f"hostile response_text {type(bad).__name__} fails open", False, f"raised {exc}")
for kwargs in [{}, {"session_id": None}, {"session_id": "s1", "turn_id": None, "delta": None}]:
    try:
        mod.on_stream_delta(**kwargs)
        mod.on_stream_start(**kwargs)
        mod.on_stream_end(**kwargs)
        check(f"stream hooks tolerate {kwargs}", True)
    except Exception as exc:
        check(f"stream hooks tolerate {kwargs}", False, f"raised {exc}")

print(f"\n{checks - len(failures)}/{checks} checks passed")
sys.exit(0 if not failures else 1)
