#!/usr/bin/env python3
"""Reference Hermes-side shim (pull model). Demonstrates the whole contract with a simulated agent.

Real Hermes integration = replace `SimulatedAgent` with hooks into Hermes' own event stream and
replace `apply_directive` with Hermes' real session-control call (UNVERIFIED: see
HERMES_WATCHDOG_INTEGRATION.md, "What still has to be verified on the Hermes side").

    python examples/hermes_shim_example.py --state-dir state --url http://127.0.0.1:8791
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from antiloop_watchdog.client import WatchdogClient, WatchdogUnavailable  # noqa: E402


def apply_directive(session_id: str, directive: dict) -> bool:
    """Replace with the real Hermes call that injects `message` into THIS session (redirect)
    or stops THIS session's current turn (interrupt). Return True if it was applied."""
    print(f"[hermes:{session_id}] applying {directive['action']}: {directive['message'][:70]}...")
    return True


def pump(wd: WatchdogClient, session_id: str) -> None:
    """Call this between agent steps (cheap: one GET). Fail-open: watchdog problems never stop Hermes."""
    try:
        for d in wd.directives(session_id):                    # marks them 'delivered'
            ok = apply_directive(session_id, d)
            wd.ack(session_id, d["id"], "applied" if ok else "failed")
    except WatchdogUnavailable:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--url", default="http://127.0.0.1:8791")
    ap.add_argument("--session", default="hermes-demo-1")
    a = ap.parse_args()
    wd = WatchdogClient.from_files(a.state_dir, a.url)
    sid = a.session

    print("health:", wd.health()["status"])
    wd.state_changed(sid)                                      # creates the session
    wd.set_mode(sid, "warn")                                   # observe | warn | automatic
    # --- simulated agent: runs the same failing command four times --------------------------------
    for step in range(4):
        r = wd.tool_call(sid, "terminal", {"command": "pytest -x"}, status="error",
                         result_text="FAILED tests/test_a.py::test_x - assert 1 == 2")
        for det in r["detections"]:
            print(f"step {step}: ALERT {det['detection_type']} confidence={det['confidence']} reps={det['repetitions']}")
        pump(wd, sid)
    print("session:", {k: wd.session(sid)[k] for k in ("mode", "pending_directives", "detections_total")})
    print("recent events:", [e["event_type"] for e in wd.events(session_id=sid)["events"]])


if __name__ == "__main__":
    main()
