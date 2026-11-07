"""Token speed monitor.

Speed is an *indicator only*: it never produces a Detection. The engine uses the level
solely as a small corroboration bonus when an independent detector has already fired.

Filtering rules (all configurable):
  * prefill blocks (phase == "prefill", or implausibly fast for generation) are ignored,
  * blocks with fewer than `min_block_tokens` tokens are ignored,
  * blocks shorter than `min_block_duration_s` seconds are ignored,
  * the rolling speed is token-weighted over `window_s` seconds and only reported once
    the window holds enough data (`min_window_blocks`, `min_window_duration_s`, `min_window_tokens`).
"""
from __future__ import annotations

from collections import deque
from typing import Callable, Optional


class SpeedMonitor:
    def __init__(self, cfg: Callable[[], dict]):
        self.cfg = cfg
        self.samples: deque = deque(maxlen=512)       # (ts, tokens, duration_s)
        self.level = "normal"
        self.last_block_tps: Optional[float] = None
        self.peak_block_tps: Optional[float] = None
        self.last_prefill_tps: Optional[float] = None
        self.last_phase: Optional[str] = None
        self.accepted = 0
        self.ignored = {"tiny_block": 0, "short_block": 0, "prefill": 0}
        self.baseline_counts = {"below": 0, "within": 0, "above": 0}

    def _sc(self) -> dict:
        return self.cfg()["speed"]

    def _baseline_label(self, sc: dict, phase: str, tps: float) -> Optional[str]:
        rng = sc["baseline"].get(phase)
        if not rng:
            return None
        lo, hi = rng
        if tps < lo * 0.9:
            return "below"
        if tps > hi * 1.1:
            return "above"
        return "within"

    def _level_for(self, rolling: Optional[float], sc: dict) -> str:
        if rolling is None:
            return "normal"
        if rolling >= sc["critical_tps"]:
            return "critical"
        if rolling >= sc["warning_tps"]:
            return "warning"
        return "normal"

    def rolling_tps(self, now: float) -> Optional[float]:
        sc = self._sc()
        cutoff = now - sc["window_s"]
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        if len(self.samples) < sc["min_window_blocks"]:
            return None
        toks = sum(s[1] for s in self.samples)
        dur = sum(s[2] for s in self.samples)
        if dur <= 0 or dur < sc["min_window_duration_s"] or toks < sc["min_window_tokens"]:
            return None
        return toks / dur

    def observe(self, now: float, phase: str, tokens: int, duration_s: float) -> dict:
        sc = self._sc()
        out = {"accepted": False, "reason": None, "tps": None, "level": self.level,
               "level_changed": False, "rolling_tps": None, "baseline": None}
        if not sc["enabled"]:
            out["reason"] = "disabled"
            return out
        raw_tps = tokens / duration_s if duration_s > 0 else float("inf")
        if phase == "prefill":
            return self._ignore(out, "prefill", raw_tps)
        if tokens < sc["min_block_tokens"]:
            return self._ignore(out, "tiny_block", raw_tps)
        if duration_s < sc["min_block_duration_s"]:
            return self._ignore(out, "short_block", raw_tps)
        if raw_tps >= sc["prefill_floor_tps"]:
            # physically implausible for decoding on this setup -> prompt processing mislabelled
            return self._ignore(out, "prefill", raw_tps)
        self.samples.append((now, tokens, duration_s))
        self.accepted += 1
        self.last_block_tps = raw_tps
        self.last_phase = phase
        self.peak_block_tps = raw_tps if self.peak_block_tps is None else max(self.peak_block_tps, raw_tps)
        label = self._baseline_label(sc, phase, raw_tps)
        if label:
            self.baseline_counts[label] += 1
        rolling = self.rolling_tps(now)
        new_level = self._level_for(rolling, sc)
        out.update(accepted=True, tps=raw_tps, rolling_tps=rolling, baseline=label, level=new_level,
                   level_changed=new_level != self.level)
        self.level = new_level
        return out

    def _ignore(self, out: dict, reason: str, raw_tps: float) -> dict:
        self.ignored[reason] += 1
        if reason == "prefill":
            self.last_prefill_tps = raw_tps
        out["reason"] = reason
        return out

    def current_level(self, now: float) -> str:
        """Level based on the *current* window (decays when no new blocks arrive)."""
        lvl = self._level_for(self.rolling_tps(now), self._sc())
        self.level = lvl
        return lvl

    def snapshot(self, now: float) -> dict:
        rolling = self.rolling_tps(now)
        return {
            "rolling_tps": None if rolling is None else round(rolling, 2),
            "last_block_tps": None if self.last_block_tps is None else round(self.last_block_tps, 2),
            "peak_block_tps": None if self.peak_block_tps is None else round(self.peak_block_tps, 2),
            "last_prefill_tps": None if self.last_prefill_tps is None else round(self.last_prefill_tps, 1),
            "level": self._level_for(rolling, self._sc()),
            "accepted_blocks": self.accepted,
            "ignored_blocks": dict(self.ignored),
            "baseline_blocks": dict(self.baseline_counts),
        }

    def reset(self) -> None:
        self.__init__(self.cfg)
