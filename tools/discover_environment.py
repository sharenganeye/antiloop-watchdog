#!/usr/bin/env python3
"""Read-only environment discovery. RUN THIS ON THE MACHINE THAT RUNS HERMES + STRATA.

It never changes anything: it checks the Python version, looks for a Hermes executable and asks it
for `--version` / `--help`, probes the local llama.cpp endpoint with GET requests only, and notes
whether common Hermes config directories exist (it does NOT read their contents).
Output: reports/environment.json  (give this file to the agent that finishes the Hermes integration).

    python tools/discover_environment.py [--llama http://127.0.0.1:8080] [--watchdog-port 8791]
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import urllib.request
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd, timeout=15, max_chars=6000):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr.strip() else "")
        return {"cmd": cmd, "returncode": p.returncode, "output": out[:max_chars]}
    except Exception as exc:
        return {"cmd": cmd, "error": f"{type(exc).__name__}: {exc}"}


def http_get(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read(200000).decode("utf-8", "replace")
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llama", default="http://127.0.0.1:8080")
    ap.add_argument("--watchdog-port", type=int, default=8791)
    ap.add_argument("--out", default=str(ROOT / "reports" / "environment.json"))
    a = ap.parse_args()

    rep = {"python": {"version": sys.version, "executable": sys.executable,
                      "tested_with": "3.12 (stdlib only; 3.9+ expected)"},
           "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()}}
    try:
        rep["platform"]["wsl"] = "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        rep["platform"]["wsl"] = False

    hermes = {"executables": {}, "packages": {}, "config_dirs_exist": {}}
    for name in ("hermes", "hermes-agent", "hermes.exe"):
        exe = shutil.which(name)
        if exe:
            hermes["executables"][name] = {"path": exe, "version": run([exe, "--version"]), "help": run([exe, "--help"])}
    for pkg in ("hermes-agent", "hermes_agent", "hermes", "nous-hermes"):
        try:
            hermes["packages"][pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            pass
    home = Path.home()
    for d in (home / ".hermes", home / ".config" / "hermes", Path(os.environ.get("APPDATA", "")) / "hermes",
              Path(os.environ.get("LOCALAPPDATA", "")) / "hermes"):
        if str(d) not in ("hermes", "."):
            hermes["config_dirs_exist"][str(d)] = d.exists()      # existence only, contents are never read
    hermes["found"] = bool(hermes["executables"] or hermes["packages"])
    rep["hermes"] = hermes

    ll = {"base_url": a.llama}
    st, body = http_get(a.llama.rstrip("/") + "/health")
    ll["health"] = {"status": st, "body": body[:200]}
    st, body = http_get(a.llama.rstrip("/") + "/props")
    if st == 200:
        try:
            props = json.loads(body)
            ll["props"] = {"total_slots": props.get("total_slots"),
                           "n_ctx": (props.get("default_generation_settings") or {}).get("n_ctx"),
                           "model_alias": props.get("model_alias") or props.get("model_path")}
        except ValueError:
            ll["props"] = "unparsable"
    else:
        ll["props"] = {"status": st}
    st, body = http_get(a.llama.rstrip("/") + "/slots")
    ll["slots_endpoint"] = {"status": st}
    if st == 200:
        try:
            slots = json.loads(body)
            ll["slots_endpoint"]["count"] = len(slots) if isinstance(slots, list) else None
        except ValueError:
            pass
    rep["llama_endpoint"] = ll

    s = socket.socket()
    s.settimeout(0.5)
    rep["watchdog_port"] = {"port": a.watchdog_port, "in_use": s.connect_ex(("127.0.0.1", a.watchdog_port)) == 0}
    s.close()

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print(f"python {platform.python_version()} on {rep['platform']['system']} (WSL={rep['platform']['wsl']})")
    print("hermes found:", hermes["found"], list(hermes["executables"]) or "", hermes["packages"] or "")
    print("llama endpoint health:", ll["health"]["status"], "| props:", ll.get("props"))
    print("watchdog port", a.watchdog_port, "in use:", rep["watchdog_port"]["in_use"])
    print("report written to", a.out)


if __name__ == "__main__":
    main()
