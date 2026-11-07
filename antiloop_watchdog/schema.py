"""Shared constants, error types, the Detection record and input-event validation."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

MODES = ("observe", "warn", "automatic")
INPUT_TYPES = ("text_delta", "generation", "tool_call", "tool_result", "state", "session_end")
TEXT_KINDS = ("reasoning", "answer", "code", "tool_output", "file_content")
PHASES = ("thinking", "answering", "tool_call", "decode", "prefill")
STATUSES = ("ok", "error", "unknown")
ACTIONS = ("redirect", "interrupt")

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$")
UNATTRIBUTED = "__unattributed__"
RESERVED_PREFIXES = ("slot-", "__")
EVENT_SCHEMA = "antiloop.event.v1"
DIRECTIVE_SCHEMA = "antiloop.directive.v1"


class WatchdogError(Exception):
    code = "error"
    status = 500

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class ValidationError(WatchdogError):
    code = "invalid_request"
    status = 400


class NotFoundError(WatchdogError):
    code = "not_found"
    status = 404


class ConflictError(WatchdogError):
    code = "conflict"
    status = 409


class ConfigError(WatchdogError):
    code = "config_error"
    status = 400


@dataclass
class Detection:
    family: str                      # text | tool | progress
    type: str
    confidence: float
    repetitions: int = 0
    sequence_fp: Optional[str] = None
    tool_fp: Optional[str] = None
    wasted_tokens: Optional[int] = None
    evidence: dict = field(default_factory=dict)
    sample: Optional[str] = None     # only ever emitted when logging.capture_text is true
    group: Optional[str] = None      # when set, de-duplication ignores the individual fingerprint

    @property
    def key(self) -> tuple:
        return (self.type, self.group if self.group is not None else (self.sequence_fp or self.tool_fp or ""))


# ---------------------------------------------------------------- validation helpers

def _opt_int(raw: dict, key: str, lo: int, hi: int) -> Optional[int]:
    v = raw.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < lo or v > hi:
        raise ValidationError(f"'{key}' must be an integer in [{lo}, {hi}]")
    return v


def _num(raw: dict, key: str, lo: float, hi: float, required: bool = True) -> Optional[float]:
    v = raw.get(key)
    if v is None:
        if required:
            raise ValidationError(f"'{key}' is required")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < lo or v > hi:
        raise ValidationError(f"'{key}' must be a number in [{lo}, {hi}]")
    return float(v)


def _opt_str(raw: dict, key: str, max_len: int) -> Optional[str]:
    v = raw.get(key)
    if v is None:
        return None
    if not isinstance(v, str) or len(v) > max_len:
        raise ValidationError(f"'{key}' must be a string up to {max_len} characters")
    return v


def validate_session_id(sid: Any, allow_reserved: bool = False) -> str:
    if allow_reserved and sid == UNATTRIBUTED:
        return sid
    if not isinstance(sid, str) or not SESSION_ID_RE.match(sid):
        raise ValidationError(
            "session_id must match ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", "invalid_session_id")
    if not allow_reserved and sid.startswith(RESERVED_PREFIXES):
        raise ValidationError(
            f"session_id may not start with {RESERVED_PREFIXES}", "reserved_session_id")
    return sid


def validate_event(raw: Any, limits: dict, allow_reserved: bool = False) -> dict:
    """Validate and normalise one input event. Raises ValidationError."""
    if not isinstance(raw, dict):
        raise ValidationError("event must be a JSON object")
    etype = raw.get("type")
    if etype not in INPUT_TYPES:
        raise ValidationError(f"'type' must be one of {list(INPUT_TYPES)}", "invalid_event_type")
    sid = raw.get("session_id")
    if sid is not None:
        validate_session_id(sid, allow_reserved)
    ev: dict = {"type": etype, "session_id": sid}

    if etype == "text_delta":
        text = raw.get("text", "")
        if not isinstance(text, str) or len(text) > limits["max_text_chars"]:
            raise ValidationError(f"'text' must be a string up to {limits['max_text_chars']} characters")
        kind = raw.get("kind", "answer")
        if kind not in TEXT_KINDS:
            raise ValidationError(f"'kind' must be one of {list(TEXT_KINDS)}")
        final = raw.get("final", False)
        if not isinstance(final, bool):
            raise ValidationError("'final' must be a boolean")
        ev.update(text=text, kind=kind, final=final, tokens=_opt_int(raw, "tokens", 0, 1_000_000))

    elif etype == "generation":
        phase = raw.get("phase", "decode")
        if phase not in PHASES:
            raise ValidationError(f"'phase' must be one of {list(PHASES)}")
        tokens = _opt_int(raw, "tokens", 0, 10_000_000)
        if tokens is None:
            raise ValidationError("'tokens' is required")
        ev.update(phase=phase, tokens=tokens, duration_s=_num(raw, "duration_s", 1e-6, 3600.0))

    elif etype in ("tool_call", "tool_result"):
        tool = raw.get("tool")
        if tool is None and etype == "tool_call":
            raise ValidationError("'tool' is required for tool_call")
        tool = _opt_str(raw, "tool", 128)
        if tool is not None and not tool.strip():
            raise ValidationError("'tool' must not be empty")
        args = raw.get("args")
        if args is not None:
            if not isinstance(args, (dict, list, str, int, float, bool)):
                raise ValidationError("'args' must be JSON data")
            try:
                size = len(json.dumps(args))
            except (TypeError, ValueError):
                raise ValidationError("'args' must be JSON serialisable")
            if size > limits["max_args_bytes"]:
                raise ValidationError(f"'args' exceeds {limits['max_args_bytes']} bytes")
        status = raw.get("status", "unknown")
        if status not in STATUSES:
            raise ValidationError(f"'status' must be one of {list(STATUSES)}")
        result_text = _opt_str(raw, "result_text", limits["max_result_chars"])
        result_hash = _opt_str(raw, "result_hash", 64)
        if result_hash is not None and not re.fullmatch(r"[A-Za-z0-9]{4,64}", result_hash):
            raise ValidationError("'result_hash' must be 4-64 alphanumeric characters")
        ev.update(tool=tool, args=args, call_id=_opt_str(raw, "call_id", 128), status=status,
                  result_text=result_text, result_hash=result_hash,
                  tokens=_opt_int(raw, "tokens", 0, 10_000_000))

    elif etype == "state":
        changed = raw.get("changed", True)
        if not isinstance(changed, bool):
            raise ValidationError("'changed' must be a boolean")
        ev.update(changed=changed)

    return ev
