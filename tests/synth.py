"""Synthetic test data derived from the measured Strata performance ranges."""
from __future__ import annotations

import random

SYL = ["ka", "to", "ri", "ven", "mol", "sa", "du", "pen", "lo", "xi", "ar", "bel", "cor", "fen", "gu",
       "hal", "ix", "jor", "kes", "lum", "nor", "ot", "pra", "qui", "rus", "sel", "tam", "ul", "vi", "wex",
       "yar", "zon", "ab", "ed", "il", "om", "un"]
_vrng = random.Random(1234)
VOCAB = sorted({"".join(_vrng.choice(SYL) for _ in range(_vrng.randint(1, 3))) for _ in range(6000)})

# measured baselines (tok/s)
RANGES = {"thinking": (40, 55), "answering": (34, 50), "tool_call": (50, 71), "prefill": (1500, 1800)}


def prose(rng: random.Random, words: int) -> str:
    out, n = [], 0
    while n < words:
        k = rng.randint(7, 16)
        sent = " ".join(rng.choice(VOCAB) for _ in range(k))
        out.append(sent.capitalize() + ".")
        n += k
    return " ".join(out)


def paragraph(seed: int, words: int = 90) -> str:
    return prose(random.Random(seed), words)


def block(rng: random.Random, phase: str):
    """(tokens, duration_s, tps) for one synthetic block inside the measured range of `phase`."""
    lo, hi = RANGES[phase]
    tps = rng.uniform(lo, hi)
    tokens = rng.randint(120, 600) if phase != "prefill" else rng.randint(2000, 20000)
    return tokens, tokens / tps, tps


def feed_text(eng, clock, sid, text, kind="answer", chunk=80, dt=0.3, final=True):
    dets = []
    for i in range(0, len(text), chunk):
        clock.advance(dt)
        dets.extend(eng.ingest({"type": "text_delta", "session_id": sid, "text": text[i:i + chunk],
                                "kind": kind})["detections"])
    if final:
        dets.extend(eng.ingest({"type": "text_delta", "session_id": sid, "text": "", "kind": kind,
                                "final": True})["detections"])
    return dets


def gen(eng, clock, sid, tokens, duration, phase="decode"):
    clock.advance(duration)
    return eng.ingest({"type": "generation", "session_id": sid, "phase": phase, "tokens": tokens,
                       "duration_s": duration})


def call(eng, clock, sid, tool, args, status="unknown", result=None, call_id=None, dt=1.0):
    clock.advance(dt)
    ev = {"type": "tool_call", "session_id": sid, "tool": tool, "args": args, "status": status}
    if result is not None:
        ev["result_text"] = result
    if call_id:
        ev["call_id"] = call_id
    return eng.ingest(ev)
