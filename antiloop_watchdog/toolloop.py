"""Tool-call loop detection.

Every tool call becomes a record with a normalised *action fingerprint* (tool name +
normalised arguments, volatile keys such as ids and timestamps removed) and, when the
result is reported, a *result fingerprint*. Raw arguments and results are never stored.

State epochs make legitimate repetition safe: a successful write whose fingerprint is new,
or an explicit `state` event, starts a new epoch. Repetition counts only consider records
of the current epoch, so "edit, run tests, edit, run tests" is never a loop, while
"run tests, run tests, run tests" with nothing changed in between is.
"""
from __future__ import annotations

import json
import re
from collections import Counter, deque
from dataclasses import dataclass
from typing import Callable, List, Optional

from .schema import Detection
from .util import sha

TARGET_KEYS = ("path", "file_path", "filepath", "file", "filename", "target", "url", "uri",
               "command", "cmd", "query", "q", "pattern")
_DIGITS = re.compile(r"\d+")
_HEX = re.compile(r"0x[0-9a-f]+")
_WS = re.compile(r"\s+")


@dataclass
class ToolRecord:
    call_id: Optional[str]
    tool: str
    category: str
    action_fp: str
    target_fp: str
    target_sample: Optional[str]
    epoch: int
    ts: float
    tokens: Optional[int] = None
    status: str = "unknown"
    result_fp: Optional[str] = None
    error_fp: Optional[str] = None
    resolved: bool = False


def normalize_args(value, volatile: set, depth: int = 0):
    if depth > 6:
        return "<deep>"
    if isinstance(value, dict):
        return {str(k): normalize_args(v, volatile, depth + 1)
                for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
                if str(k).lower() not in volatile}
    if isinstance(value, (list, tuple)):
        return [normalize_args(v, volatile, depth + 1) for v in value[:50]]
    if isinstance(value, str):
        s = _WS.sub(" ", value).strip()
        return sha(s, 16) + f":{len(s)}" if len(s) > 2000 else s
    return value


def extract_target(args) -> str:
    if isinstance(args, str):
        return _WS.sub(" ", args).strip()[:300]
    if isinstance(args, dict):
        low = {str(k).lower(): v for k, v in args.items()}
        for key in TARGET_KEYS:
            v = low.get(key)
            if isinstance(v, str) and v.strip():
                return _WS.sub(" ", v.replace("\\", "/")).strip()[:300]
            if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                return " ".join(v)[:300]
    return ""


def normalize_error(text: str) -> str:
    t = _WS.sub(" ", text.lower()).strip()
    t = _HEX.sub("0x#", t)
    return _DIGITS.sub("#", t)


