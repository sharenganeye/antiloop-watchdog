"""Intervention engine.

Modes (per session, falling back to `default_mode`):
  observe    detect + log evidence only. Nothing is ever created or sent.
  warn       detect + create a *directive* (corrective message) exposed through the API.
             Nothing is pushed to Hermes and nothing is interrupted.
  automatic  like warn, plus delivery through the configured adapter once the combined
             confidence reaches `auto_threshold`. A redirect is preferred; an interrupt is only
             used as escalation after `escalate_after_redirects` delivered redirects failed to
             stop the loop. Automatic mode is impossible for sessions without an explicit
             session id (the request is downgraded to warn).

Every adapter call is scoped to exactly the session that produced the evidence.
"""
from __future__ import annotations

import queue
import re
import threading
import uuid
from typing import Optional

from .adapters import AdapterResult
from .schema import DIRECTIVE_SCHEMA, ConflictError, NotFoundError, ValidationError
from .util import iso

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ACK_RESULTS = ("applied", "ignored", "failed")


def public_directive(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.endswith("_epoch")}


class InterventionController:
    def __init__(self, engine):
        self.e = engine
        self._q: Optional[queue.Queue] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ modes

    def requested_mode(self, s) -> str:
        return s.mode_override or self.e.cm.data["default_mode"]

    def effective_mode(self, s) -> str:
        m = self.requested_mode(s)
        if m == "automatic" and not s.automatic_eligible:
            return "warn"
        return m

    # ------------------------------------------------------------------ automatic / warn path

    def handle_detection(self, s, combined, now: float) -> None:
        iv = self.e.cm.data["intervention"]
        mode = self.effective_mode(s)
        if mode == "observe" or combined.confidence < iv["alert_threshold"]:
            return
        auto = mode == "automatic" and combined.confidence >= iv["auto_threshold"]
        if auto:     # an earlier queue-only warning must not delay a real delivery
            gate_ts, gate = s.last_dispatch_ts, (iv["failure_retry_s"] if s.last_result_failed else iv["cooldown_s"])
        else:
            gate_ts, gate = max(s.last_warn_ts, s.last_dispatch_ts), iv["cooldown_s"]
        if gate_ts and now - gate_ts < gate:
            return
        action = "redirect"
        if auto and self._should_escalate(s, iv, now):
            action = "interrupt"
        p = combined.primary
        reason = {"detection_type": p.type, "confidence": combined.confidence, "families": combined.families,
                  "repetitions": p.repetitions, "sequence_fingerprint": p.sequence_fp,
                  "tool_fingerprint": p.tool_fp, "speed_level": combined.speed_level}
        d = self._create(s, action, "automatic" if auto else "warn", reason, now)
        if auto:
            s.last_dispatch_ts = now
        else:
            s.last_warn_ts = now
        note = None
        if self.requested_mode(s) == "automatic" and not s.automatic_eligible:
            note = "automatic mode unavailable: session has no explicit session id"
        elif mode == "automatic" and not auto:
            note = "confidence below auto_threshold: directive queued only"
        if auto:
            self._dispatch(s, d, now, note)
        else:
            self.e.emit("intervention", s, mode=mode, confidence=combined.confidence,
                        intervention_action=action, intervention_result="queued_for_pull", directive_id=d["id"],
                        detection=p, details={"directive_source": "warn", **({"note": note} if note else {})})
            self.e.counters["interventions_queued"] += 1

    def _should_escalate(self, s, iv: dict, now: float) -> bool:
        ad = self.e.adapter
        last = s.last_redirect
        return bool(
            iv["allow_interrupt"] and ad.supports_interrupt and s.in_intervention
            and s.redirects_since_recovery >= iv["escalate_after_redirects"]
            and last and last["status"] in ("delivered", "acked") and last.get("ack_result") != "failed"
            and now - (last.get("delivered_epoch") or now) >= iv["escalation_grace_s"])

    # ------------------------------------------------------------------ directives

    def _create(self, s, action: str, source: str, reason: dict, now: float, message: Optional[str] = None) -> dict:
        iv = self.e.cm.data["intervention"]
        text = message if message else (iv["message"] if action == "redirect" else iv["interrupt_message"])
        d = {"schema": DIRECTIVE_SCHEMA, "id": "d-" + uuid.uuid4().hex[:12], "session_id": s.id, "action": action,
             "message": text, "source": source, "status": "pending", "delivery": "queued",
             "created_at": iso(now), "expires_at": iso(now + iv["directive_ttl_s"]),
             "delivered_at": None, "acked_at": None, "ack_result": None, "reason": reason,
             "created_epoch": now, "expires_epoch": now + iv["directive_ttl_s"], "delivered_epoch": None}
        for old in s.directives:
            if old["status"] == "pending" and old["action"] == action:
                old["status"] = "superseded"
        s.directives.append(d)
        return d

    def _dispatch(self, s, d: dict, now: float, note: Optional[str] = None) -> None:
        ad = self.e.adapter
        if d["session_id"] != s.id:                     # defence in depth: never cross sessions
            raise RuntimeError("directive/session mismatch")
        supported = ad.supports_redirect if d["action"] == "redirect" else ad.supports_interrupt
        if not supported:
            res = AdapterResult(False, "adapter_unavailable", f"adapter '{ad.name}' cannot {d['action']}")
            self._finish(s, d, res, now, "intervention", note)
            return
        if ad.blocking:
            self._enqueue(s.id, d)
            self.e.emit("intervention", s, mode="automatic", intervention_action=d["action"],
                        intervention_result="dispatch_pending", directive_id=d["id"],
                        confidence=d["reason"].get("confidence"),
                        details={"directive_source": d["source"], **({"note": note} if note else {})})
            return
        fn = ad.redirect if d["action"] == "redirect" else ad.interrupt
        try:
            res = fn(s.id, dict(d))
        except Exception as exc:                        # an adapter must never break the engine
            res = AdapterResult(False, "adapter_error", f"{type(exc).__name__}: {str(exc)[:150]}")
        self._finish(s, d, res, now, "intervention", note)

    def _finish(self, s, d: dict, res: AdapterResult, now: float, event_type: str, note: Optional[str] = None) -> None:
        ad = self.e.adapter
        if res.ok:
            d["delivery"] = "pushed" if ad.push else "queued"
            if ad.push:
                d["status"] = "delivered"
                d["delivered_at"], d["delivered_epoch"] = iso(now), now
            s.last_result_failed = False
            s.in_intervention = True
            s.events_since_intervention = 0
            if d["action"] == "redirect":
                s.redirects_since_recovery += 1
                s.redirects_total += 1
                s.last_redirect = d
            else:
                s.interrupts_total += 1
            s.reset_detectors()
            self.e.counters["interventions_ok"] += 1
        else:
            d["delivery"] = "unavailable" if res.status.startswith("adapter_") else "push_failed"
            s.last_result_failed = True
            self.e.counters["adapter_failures"] += 1
        result = res.status if res.ok else f"{res.status}: {res.detail}".strip(": ")
        self.e.emit(event_type, s, mode="automatic", intervention_action=d["action"],
                    intervention_result=result, directive_id=d["id"], confidence=d["reason"].get("confidence"),
                    details={"directive_source": d["source"], "delivery": d["delivery"],
                             "directive_status": d["status"], **({"note": note} if note else {})})

    # blocking adapters run on one background thread so a slow Hermes never stalls ingestion
    def _enqueue(self, sid: str, d: dict) -> None:
        if self._q is None:
            self._q = queue.Queue(maxsize=64)
            self._thread = threading.Thread(target=self._worker, name="antiloop-dispatch", daemon=True)
            self._thread.start()
        try:
            self._q.put_nowait((sid, d))
        except queue.Full:
            d["delivery"] = "push_failed"

    def _worker(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                return
            sid, d = job
            ad = self.e.adapter
            with self.e.lock:
                snap = dict(d)
            fn = ad.redirect if snap["action"] == "redirect" else ad.interrupt
            try:
                res = fn(sid, snap)
            except Exception as exc:
                res = AdapterResult(False, "adapter_error", f"{type(exc).__name__}: {str(exc)[:150]}")
            with self.e.lock:
                s = self.e.sessions.get(sid)
                if s is not None:
                    self._finish(s, d, res, self.e.clock(), "intervention_result")

    def stop(self) -> None:
        if self._q is not None:
            try:
                self._q.put_nowait(None)
            except queue.Full:
                pass

    # ------------------------------------------------------------------ manual / API

    def manual(self, s, action: str, message: Optional[str], now: float) -> dict:
        if action not in ("redirect", "interrupt"):
            raise ValidationError("'action' must be 'redirect' or 'interrupt'")
        if message is not None:
            if not isinstance(message, str) or not message.strip() or len(message) > 2000:
                raise ValidationError("'message' must be a non-empty string up to 2000 characters")
            message = _CTRL.sub("", message).strip()
        mode = self.effective_mode(s)
        if mode == "observe":
            raise ConflictError("session is in observe mode; switch it to warn or automatic first", "mode_observe")
        if action == "interrupt":
            if mode != "automatic":
                raise ConflictError("interrupt requires automatic mode on an identified session", "interrupt_not_allowed")
            if not self.e.cm.data["intervention"]["allow_interrupt"]:
                raise ConflictError("interrupts are disabled by configuration", "interrupt_not_allowed")
        d = self._create(s, action, "manual", {"requested_by": "api"}, now, message)
        if mode == "automatic":
            s.last_dispatch_ts = now
            self._dispatch(s, d, now)
        else:
            s.last_warn_ts = now
            self.e.emit("intervention", s, mode=mode, intervention_action=action,
                        intervention_result="queued_for_pull", directive_id=d["id"],
                        details={"directive_source": "manual"})
            self.e.counters["interventions_queued"] += 1
        return public_directive(d)

    def fetch(self, s, status: Optional[str], peek: bool, now: float) -> list:
        out = []
        for d in s.directives:
            if status and d["status"] != status:
                continue
            if not status and d["status"] not in ("pending", "delivered"):
                continue
            if d["status"] == "pending" and not peek:
                d["status"] = "delivered"
                d["delivered_at"], d["delivered_epoch"] = iso(now), now
            out.append(public_directive(d))
        return out

    def ack(self, s, directive_id: str, result: str, now: float) -> dict:
        if result not in ACK_RESULTS:
            raise ValidationError(f"'result' must be one of {list(ACK_RESULTS)}")
        for d in s.directives:
            if d["id"] == directive_id:
                if d["delivered_epoch"] is None:
                    d["delivered_at"], d["delivered_epoch"] = iso(now), now
                d["status"], d["ack_result"], d["acked_at"] = "acked", result, iso(now)
                if result == "failed":
                    s.last_result_failed = True
                self.e.emit("directive_acked", s, intervention_action=d["action"], intervention_result=result,
                            directive_id=directive_id)
                return public_directive(d)
        raise NotFoundError(f"directive '{directive_id}' not found for this session", "directive_not_found")

    def expire(self, now: float) -> None:
        for s in self.e.sessions.values():
            for d in s.directives:
                if d["status"] in ("pending", "delivered") and now >= d["expires_epoch"]:
                    d["status"] = "expired"

    def pending_count(self, s) -> int:
        return sum(1 for d in s.directives if d["status"] in ("pending", "delivered"))
