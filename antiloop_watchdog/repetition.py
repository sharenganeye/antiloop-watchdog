"""Incremental text-repetition detection.

Two independent mechanisms share one rolling buffer:

1. n-gram loop: the most recent `min_sequence_tokens` tokens are located in the rolling
   window (`window_tokens`). If that exact sequence occurs `threshold` times the text is
   looping. Exact comparison, no hash collisions. The check runs every
   `check_interval_tokens` new tokens, so work per token is tiny.
2. sentence repeat: the same normalised sentence (>= `sentence_min_words` words) seen
   `sentence_repeat_threshold` times in recent prose. Lower confidence by design.

False-positive guards: tool output / file content kinds are never inspected; fenced code,
`kind == "code"` and punctuation-heavy (JSON-like) windows need `code_threshold_multiplier`
times more repetitions; punctuation-only runs (rules, tables) are ignored; operator
supplied `ignore_patterns` are honoured.

Tokens are approximated by words and single punctuation characters (not model tokens).
"""
from __future__ import annotations

import re
from collections import Counter, deque
from typing import Callable, List, Optional

from .schema import Detection
from .util import sha

TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_DIGITS_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")


def least_rotation(seq: list) -> int:
    """Index of the lexicographically least rotation (O(n))."""
    n = len(seq)
    i, j, k = 0, 1, 0
    while i < n and j < n and k < n:
        a, b = seq[(i + k) % n], seq[(j + k) % n]
        if a == b:
            k += 1
            continue
        if a > b:
            i += k + 1
        else:
            j += k + 1
        if i == j:
            j += 1
        k = 0
    return min(i, j)


def canonical_fingerprint(unit: list) -> str:
    if not unit:
        return sha("")
    r = least_rotation(unit)
    return sha(" ".join(unit[r:] + unit[:r]))


def _is_punct(tok: str) -> bool:
    return not (tok[0].isalnum() or tok[0] == "_")


