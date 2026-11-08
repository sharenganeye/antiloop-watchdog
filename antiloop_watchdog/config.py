"""Configuration: defaults, validation, copy-on-write updates, atomic persistence.

`ConfigManager.data` is replaced (never mutated) on every change, so detectors may
read it at any time without locking and always see a consistent snapshot.
"""
from __future__ import annotations

import copy
import json
import re
import threading
from pathlib import Path
from typing import Any, Optional

from .schema import MODES, ConfigError, ValidationError
from .util import atomic_write_text

VERSION = "1.1.0-beta.1"

DEFAULT_MESSAGE = (
    "You appear to be stuck in a repetitive loop. Stop repeating previous reasoning and actions. "
    "Review what has already been completed, identify the blocker, preserve completed work, and "
    "select a different concrete approach. Do not restart the same failed sequence."
)

DEFAULTS: dict = {
    "version": 1,
    "default_mode": "observe",
    "require_session_id": False,
    "api": {
        "host": "127.0.0.1",
        "port": 8791,
        "token_file": "state/token",
        "max_body_bytes": 1048576,
        "max_batch_events": 100,
    },
    "paths": {"log_dir": "logs", "state_dir": "state"},
    "speed": {
        "enabled": True,
        "warning_tps": 70.0,
        "critical_tps": 80.0,
        "min_block_tokens": 5,
        "min_block_duration_s": 1.0,
        "window_s": 60.0,
        "min_window_duration_s": 5.0,
        "min_window_tokens": 64,
        "min_window_blocks": 3,
        "prefill_floor_tps": 400.0,
        "baseline": {
            "thinking": [40, 55],
            "answering": [34, 50],
            "tool_call": [50, 71],
            "decode": [48, 49],
        },
    },
    "repetition": {
        "enabled": True,
        "window_tokens": 512,
        "min_sequence_tokens": 64,
        "threshold": 3,
        "check_interval_tokens": 16,
        "code_threshold_multiplier": 3,
        "structured_punct_ratio": 0.45,
        "ignore_kinds": ["tool_output", "file_content"],
        "sentence_repeat_threshold": 3,
        "sentence_min_words": 8,
        "ignore_patterns": [],
    },
    "tool_loop": {
        "enabled": True,
        "history": 64,
        "failed_repeat_threshold": 3,
        "identical_cycle_threshold": 4,
        "read_repeat_threshold": 4,
        "edit_repeat_threshold": 3,
        "search_repeat_threshold": 3,
        "cycle_max_period": 4,
        "cycle_repeats": 3,
        "poll_threshold_multiplier": 3,
        "volatile_arg_keys": ["timestamp", "ts", "time", "request_id", "id", "call_id",
                              "tool_call_id", "nonce", "session_id", "trace_id", "uuid"],
        "exempt_tools": [],
        "poll_tools": ["^(sleep|wait|poll|process_poll|check_status|get_status|process_status)$"],
        "tool_categories": {
            "write": ["(write|edit|patch|replace|create_file|append|apply|save|modify|delete|remove|move|rename)"],
            "search": ["(search|grep|find|query|lookup|glob)"],
            "read": ["(^|[_.\\-])(read|cat|view|open|get_file|head|tail|list_dir|ls|stat)([_.\\-]|$)"],
            "exec": ["(terminal|bash|shell|exec|run|cmd|powershell|command|python)"],
        },
    },
    "progress": {
        "enabled": True,
        "window_calls": 24,
        "min_calls": 8,
        "novelty_floor": 0.35,
        "error_streak_start": 3,
        "no_change_calls": 15,
        "alert_score": 0.6,
        "weights": {"novelty": 0.35, "errors": 0.25, "duplicate_results": 0.2, "no_state_change": 0.2},
    },
    "intervention": {
        "log_threshold": 0.5,
        "alert_threshold": 0.6,
        "auto_threshold": 0.85,
        "detection_ttl_s": 120.0,
        "realert_step": 2,
        "realert_min_interval_s": 5.0,
        "cooldown_s": 30.0,
        "failure_retry_s": 15.0,
        "escalate_after_redirects": 2,
        "escalation_grace_s": 10.0,
        "allow_interrupt": True,
        "recovery_quiet_s": 60.0,
        "recovery_min_events": 10,
        "directive_ttl_s": 600.0,
        "message": DEFAULT_MESSAGE,
        "interrupt_message": ("Watchdog requests that this session's current generation/turn be interrupted "
                              "because it is stuck in a repetitive loop that did not respond to redirection."),
    },
    "adapter": {
        "type": "queue",
        "redirect_url": "",
        "interrupt_url": "",
        "timeout_s": 3.0,
        "auth_env": "",
    },
    "sessions": {"max_sessions": 64, "idle_ttl_s": 3600.0, "max_events": 2000},
    "logging": {
        "json_file": "watchdog.jsonl",
        "text_file": "watchdog.log",
        "max_bytes": 5242880,
        "backups": 3,
        "capture_text": False,
        "capture_chars": 200,
    },
    "collector": {"llama_log_path": "", "llama_log_from_start": False, "poll_interval_s": 1.0},
}

