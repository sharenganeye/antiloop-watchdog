"""The watchdog engine: per-session state, event ingestion, detector fusion, event stream."""
from __future__ import annotations

import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Callable, List, Optional

from .adapters import HermesAdapter, QueueAdapter
from .config import VERSION, ConfigManager
from .intervention import InterventionController, public_directive
from .progress import ProgressDetector
from .repetition import RepetitionDetector
from .schema import (EVENT_SCHEMA, MODES, UNATTRIBUTED, ConflictError, Detection, NotFoundError,
                     ValidationError, validate_event, validate_session_id)
from .speed import SpeedMonitor
from .toolloop import ToolLoopDetector
from .util import CpuMeter, iso, rss_bytes


@dataclass
class Combined:
    confidence: float
    primary: Detection
    families: list
    speed_level: str
    family_bonus: float
    speed_bonus: float


class Session:
    def __init__(self, sid: str, identified: bool, kind: str, cfg: Callable[[], dict], now: float):
        self.id, self.identified, self.kind = sid, identified, kind     # kind: hermes | slot | unattributed
        self.cfg = cfg
        self.created = self.last_seen = now
        self.mode_override: Optional[str] = None
        self.speed = SpeedMonitor(cfg)
        self.rep = RepetitionDetector(cfg)
        self.tool = ToolLoopDetector(cfg)
        self.prog = ProgressDetector(cfg)
        self.events = 0
        self.tokens_observed = 0
        self.active: dict = {}                 # family -> (Detection, ts)
        self.last_emit: dict = {}              # detection key -> (repetitions, ts)
        self.directives: deque = deque(maxlen=32)
        self.in_intervention = False
        self.redirects_since_recovery = 0
        self.redirects_total = 0
        self.interrupts_total = 0
        self.last_redirect: Optional[dict] = None
        self.last_warn_ts = 0.0                # last queue-only (warn level) directive
        self.last_dispatch_ts = 0.0            # last automatic delivery attempt
        self.last_result_failed = False
        self.last_detection_ts = 0.0
        self.events_since_intervention = 0
        self.detections_total = 0
        self.wasted_tokens_est = 0

    @property
    def automatic_eligible(self) -> bool:
        return self.identified and self.kind == "hermes"

    def reset_detectors(self) -> None:
        self.rep.reset()
        self.tool.reset()
        self.prog.reset()
        self.active.clear()
        self.last_emit.clear()


