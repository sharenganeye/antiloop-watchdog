#!/usr/bin/env python3
"""Exit 0 if the watchdog answers /health (and, with --status, /status). Usable from any shell."""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8791")
    ap.add_argument("--status", action="store_true", help="also query /status using the token file")
    ap.add_argument("--token-file", default=str(ROOT / "state" / "token"))
    ap.add_argument("--timeout", type=float, default=3.0)
    a = ap.parse_args()
    try:
        with urllib.request.urlopen(a.url.rstrip("/") + "/health", timeout=a.timeout) as r:
            health = json.loads(r.read())
        out = {"health": health}
        if a.status:
            tok = Path(a.token_file).read_text(encoding="utf-8").strip()
            req = urllib.request.Request(a.url.rstrip("/") + "/status", headers={"Authorization": f"Bearer {tok}"})
            with urllib.request.urlopen(req, timeout=a.timeout) as r:
                st = json.loads(r.read())
            out["status"] = {k: st[k] for k in ("version", "uptime_s", "default_mode", "adapter", "sessions", "resource")}
        print(json.dumps(out, indent=2))
        return 0 if health.get("status") == "ok" else 1
    except Exception as exc:
        print(f"UNHEALTHY: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
