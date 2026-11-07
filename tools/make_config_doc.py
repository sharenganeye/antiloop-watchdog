#!/usr/bin/env python3
"""Generates config/watchdog.config.json (defaults) and CONFIGURATION.md from the validation spec in config.py."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from antiloop_watchdog.config import DEFAULTS, SPEC, _get  # noqa: E402

(ROOT / "config").mkdir(exist_ok=True)
(ROOT / "config" / "watchdog.config.json").write_text(json.dumps(DEFAULTS, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

NOTES = {
    "version": "Config schema version (must be 1).",
    "api.max_body_bytes": "Largest accepted request body (413 above).",
    "api.max_batch_events": "Largest `{\"events\": [...]}` batch.",
    "paths.log_dir": "Directory for the JSON and human-readable logs.",
    "paths.state_dir": "Directory for the token, pid file and persisted session modes.",
    "speed.enabled": "Disable speed monitoring entirely.",
    "speed.window_s": "Rolling window for the token-weighted speed.",
    "speed.min_window_duration_s": "Rolling TPS is only reported once accepted blocks cover at least this many seconds.",
    "speed.min_window_tokens": "... and at least this many tokens.",
    "repetition.enabled": "Disable text repetition detection.",
    "repetition.sentence_min_words": "Sentences shorter than this are never counted as repeats.",
    "tool_loop.enabled": "Disable tool-call loop detection.",
    "progress.enabled": "Disable stagnation scoring.",
    "progress.window_calls": "Number of most recent tool calls the stagnation score looks at.",
    "progress.min_calls": "No stagnation score before this many calls.",
    "progress.novelty_floor": "Fraction of distinct (action, result) pairs below which novelty counts against progress.",
    "progress.error_streak_start": "Consecutive failures from which the error signal starts to grow.",
    "progress.no_change_calls": "Calls without any state change from which that signal starts to grow.",
    "intervention.interrupt_message": "Text of interrupt directives.",
    "adapter.timeout_s": "Webhook request timeout (requests run on a background thread, ingestion never waits).",
    "sessions.max_sessions": "Tracked sessions; the least recently seen one is evicted beyond this.",
    "sessions.idle_ttl_s": "Sessions without events for this long are dropped.",
    "sessions.max_events": "Size of the in-memory event ring served by `GET /events` (the log files keep everything).",
    "logging.json_file": "JSON-lines event log (file name inside `paths.log_dir`).",
    "logging.text_file": "Human-readable log.",
    "logging.max_bytes": "Rotate each log file at this size ...",
    "logging.backups": "... keeping this many rotated files.",
    "collector.poll_interval_s": "Poll interval of the log tailer (the only polling in the service).",
    "default_mode": "Mode for sessions without an override. `observe` is the safe default.",
    "require_session_id": "Reject events that carry no `session_id` instead of filing them under `__unattributed__`.",
    "api.host": "Must be a loopback address. Anything else makes the service refuse to start.",
    "api.port": "0 = pick a free port (tests). Default 8791 (llama.cpp uses 8080).",
    "api.token_file": "Bearer token; generated (256-bit) on first start with mode 0600. `WATCHDOG_TOKEN` env var overrides it.",
    "speed.warning_tps": "Indicator level only; never an interruption reason by itself.",
    "speed.critical_tps": "Indicator level only; adds +0.10 to an *existing* detection's combined confidence (warning: +0.05).",
    "speed.min_block_tokens": "Blocks smaller than this are ignored (tiny-block artefacts).",
    "speed.min_block_duration_s": "Blocks shorter than this are ignored.",
    "speed.min_window_blocks": "Rolling TPS is only reported when the window holds at least this many accepted blocks.",
    "speed.prefill_floor_tps": "Blocks faster than this are treated as mislabelled prompt prefill and ignored (measured prefill 1500-1800, decode <= 71).",
    "speed.baseline": "Measured normal ranges per phase; only used to label blocks below/within/above in metrics.",
    "repetition.window_tokens": "Rolling buffer size. Must be >= threshold x min_sequence_tokens. Longest detectable loop period ~ (window - min_sequence) / (threshold - 1).",
    "repetition.min_sequence_tokens": "Length of the sequence that must repeat (approximate tokens: words + punctuation marks).",
    "repetition.threshold": "Occurrences of the sequence that make a loop.",
    "repetition.check_interval_tokens": "The n-gram check runs once per this many new tokens (CPU knob).",
    "repetition.code_threshold_multiplier": "Fenced code, `kind=code` and punctuation-heavy (JSON-like) windows need threshold x this many repetitions.",
    "repetition.structured_punct_ratio": "Fraction of punctuation tokens at which a window counts as structured.",
    "repetition.ignore_kinds": "Event `kind`s that are never inspected for text repetition.",
    "repetition.sentence_repeat_threshold": "Same normalised sentence this many times -> low-confidence (0.5-0.8) `sentence_repeat`.",
    "repetition.ignore_patterns": "Regexes; a repeated sequence matching any of them is ignored (legitimately repeated instructions).",
    "tool_loop.history": "Tool records kept per session (no raw arguments or results are stored).",
    "tool_loop.failed_repeat_threshold": "Same call failing this many times in one state epoch.",
    "tool_loop.identical_cycle_threshold": "Same exec/other call with the same result this many times in one epoch.",
    "tool_loop.read_repeat_threshold": "Same read with unchanged result this many times in one epoch.",
    "tool_loop.edit_repeat_threshold": "Identical edit this many times.",
    "tool_loop.search_repeat_threshold": "Same search with unchanged result this many times.",
    "tool_loop.cycle_max_period": "Longest A-B-C... action cycle that is looked for.",
    "tool_loop.cycle_repeats": "Repetitions of a cycle that make a loop.",
    "tool_loop.poll_threshold_multiplier": "Thresholds are multiplied by this for tools matching `poll_tools`.",
    "tool_loop.volatile_arg_keys": "Argument keys dropped before fingerprinting (ids, timestamps).",
    "tool_loop.exempt_tools": "Regexes (case-insensitive) of tool names that are never evaluated.",
    "tool_loop.poll_tools": "Regexes of tools that legitimately repeat (sleep, status polling).",
    "tool_loop.tool_categories": "Regexes mapping tool names to read / write / search / exec. First match in the order write, search, read, exec wins.",
    "progress.weights": "Signal weights: novelty, errors, duplicate_results, no_state_change.",
    "progress.alert_score": "Weighted stagnation score at which a `stagnation` detection is produced (confidence = min(0.9, score)).",
    "intervention.log_threshold": "Detections below this confidence are kept for corroboration but not logged.",
    "intervention.alert_threshold": "Minimum combined confidence for a warn-level directive.",
    "intervention.auto_threshold": "Minimum combined confidence for automatic delivery (automatic mode only).",
    "intervention.detection_ttl_s": "How long a detection counts as active evidence for corroboration.",
    "intervention.realert_step": "A repeating detection is re-reported after this many additional repetitions ...",
    "intervention.realert_min_interval_s": "... and at least this many seconds.",
    "intervention.cooldown_s": "Minimum time between directives (warn) / deliveries (automatic) per session.",
    "intervention.failure_retry_s": "Shorter retry gap after a failed delivery.",
    "intervention.escalate_after_redirects": "Delivered redirects that failed to stop the loop before an interrupt is requested.",
    "intervention.escalation_grace_s": "Seconds a redirect must have been delivered before escalation is considered.",
    "intervention.allow_interrupt": "Set false to forbid interrupts entirely (redirect only).",
    "intervention.recovery_quiet_s": "Quiet period (no loop evidence) after which a session counts as recovered.",
    "intervention.recovery_min_events": "... and minimum number of events since the intervention.",
    "intervention.directive_ttl_s": "Unacknowledged directives expire after this long.",
    "intervention.message": "Corrective text for redirect directives (default: the required text).",
    "adapter.type": "`queue` (pull, default), `webhook` (push to a loopback URL you implement), `null` (no delivery).",
    "adapter.redirect_url": "Webhook only. http:// on a loopback host. File-only (not changeable via API).",
    "adapter.interrupt_url": "Webhook only. Same rules. Empty = interrupts cannot be pushed.",
    "adapter.auth_env": "Name of an environment variable whose value is sent as `Authorization: Bearer ...` to the webhook.",
    "logging.capture_text": "Opt-in diagnostic capture: adds a truncated sample of the *repeated* sequence to events. Default off.",
    "logging.capture_chars": "Maximum characters of that sample.",
    "collector.llama_log_path": "Opt-in: tail this llama.cpp/Strata log for speed samples. Empty = disabled. Verify the format first with `tools/check_llama_log.py`.",
    "collector.llama_log_from_start": "Replay the file from its beginning instead of attaching at the end.",
}


def fmt(v):
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) < 70 else s[:67] + "..."


rows = []
for key, (kind, lo, hi, choices, api) in SPEC.items():
    rng = ""
    if choices:
        rng = " / ".join(choices)
    elif lo is not None:
        rng = f"{lo} .. {hi}"
    rows.append(f"| `{key}` | {kind} | `{fmt(_get(DEFAULTS, key))}` | {rng} | {'yes' if api else 'file only'} | {NOTES.get(key, '')} |")

doc = f"""# CONFIGURATION