class WatchdogEngine:
    def __init__(self, cm: ConfigManager, adapter: Optional[HermesAdapter] = None, event_log=None,
                 clock: Callable[[], float] = time.time, state=None):
        self.cm = cm
        self.adapter = adapter or QueueAdapter()
        self.log = event_log
        self.clock = clock
        self.state = state
        self.lock = threading.RLock()
        self.sessions: dict = {}
        self.counters: Counter = Counter()
        self.started = clock()
        self.cpu = CpuMeter()
        self._events: deque = deque(maxlen=cm.data["sessions"]["max_events"])
        self._next_id = 1
        self.last_detection_event: Optional[dict] = None
        self.ctrl = InterventionController(self)

    def cfgget(self) -> dict:
        return self.cm.data

    # ================================================================== events out

    def emit(self, etype: str, s: Optional[Session] = None, *, mode: Optional[str] = None,
             detection: Optional[Detection] = None, confidence: Optional[float] = None,
             intervention_action: Optional[str] = None, intervention_result: Optional[str] = None,
             directive_id: Optional[str] = None, details: Optional[dict] = None) -> dict:
        now = self.clock()
        tps = None
        if s is not None:
            r = s.speed.rolling_tps(now)
            tps = r if r is not None else s.speed.last_block_tps
        det_details = dict(details or {})
        if detection is not None and self.cm.data["logging"]["capture_text"] and detection.sample:
            det_details["sample"] = detection.sample[: self.cm.data["logging"]["capture_chars"]]
        evt = {
            "schema": EVENT_SCHEMA, "id": self._next_id, "ts": iso(now), "ts_epoch": round(now, 3),
            "session_id": s.id if s else None, "event_type": etype,
            "mode": mode or (self.ctrl.effective_mode(s) if s else None),
            "tps": None if tps is None else round(tps, 1),
            "detection_type": detection.type if detection else None,
            "confidence": confidence if confidence is not None else (detection.confidence if detection else None),
            "sequence_fingerprint": detection.sequence_fp if detection else None,
            "tool_fingerprint": detection.tool_fp if detection else None,
            "repetitions": detection.repetitions if detection else None,
            "intervention_action": intervention_action, "intervention_result": intervention_result,
            "directive_id": directive_id,
            "wasted_tokens_est": detection.wasted_tokens if detection else None,
            "details": det_details,
        }
        self._next_id += 1
        self._events.append(evt)
        self.counters[f"event_{etype}"] += 1
        if self.log is not None:
            try:
                self.log.write(evt)
            except Exception:
                self.counters["log_write_errors"] += 1
        return evt

    # ================================================================== sessions

    def _get_session(self, sid: str, identified: bool, kind: str, now: float) -> Session:
        s = self.sessions.get(sid)
        if s is not None:
            return s
        cap = self.cm.data["sessions"]["max_sessions"]
        while len(self.sessions) >= cap:
            victim = min(self.sessions.values(), key=lambda x: x.last_seen)
            self._drop(victim, "evicted")
        s = Session(sid, identified, kind, self.cfgget, now)
        if self.state is not None and kind == "hermes":
            s.mode_override = self.state.get_mode(sid)
        self.sessions[sid] = s
        self.emit("session_started", s, details={"kind": kind, "identified": identified})
        return s

    def _drop(self, s: Session, reason: str) -> None:
        self.sessions.pop(s.id, None)
        self.emit("session_ended", s, details={"reason": reason})

    def _require(self, sid: str) -> Session:
        s = self.sessions.get(sid)
        if s is None:
            raise NotFoundError(f"session '{sid}' is not known to the watchdog", "session_not_found")
        return s

    # ================================================================== ingestion

    def _limits(self) -> dict:
        return {"max_text_chars": 65536, "max_result_chars": 65536, "max_args_bytes": 65536}

    def ingest(self, raw: dict, _internal: Optional[tuple] = None) -> dict:
        try:
            ev = validate_event(raw, self._limits(), allow_reserved=_internal is not None)
        except ValidationError:
            self.counters["events_rejected"] += 1
            raise
        with self.lock:
            now = self.clock()
            sid = ev.get("session_id")
            if _internal is not None:
                sid, identified, kind = _internal
            elif sid is None:
                if self.cm.data["require_session_id"]:
                    self.counters["events_rejected"] += 1
                    raise ValidationError("session_id is required (require_session_id is enabled)", "session_id_required")
                sid, identified, kind = UNATTRIBUTED, False, "unattributed"
            else:
                identified, kind = True, "hermes"
            if ev["type"] == "session_end":
                s = self.sessions.get(sid)
                if s is not None:
                    self._drop(s, "session_end")
                self.counters["events_ingested"] += 1
                return {"session_id": sid, "accepted": True, "detections": [], "pending_directives": 0, "mode": None}
            s = self._get_session(sid, identified, kind, now)
            s.last_seen = now
            s.events += 1
            s.events_since_intervention += 1
            self.counters["events_ingested"] += 1
            self.counters[f"input_{ev['type']}"] += 1
            keep = bool(self.cm.data["logging"]["capture_text"])
            dets: List[Detection] = []
            t = ev["type"]
            if t == "text_delta":
                s.tokens_observed += ev["tokens"] if ev.get("tokens") else max(1, len(ev["text"]) // 4)
                dets += s.rep.feed(ev["text"], ev["kind"], ev["final"])
            elif t == "generation":
                res = s.speed.observe(now, ev["phase"], ev["tokens"], ev["duration_s"])
                if res["accepted"]:
                    s.tokens_observed += ev["tokens"]
                if res["level_changed"]:
                    self.emit("speed_indicator", s, details={
                        "level": res["level"], "rolling_tps": None if res["rolling_tps"] is None else round(res["rolling_tps"], 1),
                        "note": "indicator only; never an interruption reason by itself"})
            elif t == "tool_call":
                dets += s.tool.on_call(now, ev["tool"], ev["args"], ev["call_id"], ev["tokens"], ev["status"],
                                       ev["result_text"], ev["result_hash"], keep)
                dets += s.prog.evaluate(s.tool)
            elif t == "tool_result":
                dets += s.tool.on_result(now, ev["tool"], ev["args"], ev["call_id"], ev["status"],
                                         ev["result_text"], ev["result_hash"], ev["tokens"], keep)
                dets += s.prog.evaluate(s.tool)
            elif t == "state":
                if ev["changed"]:
                    s.tool.bump_epoch()
            emitted = self._process(s, dets, now)
            self._check_recovery(s, now)
            return {"session_id": sid, "accepted": True, "detections": emitted,
                    "pending_directives": self.ctrl.pending_count(s), "mode": self.ctrl.effective_mode(s)}

    def ingest_batch(self, events: list) -> dict:
        accepted, rejected, detections = 0, [], []
        for i, raw in enumerate(events):
            try:
                res = self.ingest(raw)
                accepted += 1
                detections.extend(res["detections"])
            except ValidationError as exc:
                rejected.append({"index": i, "code": exc.code, "error": exc.message})
        return {"accepted": accepted, "rejected": rejected, "detections": detections}

    def ingest_speed_sample(self, slot: str, phase: str, tokens: int, duration_s: float) -> None:
        """Used by the llama.cpp log tailer. Slot pseudo-sessions can never be intervened on."""
        self.ingest({"type": "generation", "phase": phase, "tokens": tokens, "duration_s": duration_s},
                    _internal=(f"slot-{slot}", False, "slot"))

    # ================================================================== fusion

    def _process(self, s: Session, dets: List[Detection], now: float) -> list:
        if not dets:
            return []
        iv = self.cm.data["intervention"]
        fresh: List[Detection] = []
        ttl = iv["detection_ttl_s"]
        for d in dets:
            cur = s.active.get(d.family)
            if cur is None or d.confidence >= cur[0].confidence or now - cur[1] > ttl:
                s.active[d.family] = (d, now)
            s.last_detection_ts = now
            prev = s.last_emit.get(d.key)
            if prev is not None:
                last_reps, last_ts = prev
                if not (d.repetitions >= last_reps + iv["realert_step"] and now - last_ts >= iv["realert_min_interval_s"]):
                    continue
            s.last_emit[d.key] = (d.repetitions, now)
            fresh.append(d)
        if not fresh:
            return []
        combined = self._combine(s, now)
        out = []
        for d in fresh:
            if d.confidence >= iv["log_threshold"] or (combined and combined.confidence >= iv["log_threshold"]):
                evt = self.emit("detection", s, detection=d, details={
                    "combined_confidence": combined.confidence if combined else d.confidence,
                    "families": combined.families if combined else [d.family],
                    "speed_level": combined.speed_level if combined else "normal",
                    "evidence": d.evidence})
                self.counters[f"detection_{d.type}"] += 1
                s.detections_total += 1
                if d.wasted_tokens:
                    s.wasted_tokens_est = max(s.wasted_tokens_est, d.wasted_tokens)
                self.last_detection_event = evt
                out.append(evt)
        if combined and out:
            self.ctrl.handle_detection(s, combined, now)
        return out

    def _combine(self, s: Session, now: float) -> Optional[Combined]:
        ttl = self.cm.data["intervention"]["detection_ttl_s"]
        s.active = {f: (d, t) for f, (d, t) in s.active.items() if now - t <= ttl}
        if not s.active:
            return None
        primary = max((d for d, _ in s.active.values()), key=lambda d: d.confidence)
        fams = set(s.active)
        bonus = 0.0
        if len(fams) >= 2:
            bonus = 0.05 if fams == {"tool", "progress"} else 0.10 * (len(fams) - 1)
        level = s.speed.current_level(now)
        sb = 0.0
        if primary.confidence >= 0.4:          # speed only ever corroborates an existing detection
            sb = 0.10 if level == "critical" else 0.05 if level == "warning" else 0.0
        conf = min(0.99, primary.confidence + bonus + sb)
        return Combined(round(conf, 3), primary, sorted(fams), level, round(bonus, 3), sb)

    def _check_recovery(self, s: Session, now: float) -> None:
        iv = self.cm.data["intervention"]
        if (s.in_intervention and now - s.last_detection_ts >= iv["recovery_quiet_s"]
                and s.events_since_intervention >= iv["recovery_min_events"]):
            s.in_intervention = False
            s.redirects_since_recovery = 0
            s.last_result_failed = False
            self.emit("recovered", s, details={"note": "no loop evidence since the last intervention"})

    # ================================================================== housekeeping

    def housekeeping(self) -> None:
        with self.lock:
            now = self.clock()
            self.ctrl.expire(now)
            ttl = self.cm.data["sessions"]["idle_ttl_s"]
            for s in list(self.sessions.values()):
                if now - s.last_seen > ttl:
                    self._drop(s, "idle_timeout")
                else:
                    self._check_recovery(s, now)

    # ================================================================== API surface

    def set_mode(self, sid: str, mode: str) -> dict:
        validate_session_id(sid, allow_reserved=True)
        if mode not in MODES and mode != "default":
            raise ValidationError(f"'mode' must be one of {list(MODES)} or 'default'")
        with self.lock:
            now = self.clock()
            reserved = sid == UNATTRIBUTED or sid.startswith("slot-")
            s = self.sessions.get(sid)
            if s is None:
                if reserved:
                    raise NotFoundError(f"session '{sid}' is not known to the watchdog", "session_not_found")
                s = self._get_session(sid, True, "hermes", now)
            if mode == "automatic" and not s.automatic_eligible:
                raise ConflictError("automatic mode is not allowed for sessions without an explicit session id "
                                    "(unattributed or inference-slot pseudo sessions)", "automatic_not_allowed")
            old = self.ctrl.requested_mode(s)
            s.mode_override = None if mode == "default" else mode
            if self.state is not None and s.kind == "hermes":
                self.state.set_mode(sid, s.mode_override)
            self.emit("mode_change", s, mode=self.ctrl.effective_mode(s),
                      details={"previous": old, "requested": mode})
            return self._summary(s, now)

    def reset_session(self, sid: str) -> dict:
        with self.lock:
            s = self._require(sid)
            s.speed.reset()
            s.reset_detectors()
            for d in s.directives:
                if d["status"] in ("pending", "delivered"):
                    d["status"] = "superseded"
            s.in_intervention = False
            s.redirects_since_recovery = 0
            s.last_redirect = None
            s.last_result_failed = False
            s.last_warn_ts = s.last_dispatch_ts = 0.0
            self.emit("session_reset", s)
            return self._summary(s, self.clock())

    def intervene(self, sid: str, action: str, message: Optional[str]) -> dict:
        with self.lock:
            s = self._require(sid)
            return self.ctrl.manual(s, action, message, self.clock())

    def get_directives(self, sid: str, status: Optional[str], peek: bool) -> list:
        with self.lock:
            s = self._require(sid)
            return self.ctrl.fetch(s, status, peek, self.clock())

    def ack_directive(self, sid: str, did: str, result: str) -> dict:
        with self.lock:
            return self.ctrl.ack(self._require(sid), did, result, self.clock())

    def events_since(self, since_id: int = 0, session_id: Optional[str] = None, event_type: Optional[str] = None,
                     limit: int = 100) -> dict:
        with self.lock:
            evs = [e for e in self._events if e["id"] > since_id
                   and (session_id is None or e["session_id"] == session_id)
                   and (event_type is None or e["event_type"] == event_type)]
            if since_id <= 0 and len(evs) > limit:
                evs = evs[-limit:]
            else:
                evs = evs[:limit]
            return {"events": evs, "next_since_id": evs[-1]["id"] if evs else since_id}

    def _summary(self, s: Session, now: float) -> dict:
        return {
            "session_id": s.id, "kind": s.kind, "identified": s.identified,
            "automatic_eligible": s.automatic_eligible,
            "mode": self.ctrl.effective_mode(s), "requested_mode": self.ctrl.requested_mode(s),
            "mode_override": s.mode_override,
            "created_at": iso(s.created), "last_seen_at": iso(s.last_seen),
            "events": s.events, "tokens_observed": s.tokens_observed,
            "speed": s.speed.snapshot(now),
            "active_detections": [
                {"family": f, "detection_type": d.type, "confidence": d.confidence, "repetitions": d.repetitions,
                 "age_s": round(now - t, 1)} for f, (d, t) in s.active.items()
                if now - t <= self.cm.data["intervention"]["detection_ttl_s"]],
            "detections_total": s.detections_total, "wasted_tokens_est": s.wasted_tokens_est or None,
            "pending_directives": self.ctrl.pending_count(s),
            "in_intervention": s.in_intervention, "redirects_total": s.redirects_total,
            "interrupts_total": s.interrupts_total, "progress": dict(s.prog.last_signals) or None,
        }

    def list_sessions(self) -> list:
        with self.lock:
            now = self.clock()
            return [self._summary(s, now) for s in self.sessions.values()]

    def session_detail(self, sid: str) -> dict:
        with self.lock:
            s = self._require(sid)
            out = self._summary(s, self.clock())
            out["directives"] = [public_directive(d) for d in s.directives]
            return out

    def resource(self) -> dict:
        rss = rss_bytes()
        return {"rss_mb": None if rss is None else round(rss / 1048576, 1),
                "cpu_percent_since_start": self.cpu.since_start()}

    def status(self) -> dict:
        with self.lock:
            now = self.clock()
            by_mode = Counter(self.ctrl.effective_mode(s) for s in self.sessions.values())
            return {
                "service": "antiloop-watchdog", "version": VERSION, "uptime_s": round(now - self.started, 1),
                "default_mode": self.cm.data["default_mode"],
                "adapter": self.adapter.info(),
                "sessions": {"active": len(self.sessions), "by_mode": dict(by_mode)},
                "counters": {k: v for k, v in sorted(self.counters.items())},
                "last_detection": self.last_detection_event,
                "resource": self.resource(),
                "config_warnings": list(self.cm.warnings),
            }

    def metrics(self) -> dict:
        with self.lock:
            now = self.clock()
            return {
                "uptime_s": round(now - self.started, 1),
                "counters": dict(self.counters),
                "sessions": [{"session_id": s.id, "mode": self.ctrl.effective_mode(s),
                              "rolling_tps": s.speed.snapshot(now)["rolling_tps"],
                              "speed_level": s.speed.snapshot(now)["level"],
                              "tokens_observed": s.tokens_observed, "detections_total": s.detections_total,
                              "wasted_tokens_est": s.wasted_tokens_est or 0} for s in self.sessions.values()],
                "resource": {**self.resource(), "cpu_percent_since_last_scrape": self.cpu.since_last()},
            }

    def prometheus(self) -> str:
        m = self.metrics()
        lines = ["# TYPE antiloop_sessions gauge", f"antiloop_sessions {len(m['sessions'])}",
                 "# TYPE antiloop_uptime_seconds gauge", f"antiloop_uptime_seconds {m['uptime_s']}"]
        if m["resource"]["rss_mb"] is not None:
            lines += ["# TYPE antiloop_rss_megabytes gauge", f"antiloop_rss_megabytes {m['resource']['rss_mb']}"]
        lines += ["# TYPE antiloop_cpu_percent gauge", f"antiloop_cpu_percent {m['resource']['cpu_percent_since_start']}",
                  "# TYPE antiloop_counter counter"]
        for k, v in sorted(m["counters"].items()):
            lines.append(f'antiloop_counter{{name="{k}"}} {v}')
        for s in m["sessions"]:
            if s["rolling_tps"] is not None:
                lines.append(f'antiloop_rolling_tps{{session="{s["session_id"]}"}} {s["rolling_tps"]}')
        return "\n".join(lines) + "\n"

    def shutdown(self) -> None:
        self.ctrl.stop()