_B, _I, _F, _S, _L, _D = "bool", "int", "float", "str", "list_str", "dict"

# path -> (kind, min, max, choices, mutable_via_api)
SPEC: dict = {
    "version": (_I, 1, 1, None, False),
    "default_mode": (_S, None, None, MODES, True),
    "require_session_id": (_B, None, None, None, True),
    "api.host": (_S, None, None, None, False),
    "api.port": (_I, 0, 65535, None, False),
    "api.token_file": (_S, None, None, None, False),
    "api.max_body_bytes": (_I, 1024, 16 * 1024 * 1024, None, False),
    "api.max_batch_events": (_I, 1, 1000, None, False),
    "paths.log_dir": (_S, None, None, None, False),
    "paths.state_dir": (_S, None, None, None, False),
    "speed.enabled": (_B, None, None, None, True),
    "speed.warning_tps": (_F, 1, 10000, None, True),
    "speed.critical_tps": (_F, 1, 10000, None, True),
    "speed.min_block_tokens": (_I, 1, 100000, None, True),
    "speed.min_block_duration_s": (_F, 0.01, 60, None, True),
    "speed.window_s": (_F, 5, 3600, None, True),
    "speed.min_window_duration_s": (_F, 0, 3600, None, True),
    "speed.min_window_tokens": (_I, 0, 1000000, None, True),
    "speed.min_window_blocks": (_I, 1, 1000, None, True),
    "speed.prefill_floor_tps": (_F, 100, 1000000, None, True),
    "speed.baseline": (_D, None, None, None, True),
    "repetition.enabled": (_B, None, None, None, True),
    "repetition.window_tokens": (_I, 128, 4096, None, True),
    "repetition.min_sequence_tokens": (_I, 16, 1024, None, True),
    "repetition.threshold": (_I, 2, 20, None, True),
    "repetition.check_interval_tokens": (_I, 1, 256, None, True),
    "repetition.code_threshold_multiplier": (_I, 1, 20, None, True),
    "repetition.structured_punct_ratio": (_F, 0.1, 1.0, None, True),
    "repetition.ignore_kinds": (_L, None, None, None, True),
    "repetition.sentence_repeat_threshold": (_I, 2, 20, None, True),
    "repetition.sentence_min_words": (_I, 3, 100, None, True),
    "repetition.ignore_patterns": (_L, None, None, None, True),
    "tool_loop.enabled": (_B, None, None, None, True),
    "tool_loop.history": (_I, 16, 512, None, True),
    "tool_loop.failed_repeat_threshold": (_I, 2, 50, None, True),
    "tool_loop.identical_cycle_threshold": (_I, 2, 50, None, True),
    "tool_loop.read_repeat_threshold": (_I, 2, 50, None, True),
    "tool_loop.edit_repeat_threshold": (_I, 2, 50, None, True),
    "tool_loop.search_repeat_threshold": (_I, 2, 50, None, True),
    "tool_loop.cycle_max_period": (_I, 2, 8, None, True),
    "tool_loop.cycle_repeats": (_I, 2, 10, None, True),
    "tool_loop.poll_threshold_multiplier": (_I, 1, 20, None, True),
    "tool_loop.volatile_arg_keys": (_L, None, None, None, True),
    "tool_loop.exempt_tools": (_L, None, None, None, True),
    "tool_loop.poll_tools": (_L, None, None, None, True),
    "tool_loop.tool_categories": (_D, None, None, None, True),
    "progress.enabled": (_B, None, None, None, True),
    "progress.window_calls": (_I, 8, 128, None, True),
    "progress.min_calls": (_I, 3, 128, None, True),
    "progress.novelty_floor": (_F, 0.05, 1.0, None, True),
    "progress.error_streak_start": (_I, 2, 50, None, True),
    "progress.no_change_calls": (_I, 3, 500, None, True),
    "progress.alert_score": (_F, 0.1, 1.0, None, True),
    "progress.weights": (_D, None, None, None, True),
    "intervention.log_threshold": (_F, 0.0, 1.0, None, True),
    "intervention.alert_threshold": (_F, 0.0, 1.0, None, True),
    "intervention.auto_threshold": (_F, 0.0, 1.0, None, True),
    "intervention.detection_ttl_s": (_F, 1, 3600, None, True),
    "intervention.realert_step": (_I, 1, 100, None, True),
    "intervention.realert_min_interval_s": (_F, 0, 3600, None, True),
    "intervention.cooldown_s": (_F, 0, 3600, None, True),
    "intervention.failure_retry_s": (_F, 0, 3600, None, True),
    "intervention.escalate_after_redirects": (_I, 1, 20, None, True),
    "intervention.escalation_grace_s": (_F, 0, 3600, None, True),
    "intervention.allow_interrupt": (_B, None, None, None, True),
    "intervention.recovery_quiet_s": (_F, 1, 86400, None, True),
    "intervention.recovery_min_events": (_I, 0, 100000, None, True),
    "intervention.directive_ttl_s": (_F, 5, 86400, None, True),
    "intervention.message": (_S, None, None, None, True),
    "intervention.interrupt_message": (_S, None, None, None, True),
    "adapter.type": (_S, None, None, ("queue", "webhook", "null"), False),
    "adapter.redirect_url": (_S, None, None, None, False),
    "adapter.interrupt_url": (_S, None, None, None, False),
    "adapter.timeout_s": (_F, 0.2, 30, None, False),
    "adapter.auth_env": (_S, None, None, None, False),
    "sessions.max_sessions": (_I, 2, 1024, None, True),
    "sessions.idle_ttl_s": (_F, 30, 604800, None, True),
    "sessions.max_events": (_I, 100, 100000, None, True),
    "logging.json_file": (_S, None, None, None, False),
    "logging.text_file": (_S, None, None, None, False),
    "logging.max_bytes": (_I, 65536, 1 << 30, None, False),
    "logging.backups": (_I, 0, 20, None, False),
    "logging.capture_text": (_B, None, None, None, True),
    "logging.capture_chars": (_I, 0, 2000, None, True),
    "collector.llama_log_path": (_S, None, None, None, False),
    "collector.llama_log_from_start": (_B, None, None, None, False),
    "collector.poll_interval_s": (_F, 0.5, 60, None, False),
}

