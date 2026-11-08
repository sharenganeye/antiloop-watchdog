"""Antiloop streaming text-loop detector + final-output protection.

Layers A and B of the accepted design (owner authorization 2026-11-07, rid 44).

A - on_stream_start/delta/end: detect a word/phrase loop (same 8 tokens repeated 4x) or a
    sentence loop (same sentence of >= 8 words repeated 3x) inside ONE turn, keyed strictly by
    (session_id, turn_id). Records one JSONL line per loop episode, forwards one text_delta event
    to the watchdog when it is reachable, and never touches the agent, the generation, or any
    session it was not called for.
B - transform_llm_output: if and only if a confirmed detection exists for this session and the
    response text really does contain that repeated unit, replace the response with the meaningful
    prefix plus an explicit truncation marker. Otherwise returns None and Hermes keeps the text.

Fail-open everywhere: any exception, any missing correlation, any doubt -> the original text.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

NGRAM = 8                 # tokens in a phrase unit
PHRASE_REPEATS = 4        # identical consecutive 8-token blocks
SENTENCE_MIN_WORDS = 8
SENTENCE_REPEATS = 3
STRUCTURED_REPEATS = 6    # stricter threshold for lists / code / tables
BUFFER_CHARS = 12_000     # bounded rolling buffer per turn
CHECK_EVERY_CHARS = 120   # run the detector at most this often
DETECTION_TTL_S = 900     # detection record must outlive on_stream_end until the transform hook
MAX_TURNS = 64            # bounded number of tracked turns

_here = Path(__file__).resolve()
# deployed as <profile>/plugins/<plugin>/__init__.py -> profile home is two levels up
ROOT = _here.parents[2] if _here.parents[1].name == "plugins" else _here.parents[1]
STATE_DIR = ROOT / "antiloop-stream"
LOG = STATE_DIR / "detections.jsonl"
# Optional local settings, never committed: {"token_file": "...", "base_url": "..."}
_LOCAL = {}
try:
    _local_file = Path(__file__).with_name("local.json")
    if _local_file.exists():
        _LOCAL = json.loads(_local_file.read_text(encoding="utf-8")) or {}
except Exception:
    _LOCAL = {}

TOKEN_FILE = Path(os.environ.get("WATCHDOG_TOKEN_FILE")
                  or _LOCAL.get("token_file")
                  or (Path.home() / "antiloop-watchdog" / "state" / "token"))
WATCHDOG = os.environ.get("WATCHDOG_URL", "http://127.0.0.1:8791")

_lock = threading.Lock()
_turns: dict[tuple[str, str], dict] = {}
_detections: dict[str, list[dict]] = {}             # session_id -> [detection, ...] (TTL bounded)


# --------------------------------------------------------------------------- helpers
def _canon_url(text: str) -> str:
    return re.sub(r"https?://", "", text)



def _unit_regex(unit: str):
    """Whitespace-tolerant pattern for a unit, so a loop broken across newlines still matches."""
    toks = unit.split()
    if not toks:
        return None
    try:
        return re.compile(r"\s+".join(re.escape(t) for t in toks))
    except re.error:
        return None


def _find_spans(text: str, unit: str, limit: int = 24):
    """All (start, end) matches of the unit in text, tolerating any whitespace between tokens."""
    pat = _unit_regex(unit)
    if pat is None:
        return []
    # the unit is normalised to lower case for counting, so the search must be case-insensitive
    # or a capitalised loop is detected and then never truncated (P1/C1)
    pat = re.compile(pat.pattern, pat.flags | re.IGNORECASE)
    return [(m.start(), m.end()) for m in pat.finditer(text)][:limit]

def _structured(text: str, unit: str, positions=None) -> bool:
    """True when the repetition is legitimate structure (list, table, boilerplate code).

    A veto, not a threshold bump: a real list of 40 rows repeats far more than any loop, so
    counting can never separate the two. Two discriminators instead:
    1. the text between consecutive occurrences varies (Station 1, Station 2, ...) -> legit list;
    2. the first occurrence sits inside an open code fence -> boilerplate, not degeneration.
    """
    if re.match(r"^\s*(?:[-*\u2022]|\d+[.)])\s", unit):
        return True
    if any(tok in unit for tok in ("{", "}", ";", "==", "def ", "class ", "return", "//")):
        return True
    if unit.count("|") >= 2:                        # table row
        return True
    if positions and len(positions) >= 2:
        head = text[:positions[0][0]]
        if head.count("```") % 2 == 1:              # inside a code fence
            return True
        spans = set()
        for (a, ea), (b, _eb) in zip(positions, positions[1:]):
            spans.add(text[ea:b].strip()[:80])
        if len(spans) > 1:                          # each repeat differs -> legitimate structure
            return True
    if any(line.strip()[:2] in ("|-", "| ", "|:", "|=") for line in text.splitlines()):
        return True
    return False


def _phrase_loop(text: str):
    """Any 8-token sequence occurring 4+ times in the recent window, whatever its period."""
    toks = re.findall(r"\S+", text.lower())
    if len(toks) < NGRAM * PHRASE_REPEATS:
        return None
    window = toks[-NGRAM * 16:]
    counts = Counter(tuple(window[i:i + NGRAM]) for i in range(len(window) - NGRAM + 1))
    if not counts:
        return None
    unit, best = counts.most_common(1)[0]
    best_unit = " ".join(unit)
    positions = _find_spans(text, best_unit)
    if _structured(text, best_unit, positions):
        return None
    need = PHRASE_REPEATS
    if best >= need:
        return {"kind": "phrase", "unit": best_unit, "repeats": best,
                "confidence": round(min(0.99, 0.7 + 0.05 * (best - need + 1)), 2)}
    return None


def _sentence_loop(text: str):
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]
    sents = [p for p in parts if len(p.split()) >= SENTENCE_MIN_WORDS]
    if len(sents) < SENTENCE_REPEATS:
        return None
    counts = Counter(sents)
    unit, n = counts.most_common(1)[0]
    positions = _find_spans(text, unit)
    if _structured(text, unit, positions):
        return None
    need = SENTENCE_REPEATS
    if n >= need:
        return {"kind": "sentence", "unit": unit, "repeats": n,
                "confidence": round(min(0.99, 0.7 + 0.05 * (n - need + 1)), 2)}
    return None


def analyse(text: str):
    """Return the strongest confirmed loop in ``text`` or None. Pure, no side effects."""
    try:
        if not text or len(text) < 80:
            return None
        cands = [c for c in (_phrase_loop(text), _sentence_loop(text)) if c]
        if not cands:
            return None
        return max(cands, key=lambda c: c["confidence"])
    except Exception:
        return None


def truncate(text: str, unit: str):
    """Keep the meaningful prefix, drop the repeated tail, add an explicit marker."""
    try:
        if not unit:
            return None
        spans = _find_spans(text, unit, limit=6)
        if len(spans) < 2:
            spans = _find_spans(text, unit[:120], limit=6)
        if len(spans) < 2:
            return None
        second = spans[1][0]
        if second < 40:
            return None
        kept = text[:second].rstrip()
        if len(kept) < 40:
            return None
        marker = ("\n\n[Antiloop: output truncated. The response repeated the same text "
                  "more than three times, so the repetitive tail was removed. "
                  "The text above is unchanged.]")
        return kept + marker
    except Exception:
        return None


# --------------------------------------------------------------------------- store
def _remember(session_id: str, detection: dict) -> None:
    now = time.time()
    with _lock:
        items = [d for d in _detections.get(session_id, []) if now - d["ts"] < DETECTION_TTL_S]
        items.append(detection)
        _detections[session_id] = items[-4:]
        for sid, ds in list(_detections.items()):
            if not ds:
                _detections.pop(sid, None)


def _recall(session_id: str, response_text: str):
    now = time.time()
    with _lock:
        items = [d for d in _detections.get(session_id, []) if now - d["ts"] < DETECTION_TTL_S]
        _detections[session_id] = items
    for d in reversed(items):
        if d["unit"] and d["unit"][:120].casefold() in response_text.casefold():
            return d
    return None


def _log(record: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _forward(session_id: str, text: str, kind: str) -> None:
    """Best effort: let the watchdog record the same episode. Never blocks generation."""
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if not token:
            return
        body = json.dumps({"type": "text_delta", "session_id": session_id,
                           "text": _canon_url(text[:8000]), "kind": "answer", "final": True}).encode()
        req = urllib.request.Request(WATCHDOG + "/events", data=body, method="POST")
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=0.8).read()
    except (urllib.error.URLError, OSError, ValueError):
        return
    except Exception:
        return


# --------------------------------------------------------------------------- hooks
def on_stream_start(**kwargs):
    try:
        key = (str(kwargs.get("session_id") or ""), str(kwargs.get("turn_id") or ""))
        if not key[0]:
            return
        with _lock:
            if len(_turns) > MAX_TURNS:
                for k in sorted(_turns, key=lambda k: _turns[k]["ts"])[:16]:
                    _turns.pop(k, None)
            _turns[key] = {"buf": "", "checked": 0, "fired": False, "ts": time.time()}
    except Exception:
        return


def on_stream_delta(**kwargs):
    try:
        sid = str(kwargs.get("session_id") or "")
        tid = str(kwargs.get("turn_id") or "")
        delta = kwargs.get("delta") or ""
        key = (sid, tid)
        if not sid or not delta:
            return
        with _lock:
            st = _turns.get(key)
            if st is None:
                st = _turns[key] = {"buf": "", "checked": 0, "fired": False, "ts": time.time()}
            st["buf"] = (st["buf"] + delta)[-BUFFER_CHARS:]
            st["ts"] = time.time()
            buf, checked, fired = st["buf"], st["checked"], st["fired"]
        if fired or len(buf) - checked < CHECK_EVERY_CHARS:
            return
        with _lock:
            if key in _turns:
                _turns[key]["checked"] = len(buf)
        det = analyse(buf)
        if not det:
            return
        record = {"ts": time.time(), "session_id": sid, "turn_id": tid, **det,
                  "chars": len(buf)}
        with _lock:
            st = _turns.get(key)
            if st is None or st["fired"]:
                return
            st["fired"] = True
        _remember(sid, {"ts": record["ts"], "unit": det["unit"], "kind": det["kind"],
                        "repeats": det["repeats"], "turn_id": tid})
        _log(record)
        _forward(sid, buf[-4000:], det["kind"])
    except Exception:
        return


def on_stream_end(**kwargs):
    """Release only the stream buffer. The detection record deliberately survives for the
    transform hook (owner requirement: do not drop it at stream end)."""
    try:
        key = (str(kwargs.get("session_id") or ""), str(kwargs.get("turn_id") or ""))
        with _lock:
            _turns.pop(key, None)
    except Exception:
        return


def transform_llm_output(**kwargs):
    try:
        text = kwargs.get("response_text")
        sid = str(kwargs.get("session_id") or "")
        if not isinstance(text, str) or not text or not sid:
            return None
        det = _recall(sid, text)
        if not det:
            return None
        trimmed = truncate(text, det["unit"])
        if not trimmed or len(trimmed) >= len(text):
            return None
        _log({"ts": time.time(), "session_id": sid, "turn_id": kwargs.get("turn_id"),
              "event": "output_truncated", "kind": det["kind"], "repeats": det["repeats"],
              "before_chars": len(text), "after_chars": len(trimmed)})
        return trimmed
    except Exception:
        return None


def register(ctx):
    ctx.register_hook("on_stream_start", on_stream_start)
    ctx.register_hook("on_stream_delta", on_stream_delta)
    ctx.register_hook("on_stream_end", on_stream_end)
    ctx.register_hook("transform_llm_output", transform_llm_output)