class ToolLoopDetector:
    def __init__(self, cfg: Callable[[], dict]):
        self.cfg = cfg
        self.records: deque = deque()
        self.epoch = 0
        self.calls_total = 0
        self.calls_since_bump = 0
        self.recent_write_fps: deque = deque(maxlen=16)
        self._rx_cfg = None
        self._rx: dict = {}
        self._pend: dict = {}

    def reset(self) -> None:
        self.__init__(self.cfg)

    # ------------------------------------------------------------------ helpers

    def _compiled(self, tc: dict) -> dict:
        if self._rx_cfg is not tc:
            self._rx = {
                "exempt": [re.compile(p, re.I) for p in tc["exempt_tools"]],
                "poll": [re.compile(p, re.I) for p in tc["poll_tools"]],
                "cats": {c: [re.compile(p, re.I) for p in pats] for c, pats in tc["tool_categories"].items()},
                "volatile": {k.lower() for k in tc["volatile_arg_keys"]},
            }
            self._rx_cfg = tc
        return self._rx

    def _category(self, tool: str, rx: dict) -> str:
        for cat in ("write", "search", "read", "exec"):
            if any(p.search(tool) for p in rx["cats"].get(cat, [])):
                return cat
        return "other"

    def bump_epoch(self) -> None:
        self.epoch += 1
        self.calls_since_bump = 0

    def _trim(self, tc: dict) -> None:
        while len(self.records) > tc["history"]:
            self.records.popleft()

    # ------------------------------------------------------------------ ingestion

    def on_call(self, now: float, tool: str, args, call_id: Optional[str] = None, tokens: Optional[int] = None,
                status: str = "unknown", result_text: Optional[str] = None,
                result_hash: Optional[str] = None, keep_target: bool = False) -> List[Detection]:
        tc = self.cfg()["tool_loop"]
        if not tc["enabled"]:
            return []
        rx = self._compiled(tc)
        if any(p.search(tool) for p in rx["exempt"]):
            return []
        name = tool.strip().lower()
        category = self._category(name, rx)
        norm = normalize_args(args, rx["volatile"])
        action_fp = sha(json.dumps([name, norm], sort_keys=True, separators=(",", ":"), default=str))
        target = extract_target(args)
        rec = ToolRecord(call_id=call_id, tool=name, category=category, action_fp=action_fp,
                         target_fp=sha(f"{category}:{target}", 12), target_sample=target[:120] if keep_target else None,
                         epoch=self.epoch, ts=now, tokens=tokens)
        # A new, state-changing write starts a new epoch (and belongs to it).
        if category == "write" and status != "error" and action_fp not in self.recent_write_fps:
            self.bump_epoch()
            rec.epoch = self.epoch
        if category == "write":
            self.recent_write_fps.append(action_fp)
        self.records.append(rec)
        self.calls_total += 1
        self.calls_since_bump += 1
        self._trim(tc)
        if call_id:
            self._pending()[call_id] = rec
        if status != "unknown" or result_text is not None or result_hash is not None:
            self._finalize(rec, status, result_text, result_hash)
        return self._evaluate(rec, tc, name)

    def _pending(self) -> dict:
        if len(self._pend) > 256:
            self._pend.clear()
        return self._pend

    def on_result(self, now: float, tool: Optional[str], args, call_id: Optional[str], status: str,
                  result_text: Optional[str], result_hash: Optional[str],
                  tokens: Optional[int] = None, keep_target: bool = False) -> List[Detection]:
        tc = self.cfg()["tool_loop"]
        if not tc["enabled"]:
            return []
        rec = self._pending().pop(call_id, None) if call_id else None
        if rec is None and tool:
            name = tool.strip().lower()
            for r in reversed(self.records):
                if not r.resolved and r.tool == name:
                    rec = r
                    break
        if rec is None:
            if not tool:
                return []
            return self.on_call(now, tool, args, call_id, tokens, status, result_text, result_hash, keep_target)
        self._finalize(rec, status, result_text, result_hash)
        return self._evaluate(rec, tc, rec.tool)

    def _finalize(self, rec: ToolRecord, status: str, text: Optional[str], rhash: Optional[str]) -> None:
        rec.resolved = True
        rec.status = status
        if text is not None:
            if status == "error":
                rec.error_fp = sha(normalize_error(text), 12)
                rec.result_fp = rec.error_fp
            else:
                rec.result_fp = sha(_WS.sub(" ", text).strip(), 12)
        elif rhash is not None:
            rec.result_fp = rhash[:12]
            if status == "error":
                rec.error_fp = rec.result_fp

    # ------------------------------------------------------------------ rules

    def _evaluate(self, rec: ToolRecord, tc: dict, name: str) -> List[Detection]:
        rx = self._compiled(tc)
        mult = tc["poll_threshold_multiplier"] if any(p.search(name) for p in rx["poll"]) else 1
        out: List[Detection] = []
        same = [r for r in self.records if r.action_fp == rec.action_fp and r.epoch == rec.epoch]
        n = len(same)
        waste = sum(r.tokens for r in same[1:] if r.tokens) or None
        distinct_results = len({r.result_fp for r in same if r.result_fp})
        has_unknown = any(r.result_fp is None for r in same)

        def det(type_: str, conf: float, evidence: dict) -> Detection:
            return Detection(family="tool", type=type_, confidence=round(min(0.98, conf), 3), repetitions=n,
                             tool_fp=rec.action_fp, wasted_tokens=waste,
                             evidence={"tool": rec.tool, "category": rec.category, "epoch_repeats": n,
                                       "target_fp": rec.target_fp, **evidence},
                             sample=rec.target_sample)

        fails = [r for r in same if r.status == "error"]
        thr = tc["failed_repeat_threshold"] * mult
        if rec.status == "error" and len(fails) >= thr:
            top_err = max(Counter(r.error_fp for r in fails).values()) if fails else 0
            same_err = top_err >= thr
            conf = (0.88 if same_err else 0.70) + 0.04 * (len(fails) - thr)
            n = len(fails)
            out.append(det("tool_repeat_failed", conf, {"identical_error": same_err, "failures": len(fails)}))
        elif rec.category == "write" and n >= tc["edit_repeat_threshold"] * mult:
            out.append(det("tool_repeat_edit", 0.80 + 0.05 * (n - tc["edit_repeat_threshold"] * mult), {}))
        elif rec.category == "read" and n >= tc["read_repeat_threshold"] * mult and distinct_results <= 1:
            base = 0.60 if has_unknown else 0.70
            out.append(det("tool_repeat_read", base + 0.06 * (n - tc["read_repeat_threshold"] * mult),
                           {"result_known": not has_unknown}))
        elif rec.category == "search" and n >= tc["search_repeat_threshold"] * mult and distinct_results <= 1:
            base = 0.60 if has_unknown else 0.65
            out.append(det("tool_repeat_search", base + 0.06 * (n - tc["search_repeat_threshold"] * mult),
                           {"result_known": not has_unknown}))
        elif (rec.category in ("exec", "other") and rec.status != "error"
              and n >= tc["identical_cycle_threshold"] * mult and distinct_results <= 1):
            base = 0.55 if has_unknown else 0.65
            out.append(det("tool_repeat_call", base + 0.06 * (n - tc["identical_cycle_threshold"] * mult), {}))

        cyc = self._cycle(rec, tc)
        if cyc:
            out.append(cyc)
        return out

    def _cycle(self, rec: ToolRecord, tc: dict) -> Optional[Detection]:
        R = tc["cycle_repeats"]
        recs = [r for r in self.records if r.epoch == rec.epoch]
        seq = [(r.action_fp, r.result_fp or "") for r in recs]
        for p in range(2, tc["cycle_max_period"] + 1):
            need = p * R
            if len(seq) < need:
                continue
            if any(seq[-i] != seq[-i - p] for i in range(1, p * (R - 1) + 1)):
                continue
            unit = [a for a, _ in seq[-p:]]
            if len(set(unit)) < 2:
                continue
            reps = R
            while len(seq) >= p * (reps + 1) and all(seq[-i] == seq[-i - p] for i in range(1, p * reps + 1)):
                reps += 1
            tokens = sum(r.tokens for r in recs[-p * (reps - 1):] if r.tokens) or None
            return Detection(
                family="tool", type="tool_cycle", confidence=round(min(0.98, 0.72 + 0.08 * (reps - R)), 3),
                repetitions=reps, tool_fp=_canonical_cycle(unit), wasted_tokens=tokens,
                evidence={"period_calls": p, "cycle_repeats": reps})
        return None


def _canonical_cycle(unit: list) -> str:
    n = len(unit)
    best = min(range(n), key=lambda i: unit[i:] + unit[:i])
    return sha("|".join(unit[best:] + unit[:best]))
