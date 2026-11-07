#!/usr/bin/env python3
"""Dry-run the llama.cpp/Strata log parser on a log file (no service, no network, read-only).

    python tools/check_llama_log.py path/to/strata.log

If it prints "0 timing lines recognised" the log format differs from what collector.py expects;
adjust the regular expressions before enabling collector.llama_log_path."""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from antiloop_watchdog.collector import parse_lines  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    lines = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").splitlines()
    found, _ = parse_lines(lines)
    stats = defaultdict(lambda: [0, 0, 0.0])
    for slot, phase, tokens, dur in found:
        s = stats[(slot, phase)]
        s[0] += 1
        s[1] += tokens
        s[2] += dur
    print(f"{len(found)} timing lines recognised in {len(lines)} lines")
    for (slot, phase), (n, tok, dur) in sorted(stats.items()):
        print(f"  slot {slot:>7} {phase:<8} blocks={n:<5} tokens={tok:<8} avg={tok / dur if dur else 0:8.1f} tok/s")
    return 0 if found else 1


if __name__ == "__main__":
    raise SystemExit(main())