_REGEX_LISTS = {"repetition.ignore_patterns", "tool_loop.exempt_tools", "tool_loop.poll_tools"}


def _check_value(path: str, value: Any) -> Any:
    kind, lo, hi, choices, _ = SPEC[path]
    err = lambda msg: ValidationError(f"{path}: {msg}", "invalid_config")  # noqa: E731
    if kind == _B:
        if not isinstance(value, bool):
            raise err("must be a boolean")
    elif kind == _I:
        if isinstance(value, bool) or not isinstance(value, int):
            raise err("must be an integer")
        if value < lo or value > hi:
            raise err(f"must be between {lo} and {hi}")
    elif kind == _F:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            raise err("must be a number")
        if value < lo or value > hi:
            raise err(f"must be between {lo} and {hi}")
        value = float(value)
    elif kind == _S:
        if not isinstance(value, str) or len(value) > 4000:
            raise err("must be a string (max 4000 chars)")
        if choices and value not in choices:
            raise err(f"must be one of {list(choices)}")
    elif kind == _L:
        if not isinstance(value, list) or len(value) > 200 or not all(isinstance(x, str) and len(x) <= 400 for x in value):
            raise err("must be a list of strings (max 200 entries, 400 chars each)")
        if path in _REGEX_LISTS:
            for pat in value:
                try:
                    re.compile(pat)
                except re.error as exc:
                    raise err(f"invalid regular expression {pat!r}: {exc}")
    elif kind == _D:
        if not isinstance(value, dict):
            raise err("must be an object")
        if path == "speed.baseline":
            for k, v in value.items():
                if not (isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v) and v[0] <= v[1]):
                    raise err("baseline entries must be [low, high] numbers")
        elif path == "tool_loop.tool_categories":
            for k, v in value.items():
                if k not in ("read", "write", "search", "exec") or not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise err("categories must be read|write|search|exec -> list of regex strings")
                for pat in v:
                    try:
                        re.compile(pat)
                    except re.error as exc:
                        raise err(f"invalid regular expression {pat!r}: {exc}")
        elif path == "progress.weights":
            keys = {"novelty", "errors", "duplicate_results", "no_state_change"}
            if set(value) != keys or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1 for v in value.values()):
                raise err(f"must contain exactly {sorted(keys)} with values in [0, 1]")
    return value


