#!/usr/bin/env python
"""Post-update canary for the Antiloop <-> Hermes integration.

Run after every `hermes update` (or after editing a profile config):

    .venv/Scripts/python.exe integration/verify_integration.py

Exit 0 = integration intact. Exit 1 = something needs re-wiring; the failing check names it.
Read-only except for one `hermes hooks test` probe, which posts a single synthetic event.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8791"
PROFILES = ["klinikradar-lead", "klinikradar-research", "klinikradar-curator",
            "klinikradar-engineer", "klinikradar-auditor"]
EVENTS = ["pre_llm_call", "pre_tool_call", "post_tool_call", "post_llm_call",
          "post_api_request", "on_session_end"]
EXPECTED_BRIDGE_SHA = "d9d0f1e7f22887da399d518c19d3b08c8ec88b9c8e22d12236470fe79347a7cc"  # informational

failures: list[str] = []
checks = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(name)


def hermes(*args: str, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(["hermes", *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.loads(r.read())


def main() -> int:
    # 1. service
    try:
        h = get("/health")
        check("watchdog healthy", h.get("status") == "ok", f"uptime {h.get('uptime_s')}s")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        check("watchdog healthy", False, f"not reachable on {BASE}: {exc}")
        print("\nStart it with: scripts\\start_watchdog.ps1  (or python -m antiloop_watchdog)")
        return 1

    # 2. bridge script
    hook = ROOT / "integration" / "hermes_hook.py"
    sha = hashlib.sha256(hook.read_bytes()).hexdigest() if hook.exists() else ""
    check("bridge script present", hook.exists(), str(hook))
    if sha and sha != EXPECTED_BRIDGE_SHA:
        print(f"      note: bridge sha {sha[:12]} differs from the recorded {EXPECTED_BRIDGE_SHA[:12]}")

    # 3. profile wiring + consent
    for prof in PROFILES:
        rc, out = hermes("-p", prof, "hooks", "list")
        configured = "Configured shell hooks" in out
        missing = [ev for ev in EVENTS if f"[{ev}]" not in out]
        denied = "not allowlisted" in out or "not allowed" in out
        check(f"{prof}: 6 hook events configured", configured and not missing and not denied,
              f"rc={rc}" + (f", missing {missing}" if missing else "") + (", unapproved" if denied else ""))

    # 4. doctor on the lead profile
    rc, out = hermes("hooks", "doctor")
    bad = out.count("✗")
    ran = out.count("ran clean")
    check("hooks doctor clean", rc == 0 and bad == 0 and ran >= len(EVENTS),
          f"rc={rc}, failures={bad}, clean runs={ran}")

    # 5. a synthetic event must reach the watchdog
    before = {s.get("session_id") for s in get_sessions()}
    rc, out = hermes("hooks", "test", "post_tool_call")
    after = {s.get("session_id") for s in get_sessions()}
    new = after - before
    check("synthetic event ingested", rc == 0 and bool(new), f"new sessions: {sorted(new)[:2]}")

    # 6. report
    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("Re-wire with the block from integration/INTEGRATION_STATUS.md section 3.")
        return 1
    return 0


def get_sessions() -> list:
    req = urllib.request.Request(BASE + "/sessions")
    token = (ROOT / "state" / "token").read_text(encoding="utf-8").strip()
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read()).get("sessions", [])


if __name__ == "__main__":
    sys.exit(main())