Configuration lives in `config/watchdog.config.json` (created with these defaults on first start if missing).
It is plain JSON; unknown keys are ignored with a warning, invalid values make the service refuse to start
(`python -m antiloop_watchdog --check-config` validates without starting).

* Relative paths resolve against the project directory (or `--base-dir`).
* Keys marked **yes** can be changed at runtime with `POST /config` (validated, applied atomically,
  persisted to the file). Keys marked **file only** (bind address, port, token location, adapter targets,
  log paths, collector) can only be changed by editing the file and restarting. This is deliberate: nothing
  that decides *where the service listens* or *where directives are sent* can be changed over HTTP.
* Per-session modes are not part of the file; they are persisted separately in `state/state.json`.

## Cross-field rules

* `speed.critical_tps >= speed.warning_tps`
* `repetition.window_tokens >= repetition.threshold * repetition.min_sequence_tokens`
  (otherwise a loop could never fit into the window)
* `intervention.log_threshold <= alert_threshold <= auto_threshold`
* `progress.min_calls <= progress.window_calls`

## Confidence model (what the thresholds act on)

| Detection | Confidence |
|---|---|
| `text_repeat` | 0.78 at the threshold, +0.08 per extra repetition (max 0.98) |
| `sentence_repeat` | 0.5 at 3 repeats, +0.1 each (max 0.8): logged, a warn directive only from 0.6 |
| `tool_repeat_failed` | 0.88 (identical error) or 0.70 (changing errors), +0.04 per extra failure |
| `tool_repeat_edit` | 0.80, +0.05 per extra repetition |
| `tool_repeat_read` | 0.70 (0.60 if the result is unknown), +0.06 per extra repetition |
| `tool_repeat_search` | 0.65 (0.60 unknown), +0.06 per extra repetition |
| `tool_repeat_call` | 0.65 (0.55 unknown), +0.06 per extra repetition |
| `tool_cycle` | 0.72, +0.08 per extra cycle |
| `stagnation` | weighted score, capped at 0.9 |

Combined confidence = strongest active detection + corroboration (another *family*: text/tool/progress: +0.10;
tool+progress only: +0.05) + speed (critical +0.10, warning +0.05, only when a detection with confidence >= 0.4
already exists), capped at 0.99. With the defaults a single 3x text loop (0.78) is a *warning*; it reaches
automatic delivery at 4 repetitions, or at 3 repetitions together with a critical speed level or a
second independent detector. An identical failed command reaches automatic delivery at its 3rd failure.

## Keys

| Key | Type | Default | Range / choices | Runtime (API) | Meaning |
|---|---|---|---|---|---|
""" + "\n".join(rows) + "\n"
(ROOT / "CONFIGURATION.md").write_text(doc, encoding="utf-8")
print("wrote config/watchdog.config.json and CONFIGURATION.md;", len(rows), "keys")