def _flatten(patch: dict, prefix: str = "") -> list:
    out = []
    for key, val in patch.items():
        if not isinstance(key, str):
            raise ValidationError("config keys must be strings", "invalid_config")
        path = f"{prefix}{key}"
        if path in SPEC:
            out.append((path, val))
        elif isinstance(val, dict) and any(p.startswith(path + ".") for p in SPEC):
            out.extend(_flatten(val, path + "."))
        else:
            raise ValidationError(f"unknown configuration key '{path}'", "unknown_config_key")
    return out


def _set(root: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    node = root
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value


def _get(root: dict, path: str) -> Any:
    node = root
    for p in path.split("."):
        node = node[p]
    return node


def _cross_validate(cfg: dict) -> None:
    if cfg["speed"]["critical_tps"] < cfg["speed"]["warning_tps"]:
        raise ValidationError("speed.critical_tps must be >= speed.warning_tps", "invalid_config")
    rep = cfg["repetition"]
    if rep["window_tokens"] < rep["threshold"] * rep["min_sequence_tokens"]:
        raise ValidationError(
            "repetition.window_tokens must be >= threshold * min_sequence_tokens, "
            "otherwise loops could never be detected", "invalid_config")
    iv = cfg["intervention"]
    if iv["alert_threshold"] > iv["auto_threshold"]:
        raise ValidationError("intervention.alert_threshold must be <= auto_threshold", "invalid_config")
    if iv["log_threshold"] > iv["alert_threshold"]:
        raise ValidationError("intervention.log_threshold must be <= alert_threshold", "invalid_config")
    if cfg["progress"]["min_calls"] > cfg["progress"]["window_calls"]:
        raise ValidationError("progress.min_calls must be <= progress.window_calls", "invalid_config")


class ConfigManager:
    def __init__(self, path, base_dir=None, overrides: Optional[dict] = None, create: bool = True):
        self.path = Path(path)
        self.base_dir = Path(base_dir) if base_dir else self.path.parent.parent
        self._lock = threading.Lock()
        self.warnings: list = []
        data = copy.deepcopy(DEFAULTS)
        file_exists = self.path.exists()
        if file_exists:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ConfigError(f"cannot read configuration file {self.path}: {exc}")
            if not isinstance(loaded, dict):
                raise ConfigError("configuration file must contain a JSON object")
            for path_, val in self._flatten_file(loaded):
                if path_ not in SPEC:
                    self.warnings.append(f"ignored unknown configuration key '{path_}'")
                    continue
                try:
                    _set(data, path_, _check_value(path_, val))
                except ValidationError as exc:
                    raise ConfigError(f"{self.path}: {exc.message}")
        if overrides:
            for path_, val in _flatten(overrides):
                _set(data, path_, _check_value(path_, val))
        try:
            _cross_validate(data)
        except ValidationError as exc:
            raise ConfigError(exc.message)
        self.data = data
        if create and not file_exists:
            self._persist(data)

    @staticmethod
    def _flatten_file(obj: dict, prefix: str = "") -> list:
        out = []
        for k, v in obj.items():
            p = f"{prefix}{k}"
            if p in SPEC or not isinstance(v, dict):
                out.append((p, v))
            else:
                out.extend(ConfigManager._flatten_file(v, p + "."))
        return out

    def get(self, path: str) -> Any:
        return _get(self.data, path)

    def resolve(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (self.base_dir / p)

    def public(self) -> dict:
        return copy.deepcopy(self.data)

    def mutable_keys(self) -> list:
        return sorted(k for k, spec in SPEC.items() if spec[4])

    def patch(self, patch: Any, persist: bool = True) -> dict:
        if not isinstance(patch, dict) or not patch:
            raise ValidationError("body must be a non-empty JSON object of configuration changes", "invalid_config")
        flat = _flatten(patch)
        with self._lock:
            new = copy.deepcopy(self.data)
            for path_, val in flat:
                if not SPEC[path_][4]:
                    raise ValidationError(
                        f"'{path_}' cannot be changed through the API; edit the configuration file and restart",
                        "config_key_immutable")
                _set(new, path_, _check_value(path_, val))
            _cross_validate(new)
            if persist:
                self._persist(new)
            self.data = new
        return copy.deepcopy(new)

    def _persist(self, data: dict) -> None:
        atomic_write_text(self.path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