class RepetitionDetector:
    def __init__(self, cfg: Callable[[], dict]):
        self.cfg = cfg
        self.buf: List[str] = []
        self.flags: List[int] = []
        self.carry = ""
        self.sent_carry = ""
        self.since_check = 0
        self.bt_run = 0
        self.in_fence = False
        self.tokens_total = 0
        self.tokens_skipped = 0
        self.sent_hist: deque = deque(maxlen=64)      # (fingerprint, n_tokens, token_position)
        self._pat_cache = (None, [])

    # ------------------------------------------------------------------ public

    def reset(self) -> None:
        self.__init__(self.cfg)

    def feed(self, text: str, kind: str = "answer", final: bool = False) -> List[Detection]:
        cfg = self.cfg()["repetition"]
        if not cfg["enabled"]:
            return []
        if kind in cfg["ignore_kinds"]:
            self.tokens_skipped += len(text) // 4
            return []
        data = self.carry + text
        if final:
            cut = len(data)
        else:
            idx = max(data.rfind(" "), data.rfind("\n"), data.rfind("\t"))
            if idx < 0:
                if len(data) < 256:
                    self.carry = data
                    return []
                cut = len(data)
            else:
                cut = idx + 1
        part, self.carry = data[:cut], data[cut:]
        if not part:
            return []
        toks = TOKEN_RE.findall(part.lower())
        code_kind = 1 if kind == "code" else 0
        for t in toks:
            if t == "`":
                self.bt_run += 1
                if self.bt_run == 3:
                    self.in_fence = not self.in_fence
                    self.bt_run = 0
            else:
                self.bt_run = 0
            self.buf.append(t)
            self.flags.append(1 if (self.in_fence or code_kind) else 0)
        window = cfg["window_tokens"]
        if len(self.buf) > window + 128:
            drop = len(self.buf) - window
            del self.buf[:drop]
            del self.flags[:drop]
        self.tokens_total += len(toks)
        self.since_check += len(toks)

        out: List[Detection] = []
        if self.since_check >= cfg["check_interval_tokens"]:
            self.since_check = 0
            det = self._check_ngram(cfg)
            if det:
                out.append(det)
        if not (self.in_fence or code_kind):
            sd = self._check_sentences(part, cfg)
            if sd:
                out.append(sd)
        return out

    # ------------------------------------------------------------------ n-gram

    def _ignored(self, text: str, cfg: dict) -> bool:
        pats = cfg["ignore_patterns"]
        if not pats:
            return False
        if self._pat_cache[0] is not pats:
            self._pat_cache = (pats, [re.compile(p, re.I) for p in pats])
        return any(rx.search(text) for rx in self._pat_cache[1])

    def _check_ngram(self, cfg: dict) -> Optional[Detection]:
        window, L = cfg["window_tokens"], cfg["min_sequence_tokens"]
        buf = self.buf
        n = len(buf)
        start = max(0, n - window)
        if n - start < L + 1:
            return None
        tail = buf[n - L:]
        first = tail[0]
        occ = [i for i in range(start, n - L + 1) if buf[i] == first and buf[i:i + L] == tail]
        if len(occ) < 2:
            return None
        gaps = [b - a for a, b in zip(occ, occ[1:])]
        p = gaps[-1]
        if p >= L:
            reps, last = 1, occ[-1]                 # greedy count of non-overlapping occurrences
            for o in reversed(occ[:-1]):
                if last - o >= L:
                    reps += 1
                    last = o
        else:
            k = len(occ) - 1
            while k > 0 and occ[k] - occ[k - 1] == p:
                k -= 1
            reps = (occ[-1] - occ[k] + L) // p

        tail_flags = self.flags[n - L:]
        code_frac = sum(tail_flags) / L
        punct = sum(1 for t in tail if _is_punct(t))
        punct_ratio = punct / L
        if punct == L and len(set(tail)) <= 4:
            return None                              # separators / ascii rules
        structured = code_frac >= 0.5 or punct_ratio >= cfg["structured_punct_ratio"]
        thr = cfg["threshold"] * (cfg["code_threshold_multiplier"] if structured else 1)
        if reps < thr:
            return None
        unit = buf[n - p:] if p <= 256 else tail
        unit_text = " ".join(unit)
        if self._ignored(unit_text, cfg):
            return None
        conf = min(0.98, 0.78 + 0.08 * (reps - thr))
        return Detection(
            family="text", type="text_repeat", confidence=round(conf, 3), repetitions=reps,
            sequence_fp=canonical_fingerprint(unit), wasted_tokens=max(0, (reps - 1) * p),
            evidence={"period_tokens": p, "window_tokens": window, "min_sequence_tokens": L,
                      "structured": structured, "code_fraction": round(code_frac, 2),
                      "punct_ratio": round(punct_ratio, 2), "effective_threshold": thr},
            sample=unit_text)

    # ------------------------------------------------------------------ sentences

    def _check_sentences(self, part: str, cfg: dict) -> Optional[Detection]:
        data = self.sent_carry + part
        pieces = SENT_SPLIT_RE.split(data)
        self.sent_carry = pieces.pop() if pieces else ""
        if len(self.sent_carry) > 2000:               # runaway text without sentence ends
            self.sent_carry = self.sent_carry[-500:]
        best: Optional[Detection] = None
        window = cfg["window_tokens"] * 2
        for sent in pieces:
            norm = _WS_RE.sub(" ", _DIGITS_RE.sub("#", sent.lower())).strip()
            words = TOKEN_RE.findall(norm)
            alpha = [w for w in words if not _is_punct(w)]
            if len(alpha) < cfg["sentence_min_words"]:
                continue
            if sum(1 for w in words if _is_punct(w)) / max(1, len(words)) >= cfg["structured_punct_ratio"]:
                continue
            fp = sha(norm)
            pos = self.tokens_total
            while self.sent_hist and pos - self.sent_hist[0][2] > window:
                self.sent_hist.popleft()
            self.sent_hist.append((fp, len(words), pos))
            n = sum(1 for h in self.sent_hist if h[0] == fp)
            if n >= cfg["sentence_repeat_threshold"]:
                if self._ignored(norm, cfg):
                    continue
                conf = min(0.8, 0.5 + 0.1 * (n - cfg["sentence_repeat_threshold"]))
                det = Detection(
                    family="text", type="sentence_repeat", confidence=round(conf, 3), repetitions=n,
                    sequence_fp=fp, wasted_tokens=(n - 1) * len(words),
                    evidence={"sentence_tokens": len(words), "window_tokens": window}, sample=norm,
                    group="sentences")
                if best is None or det.confidence > best.confidence:
                    best = det
        return best
