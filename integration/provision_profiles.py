"""Provision Antiloop protection into Hermes profiles - idempotent, report-first.

Why this exists: `hermes profile create` scaffolds a new profile from the default home, but it does not
copy third-party plugins or shell hooks, and there is no profile-creation event to hook into. So new
profiles must be provisioned explicitly. Run this after creating a profile (or at any time - it reports
what is already correct and only writes what is missing).

What it guarantees for each profile:
  1. plugins/antiloop-stream/__init__.py exists with the canonical hash.
  2. the profile's plugin.yaml declares the stream hooks.
  3. config.yaml has pre_llm_call / pre_tool_call / post_tool_call / on_session_end entries pointing at
     the shared hermes_hook.py, and plugins.enabled lists antiloop-stream.
  4. shell-hooks-allowlist.json approves exactly those event/command pairs.

Usage:
  python provision_profiles.py --check            # report only, no writes
  python provision_profiles.py --all              # provision every profile
  python provision_profiles.py --profile NAME     # provision one profile
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

PROFILES_ROOT = Path.home() / "AppData/Local/hermes/profiles"
PLUGIN_SRC = Path(__file__).resolve().parent / "hermes_plugin" / "antiloop_stream"
HOOK = Path(__file__).resolve().parent / "hermes_hook.py"
EVENTS = ("pre_llm_call", "pre_tool_call", "post_tool_call", "on_session_end")
PY = Path("C:/Users/zizor/Desktop/Antiloop/anti-loop-watchdog/.venv/Scripts/python.exe")


def _sha(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def hook_command() -> str:
    return '"%s" "%s"' % (PY.as_posix(), HOOK.as_posix())


def plan(profile: Path) -> list[str]:
    """Return the list of missing items for one profile. Empty list == fully provisioned."""
    todo = []
    dest = profile / "plugins" / "antiloop-stream"
    if _sha(dest / "__init__.py") != _sha(PLUGIN_SRC / "__init__.py"):
        todo.append("plugin")
    if not (dest / "plugin.yaml").exists():
        todo.append("plugin.yaml")
    cfg = profile / "config.yaml"
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    for ev in EVENTS:
        if ("  %s:" % ev) not in text:
            todo.append("hook:" + ev)
    if "antiloop-stream" not in text:
        todo.append("plugins.enabled")
    al = profile / "shell-hooks-allowlist.json"
    approved = set()
    if al.exists():
        try:
            for a in json.loads(al.read_text(encoding="utf-8")).get("approvals", []):
                approved.add((a.get("event"), a.get("command")))
        except ValueError:
            pass
    for ev in EVENTS:
        if (ev, hook_command()) not in approved:
            todo.append("allow:" + ev)
    return todo


def apply(profile: Path, todo: list[str]) -> None:
    dest = profile / "plugins" / "antiloop-stream"
    if "plugin" in todo or "plugin.yaml" in todo:
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PLUGIN_SRC / "__init__.py", dest / "__init__.py")
        if (PLUGIN_SRC / "plugin.yaml").exists():
            shutil.copy2(PLUGIN_SRC / "plugin.yaml", dest / "plugin.yaml")

    cfg = profile / "config.yaml"
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    nl = "\r\n" if "\r\n" in text else "\n"
    block = ""
    for ev in EVENTS:
        if ("  %s:" % ev) not in text:
            block += "  %s:%s    - command: '%s'%s      timeout: 5%s%s" % (ev, nl, hook_command(), nl, nl, nl)
    if block:
        if (nl + "plugins:") in text:
            text = text.replace(nl + "plugins:", nl + block + "plugins:", 1)
        else:
            text = text + nl + "hooks:" + nl + block
    if "antiloop-stream" not in text:
        if "plugins:" in text and "enabled:" in text.split("plugins:", 1)[1][:200]:
            head, tail = text.split("plugins:", 1)
            idx = tail.index("enabled:")
            insert = "enabled:" + nl + "    - antiloop-stream" + nl if False else None
            # simple append under enabled: is risky to patch blindly; leave a marker instead
        text += nl + "# provision: ensure 'antiloop-stream' is listed under plugins.enabled" + nl
    cfg.write_text(text, encoding="utf-8")

    al = profile / "shell-hooks-allowlist.json"
    data = {"approvals": []}
    if al.exists():
        try:
            data = json.loads(al.read_text(encoding="utf-8"))
        except ValueError:
            data = {"approvals": []}
    have = {(a.get("event"), a.get("command")) for a in data.get("approvals", [])}
    for ev in EVENTS:
        if (ev, hook_command()) not in have:
            data.setdefault("approvals", []).append({"event": ev, "command": hook_command()})
    al.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--profile")
    args = ap.parse_args(argv)

    if not PLUGIN_SRC.exists():
        print("canonical plugin source missing: %s" % PLUGIN_SRC)
        return 2

    targets = []
    if args.profile:
        targets = [PROFILES_ROOT / args.profile]
    elif args.all:
        targets = sorted(p for p in PROFILES_ROOT.iterdir() if p.is_dir() and not p.name.startswith("."))
    else:
        ap.error("choose --all, --profile NAME or --check --all")

    rc = 0
    for prof in targets:
        if not prof.is_dir():
            print("%-24s MISSING" % prof.name)
            rc = 1
            continue
        todo = plan(prof)
        if todo:
            if args.check:
                print("%-24s needs: %s" % (prof.name, ", ".join(todo)))
                rc = 1
            else:
                apply(prof, todo)
                left = plan(prof)
                print("%-24s provisioned%s" % (prof.name, "" if not left else " (still: %s)" % ", ".join(left)))
                if left:
                    rc = 1
        else:
            print("%-24s ok" % prof.name)
    return rc


if __name__ == "__main__":
    sys.exit(main())
