"""Deterministic stagnation scoring from observable tool activity (no LLM involved).

Four signals, each normalised to [0, 1], combined with configurable weights:

  novelty            few distinct (action, result) pairs in the recent window
  errors             a growing streak of consecutive failed calls
  duplicate_results  the same result fingerprint returned again and again
  no_state_change    many calls since the last state change (new write / state event)

Exploration (many *different* reads) scores low on novelty and duplicates, so a long
legitimate investigation phase does not reach the alert score on its own.
"""
from __future__ import annotations

from collections import Counter
from typing import Callable, List

from .schema import Detection
from .toolloop import ToolLoopDetector
from .util import sha


class ProgressDetector:
    def __init__(self, cfg: Callable[[], dict]):
        self.cfg = cfg
        self.last_score = 0.0
        self.last_signals: dict = {}

    def reset(self) -> None:
        self.last_score = 0.0
        self.last_signals = {}

    def evaluate(self, tool: ToolLoopDetector) -> List[Detection]:
        pc = self.cfg()["progress"]
        if not pc["enabled"]:
            return []
        recs = list(tool.records)[-pc["window_calls"]:]
        n = len(recs)
        if n < pc["min_calls"]:
            return []
        pairs = [(r.action_fp, r.result_fp or "") for r in recs]
        novelty = len(set(pairs)) / n
        floor = pc["novelty_floor"]
        s_nov = max(0.0, min(1.0, (floor - novelty) / floor))

        streak = 0
        for r in reversed(recs):
            if r.status == "error":
                streak += 1
            else:
                break
        start = pc["error_streak_start"]
        s_err = 0.0 if streak < start else min(1.0, (streak - start + 1) / 4)

        fps = [r.result_fp for r in recs if r.result_fp]
        s_dup = 0.0
        if len(fps) >= 4:
            top = max(Counter(fps).values())
            if top >= 4:
                s_dup = max(0.0, min(1.0, (top / len(fps) - 0.5) / 0.5))

        k = tool.calls_since_bump
        s_chg = 0.0 if k < pc["no_change_calls"] else min(1.0, k / (2 * pc["no_change_calls"]))

        w = pc["weights"]
        score = (w["novelty"] * s_nov + w["errors"] * s_err
                 + w["duplicate_results"] * s_dup + w["no_state_change"] * s_chg)
        self.last_score = round(score, 3)
        self.last_signals = {"novelty_ratio": round(novelty, 3), "error_streak": streak,
                             "novelty": round(s_nov, 3), "errors": round(s_err, 3),
                             "duplicate_results": round(s_dup, 3), "no_state_change": round(s_chg, 3),
                             "calls_since_state_change": k, "window_calls": n}
        if score < pc["alert_score"]:
            return []
        top_action = Counter(r.action_fp for r in recs).most_common(1)[0][0]
        return [Detection(
            family="progress", type="stagnation", confidence=round(min(0.9, score), 3), repetitions=n,
            tool_fp=sha("stagnation:" + top_action, 16), evidence=dict(self.last_signals, score=round(score, 3)))]
