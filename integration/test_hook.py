#!/usr/bin/env python
"""End-to-end check of integration/hermes_hook.py against a live watchdog.

Runs the real bridge as a subprocess with real Hermes-shaped payloads on stdin
(exactly what a shell hook receives) and asserts the watchdog's observable state.

Usage:  .venv/Scripts/python.exe integration/test_hook.py
Exit 0 = all checks passed. Requires a running watchdog on 127.0.0.1:8791.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "integration" / "hermes_hook.py"
PY = sys.executable
BASE = os.environ.get("WATCHDOG_URL", "http://127.0.0.1:8791")
TOKEN = (ROOT / "state" / "token").read_text(encoding="utf-8").strip()

failures: list[str] = []
checks = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(name)


def api(method: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:  # 404 on reset of an unknown session is fine
        return {"_http_status": exc.code}


def run_hook(payload: dict, timeout: float = 15) -> tuple[int, str, int]:
    """Invoke the bridge exactly like a shell hook: JSON on stdin, stdout back."""
    t0 = time.perf_counter()
    proc = subprocess.run(
        [PY, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, int((time.perf_counter() - t0) * 1000)


def main() -> int:
    try:
        health = api("GET", "/health")
    except Exception as exc:  # noqa: BLE001
        print(f"watchdog not reachable at {BASE}: {exc}")
        return 2
    check("watchdog reachable", health.get("status") == "ok", BASE)

    # --- 1. a real Hermes tool-loop: same failing command three times ----------
    sid = "hook-test-toolloop"
    api("POST", f"/sessions/{sid}/reset", {})
    durations = []
    for i in range(3):
        rc, out, ms = run_hook({
            "hook_event_name": "pre_tool_call",
            "tool_name": "terminal",
            "tool_input": {"command": "python missing_script.py"},
            "session_id": sid,
            "profile": "klinikradar-lead",
            "cwd": str(ROOT),
            "extra": {"tool_call_id": f"call_{i}", "task_id": "t1"},
        })
        durations.append(ms)
        check(f"pre_tool_call #{i + 1} exit 0, silent", rc == 0 and out == "", f"{ms} ms")
        rc, out, ms = run_hook({
            "hook_event_name": "post_tool_call",
            "tool_name": "terminal",
            "tool_input": {"command": "python missing_script.py"},
            "session_id": sid,
            "profile": "klinikradar-lead",
            "cwd": str(ROOT),
            "extra": {"tool_call_id": f"call_{i}", "status": "error",
                      "result": "python: can't open file 'missing_script.py': [Errno 2] No such file or directory"},
        })
        durations.append(ms)
        check(f"post_tool_call #{i + 1} exit 0, silent", rc == 0 and out == "", f"{ms} ms")

    session = api("GET", f"/sessions/{sid}")
    check("6 events reached the watchdog", (session.get("events") or 0) >= 6, str(session.get("events")))
    names = sorted({d.get("detection_type") for d in session.get("active_detections") or []})
    check("tool loop detected (same failure 3x)", "tool_repeat_failed" in names,
          ",".join(n for n in names if n) or "none")

    # --- 2. text repetition through post_llm_call -----------------------------
    sid2 = "hook-test-textloop"
    api("POST", f"/sessions/{sid2}/reset", {})
    sentence = ("The build is still failing so I will run the exact same command again "
                "and check whether the output has changed in any way at all.")
    got = []
    for _ in range(3):
        rc, out, ms = run_hook({
            "hook_event_name": "post_llm_call",
            "session_id": sid2,
            "assistant_response": ("Ich wiederhole: " + sentence + " " + sentence + " " + sentence),
            "user_message": "continue",
            "model": "qwen3.8-flash-next-coder-iq1_m",
            "platform": "cli",
            "extra": {},
        })
        got.append(ms)
    check("post_llm_call exit 0, silent", rc == 0 and out == "", f"{max(got)} ms")
    ev2 = api("GET", f"/sessions/{sid2}")
    names2 = sorted({d.get("detection_type") for d in ev2.get("active_detections") or []})
    check("repeated text detected", bool(names2), ",".join(n for n in names2 if n) or "none")

    # --- 3. directive delivery via pre_llm_call (L1 stoploop protocol) --------
    sid3 = "hook-test-directive"
    api("POST", f"/sessions/{sid3}/reset", {})
    mode = api("POST", f"/sessions/{sid3}/mode", {"mode": "warn"})
    check("session mode set to warn", mode.get("mode") == "warn", json.dumps(mode.get("mode")))
    for i in range(3):
        run_hook({"hook_event_name": "post_tool_call", "tool_name": "read_file",
                  "tool_input": {"path": "a.txt"}, "session_id": sid3,
                  "extra": {"tool_call_id": f"r{i}", "status": "error", "result": "File not found"}})
    dirs = api("GET", f"/sessions/{sid3}").get("directives") or []
    check("warn mode queued a directive", bool(dirs), json.dumps([d.get("status") for d in dirs]))
    rc, out, ms = run_hook({"hook_event_name": "pre_llm_call", "session_id": sid3,
                            "user_message": "continue", "platform": "cli",
                            "is_first_turn": False, "model": "m", "extra": {}})
    try:
        parsed = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        parsed = {}
    ctx = parsed.get("context") or ""
    check("pre_llm_call emits {\"context\": ...}", bool(ctx), f"{ms} ms, {len(out)} chars")
    check("L1 context carries the STOPLOOP protocol", "STOPLOOP protocol" in ctx
          and "RESTATE the goal" in ctx and "skill_view(name='stoploop')" in ctx, ctx[:70])
    rc2, out2, _ = run_hook({"hook_event_name": "pre_llm_call", "session_id": sid3,
                             "user_message": "continue", "platform": "cli", "extra": {}})
    check("directive is not re-delivered after ack", out2.strip() == "", repr(out2[:60]))

    # --- 3b. L2: interrupt -> surgical block of the repeated call only --------
    sid4 = "hook-test-interrupt"
    api("POST", f"/sessions/{sid4}/reset", {})
    api("POST", f"/sessions/{sid4}/mode", {"mode": "automatic"})
    api("POST", f"/sessions/{sid4}/intervene", {"action": "interrupt", "message": "stop the loop"})
    rc, out, ms = run_hook({"hook_event_name": "pre_tool_call", "tool_name": "terminal",
                            "tool_input": {"command": "python missing_script.py"},
                            "session_id": sid4, "extra": {"tool_call_id": "i1"}})
    try:
        blk = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        blk = {}
    check("interrupt directive -> pre_tool_call blocks", blk.get("action") == "block",
          f"{ms} ms, {str(blk.get('message'))[:60]}")
    rc, out2, _ = run_hook({"hook_event_name": "pre_tool_call", "tool_name": "terminal",
                            "tool_input": {"command": "python missing_script.py"},
                            "session_id": sid4, "extra": {"tool_call_id": "i2"}})
    try:
        blk2 = json.loads(out2) if out2.strip() else {}
    except json.JSONDecodeError:
        blk2 = {}
    check("identical repeat stays blocked", blk2.get("action") == "block", out2[:60])
    rc, out4, _ = run_hook({"hook_event_name": "pre_tool_call", "tool_name": "terminal",
                            "tool_input": {"command": "python a_different_approach.py"},
                            "session_id": sid4, "extra": {"tool_call_id": "i3"}})
    check("L2 allows a materially different call", out4.strip() == "", repr(out4[:60]))
    rc, out3, _ = run_hook({"hook_event_name": "pre_tool_call", "tool_name": "terminal",
                            "tool_input": {"command": "other"}, "session_id": "hook-test-other",
                            "extra": {"tool_call_id": "i4"}})
    check("other sessions are untouched", out3.strip() == "", repr(out3[:60]))

    # --- 3c. L3: Hermes' native repeated-failure warning -> early directive ----
    sid5 = "hook-test-native"
    api("POST", f"/sessions/{sid5}/reset", {})
    api("POST", f"/sessions/{sid5}/mode", {"mode": "warn"})   # observe refuses manual interventions (409)
    run_hook({"hook_event_name": "post_tool_call", "tool_name": "terminal",
              "tool_input": {"command": "python missing_script.py"},
              "session_id": sid5,
              "extra": {"tool_call_id": "n1", "status": "error",
                        "result": "[Tool loop warning: repeated_exact_failure_warning; count=3; "
                                  "terminal has failed 3 times with identical arguments.]"
                                  " python: can't open file 'missing_script.py'"}})
    n5 = api("GET", f"/sessions/{sid5}")
    check("L3 native warning queued an early directive", bool(n5.get("directives")),
          json.dumps([d.get("source") for d in n5.get("directives") or []]))
    run_hook({"hook_event_name": "post_tool_call", "tool_name": "terminal",
              "tool_input": {"command": "python missing_script.py"}, "session_id": sid5,
              "extra": {"tool_call_id": "n2", "status": "error",
                        "result": "[Tool loop warning: repeated_exact_failure_warning; count=4]"}})
    n5b = api("GET", f"/sessions/{sid5}")
    open5 = [d for d in n5b.get("directives") or [] if d.get("status") in ("pending", "delivered")]
    check("L3 cooldown stops directive storms", len(open5) == 1, f"{len(open5)} open directives")
    for f in (ROOT / "integration" / "state").glob("*"):
        try:
            f.unlink()
        except OSError:
            pass

    # --- 3d. L2 fires from the detection itself (warn), not from observe ------
    def hammer(sid, cmd, rounds):
        outs = []
        for i in range(rounds):
            run_hook({"hook_event_name": "pre_tool_call", "tool_name": "terminal",
                      "tool_input": {"command": cmd}, "session_id": sid,
                      "extra": {"tool_call_id": f"{sid}-{cmd[:4]}-{i}"}})
            outs.append(run_hook({"hook_event_name": "post_tool_call", "tool_name": "terminal",
                                  "tool_input": {"command": cmd}, "session_id": sid,
                                  "extra": {"tool_call_id": f"{sid}-{cmd[:4]}-{i}", "status": "error",
                                            "result": "python: can't open file 'x': [Errno 2] No such file"}})[1])
        return outs

    sid6 = "hook-test-l2-warn"
    api("POST", f"/sessions/{sid6}/reset", {})
    api("POST", f"/sessions/{sid6}/mode", {"mode": "warn"})
    hammer(sid6, "python probe_missing.py", 3)
    rc, out6, _ = run_hook({"hook_event_name": "pre_tool_call", "tool_name": "terminal",
                            "tool_input": {"command": "python probe_missing.py"},
                            "session_id": sid6, "extra": {"tool_call_id": "l2-4"}})
    try:
        b6 = json.loads(out6) if out6.strip() else {}
    except json.JSONDecodeError:
        b6 = {}
    check("L2 blocks the 4th identical call in warn mode", b6.get("action") == "block",
          f"{out6[:80]}")
    check("L2 block carries the recovery protocol", "STOPLOOP protocol" in (b6.get("message") or ""),
          "")
    rc, out6b, _ = run_hook({"hook_event_name": "pre_tool_call", "tool_name": "terminal",
                             "tool_input": {"command": "python a_different_probe.py"},
                             "session_id": sid6, "extra": {"tool_call_id": "l2-5"}})
    check("L2 lets a different call through", out6b.strip() == "", repr(out6b[:60]))

    sid7 = "hook-test-l2-observe"
    api("POST", f"/sessions/{sid7}/reset", {})
    api("POST", f"/sessions/{sid7}/mode", {"mode": "observe"})
    hammer(sid7, "python probe_missing.py", 3)
    rc, out7, _ = run_hook({"hook_event_name": "pre_tool_call", "tool_name": "terminal",
                            "tool_input": {"command": "python probe_missing.py"},
                            "session_id": sid7, "extra": {"tool_call_id": "l2-o4"}})
    check("observe mode never blocks", out7.strip() == "", repr(out7[:60]))

    for f in (ROOT / "integration" / "state").glob("*"):
        try:
            f.unlink()
        except OSError:
            pass

    # --- 4. fail-open when the watchdog is down -------------------------------
    breaker = ROOT / "integration" / ".watchdog_down"
    env = dict(os.environ, WATCHDOG_URL="http://127.0.0.1:8799")
    payload_down = json.dumps({
        "hook_event_name": "pre_tool_call", "tool_name": "terminal",
        "tool_input": {"command": "x"}, "session_id": "hook-test-down", "extra": {}})
    try:
        breaker.unlink()
    except OSError:
        pass
    t0 = time.perf_counter()
    proc = subprocess.run([PY, str(HOOK)], input=payload_down,
                          capture_output=True, text=True, timeout=20, env=env)
    ms_down = int((time.perf_counter() - t0) * 1000)
    check("watchdog down -> exit 0, no output", proc.returncode == 0 and proc.stdout == "", f"{ms_down} ms")
    check("circuit breaker tripped", breaker.exists(), str(breaker))

    t0 = time.perf_counter()
    proc = subprocess.run([PY, str(HOOK)], input=payload_down,
                          capture_output=True, text=True, timeout=20, env=env)
    ms_open = int((time.perf_counter() - t0) * 1000)
    check("second call short-circuits (<400 ms)", proc.returncode == 0 and ms_open < 400, f"{ms_open} ms")

    breaker.unlink()
    rc, out, _ = run_hook({"hook_event_name": "pre_tool_call", "tool_name": "terminal",
                           "tool_input": {"command": "breaker reset probe"},
                           "session_id": "hook-test-breaker-reset", "extra": {"tool_call_id": "b1"}})
    check("breaker resets on the next successful call", rc == 0 and not breaker.exists(), "")

    # --- 5. malformed / hostile input ----------------------------------------
    for bad in ["", "not json", "[]", '{"hook_event_name":"pre_tool_call"}',
                json.dumps({"hook_event_name": "pre_tool_call", "session_id": "x" * 300,
                            "tool_name": "terminal", "tool_input": {"command": "y"}})]:
        proc = subprocess.run([PY, str(HOOK)], input=bad, capture_output=True, text=True, timeout=20)
        check(f"malformed input tolerated ({bad[:28]!r})", proc.returncode == 0, proc.stderr.strip()[:80])

    print("\n--- bridge latency (tool events) ---")
    print(f"min {min(durations)} ms / median {sorted(durations)[len(durations) // 2]} ms / max {max(durations)} ms")
    print(f"\n{checks - len(failures)}/{checks} checks passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
