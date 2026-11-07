"""Optional incremental tailer for llama.cpp / Strata server logs.

Reads only the bytes appended since the previous poll (never re-parses the file), recognises
the standard llama.cpp timing lines and feeds *speed samples* to pseudo sessions named
``slot-<n>``. These pseudo sessions carry no text or tool events and can never be the target
of an intervention, so they cannot cause cross-session interruptions.

The regular expressions follow llama.cpp's usual output, for example::

    prompt eval time =  123.45 ms /  2000 tokens (  0.06 ms per token, 1620.00 tokens per second)
           eval time = 2345.67 ms /   120 tokens ( 19.55 ms per token,   51.16 tokens per second)

UNVERIFIED against your real Strata build: the format may differ. Test with
`python tools/check_llama_log.py <logfile>` before enabling `collector.llama_log_path`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, List, Tuple

RE_PROMPT = re.compile(r"prompt eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens")
RE_EVAL = re.compile(r"(?<!prompt )\beval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*(?:tokens|runs)")
RE_SLOT = re.compile(r"\bid\s+(\d+)\b")

MAX_READ = 262144
MAX_CARRY = 65536


def parse_lines(lines: List[str], slot: str = "unknown") -> Tuple[List[tuple], str]:
    """Returns ([(slot, phase, tokens, duration_s), ...], last_slot)."""
    out = []
    for line in lines:
        if "print_timing" in line or "slot release" in line or "slot launch" in line:
            m = RE_SLOT.search(line)
            if m:
                slot = m.group(1)
        m = RE_PROMPT.search(line)
        if m:
            out.append((slot, "prefill", int(m.group(2)), float(m.group(1)) / 1000.0))
            continue
        m = RE_EVAL.search(line)
        if m:
            out.append((slot, "decode", int(m.group(2)), float(m.group(1)) / 1000.0))
    return out, slot


class LlamaLogTailer:
    def __init__(self, path: str, sink: Callable[[str, str, int, float], None], from_start: bool = False):
        self.path = Path(path)
        self.sink = sink
        self.offset = 0
        self.carry = b""
        self.slot = "unknown"
        self.samples = 0
        self._attached = False
        self.from_start = from_start

    def poll(self) -> int:
        try:
            size = self.path.stat().st_size
        except OSError:
            return 0
        if not self._attached:
            self._attached = True
            if not self.from_start:
                self.offset = size
                return 0
        if size < self.offset:                      # rotated / truncated
            self.offset, self.carry = 0, b""
        if size == self.offset:
            return 0
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                data = fh.read(min(size - self.offset, MAX_READ))
        except OSError:
            return 0
        self.offset += len(data)
        data = self.carry + data
        parts = data.split(b"\n")
        self.carry = parts.pop()[-MAX_CARRY:]
        lines = [p.decode("utf-8", "replace") for p in parts]
        found, self.slot = parse_lines(lines, self.slot)
        for slot, phase, tokens, dur in found:
            try:
                self.sink(slot, phase, tokens, dur)
                self.samples += 1
            except Exception:
                pass
        return len(found)
