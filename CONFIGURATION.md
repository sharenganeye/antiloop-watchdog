# CONFIGURATION

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
| `version` | int | `1` | 1 .. 1 | file only | Config schema version (must be 1). |
| `default_mode` | str | `"observe"` | observe / warn / automatic | yes | Mode for sessions without an override. `observe` is the safe default. |
| `require_session_id` | bool | `false` |  | yes | Reject events that carry no `session_id` instead of filing them under `__unattributed__`. |
| `api.host` | str | `"127.0.0.1"` |  | file only | Must be a loopback address. Anything else makes the service refuse to start. |
| `api.port` | int | `8791` | 0 .. 65535 | file only | 0 = pick a free port (tests). Default 8791 (llama.cpp uses 8080). |
| `api.token_file` | str | `"state/token"` |  | file only | Bearer token; generated (256-bit) on first start with mode 0600. `WATCHDOG_TOKEN` env var overrides it. |
| `api.max_body_bytes` | int | `1048576` | 1024 .. 16777216 | file only | Largest accepted request body (413 above). |
| `api.max_batch_events` | int | `100` | 1 .. 1000 | file only | Largest `{"events": [...]}` batch. |
| `paths.log_dir` | str | `"logs"` |  | file only | Directory for the JSON and human-readable logs. |
| `paths.state_dir` | str | `"state"` |  | file only | Directory for the token, pid file and persisted session modes. |
| `speed.enabled` | bool | `true` |  | yes | Disable speed monitoring entirely. |
| `speed.warning_tps` | float | `70.0` | 1 .. 10000 | yes | Indicator level only; never an interruption reason by itself. |
| `speed.critical_tps` | float | `80.0` | 1 .. 10000 | yes | Indicator level only; adds +0.10 to an *existing* detection's combined confidence (warning: +0.05). |
| `speed.min_block_tokens` | int | `5` | 1 .. 100000 | yes | Blocks smaller than this are ignored (tiny-block artefacts). |
| `speed.min_block_duration_s` | float | `1.0` | 0.01 .. 60 | yes | Blocks shorter than this are ignored. |
| `speed.window_s` | float | `60.0` | 5 .. 3600 | yes | Rolling window for the token-weighted speed. |
| `speed.min_window_duration_s` | float | `5.0` | 0 .. 3600 | yes | Rolling TPS is only reported once accepted blocks cover at least this many seconds. |
| `speed.min_window_tokens` | int | `64` | 0 .. 1000000 | yes | ... and at least this many tokens. |
| `speed.min_window_blocks` | int | `3` | 1 .. 1000 | yes | Rolling TPS is only reported when the window holds at least this many accepted blocks. |
| `speed.prefill_floor_tps` | float | `400.0` | 100 .. 1000000 | yes | Blocks faster than this are treated as mislabelled prompt prefill and ignored (measured prefill 1500-1800, decode <= 71). |
| `speed.baseline` | dict | `{"thinking": [40, 55], "answering": [34, 50], "tool_call": [50, 71]...` |  | yes | Measured normal ranges per phase; only used to label blocks below/within/above in metrics. |
| `repetition.enabled` | bool | `true` |  | yes | Disable text repetition detection. |
| `repetition.window_tokens` | int | `512` | 128 .. 4096 | yes | Rolling buffer size. Must be >= threshold x min_sequence_tokens. Longest detectable loop period ~ (window - min_sequence) / (threshold - 1). |
| `repetition.min_sequence_tokens` | int | `64` | 16 .. 1024 | yes | Length of the sequence that must repeat (approximate tokens: words + punctuation marks). |
| `repetition.threshold` | int | `3` | 2 .. 20 | yes | Occurrences of the sequence that make a loop. |
| `repetition.check_interval_tokens` | int | `16` | 1 .. 256 | yes | The n-gram check runs once per this many new tokens (CPU knob). |
| `repetition.code_threshold_multiplier` | int | `3` | 1 .. 20 | yes | Fenced code, `kind=code` and punctuation-heavy (JSON-like) windows need threshold x this many repetitions. |
| `repetition.structured_punct_ratio` | float | `0.45` | 0.1 .. 1.0 | yes | Fraction of punctuation tokens at which a window counts as structured. |
| `repetition.ignore_kinds` | list_str | `["tool_output", "file_content"]` |  | yes | Event `kind`s that are never inspected for text repetition. |
| `repetition.sentence_repeat_threshold` | int | `3` | 2 .. 20 | yes | Same normalised sentence this many times -> low-confidence (0.5-0.8) `sentence_repeat`. |
| `repetition.sentence_min_words` | int | `8` | 3 .. 100 | yes | Sentences shorter than this are never counted as repeats. |
| `repetition.ignore_patterns` | list_str | `[]` |  | yes | Regexes; a repeated sequence matching any of them is ignored (legitimately repeated instructions). |
| `tool_loop.enabled` | bool | `true` |  | yes | Disable tool-call loop detection. |
| `tool_loop.history` | int | `64` | 16 .. 512 | yes | Tool records kept per session (no raw arguments or results are stored). |
| `tool_loop.failed_repeat_threshold` | int | `3` | 2 .. 50 | yes | Same call failing this many times in one state epoch. |
| `tool_loop.identical_cycle_threshold` | int | `4` | 2 .. 50 | yes | Same exec/other call with the same result this many times in one epoch. |
| `tool_loop.read_repeat_threshold` | int | `4` | 2 .. 50 | yes | Same read with unchanged result this many times in one epoch. |
| `tool_loop.edit_repeat_threshold` | int | `3` | 2 .. 50 | yes | Identical edit this many times. |
| `tool_loop.search_repeat_threshold` | int | `3` | 2 .. 50 | yes | Same search with unchanged result this many times. |
| `tool_loop.cycle_max_period` | int | `4` | 2 .. 8 | yes | Longest A-B-C... action cycle that is looked for. |
| `tool_loop.cycle_repeats` | int | `3` | 2 .. 10 | yes | Repetitions of a cycle that make a loop. |
| `tool_loop.poll_threshold_multiplier` | int | `3` | 1 .. 20 | yes | Thresholds are multiplied by this for tools matching `poll_tools`. |
| `tool_loop.volatile_arg_keys` | list_str | `["timestamp", "ts", "time", "request_id", "id", "call_id", "tool_ca...` |  | yes | Argument keys dropped before fingerprinting (ids, timestamps). |
| `tool_loop.exempt_tools` | list_str | `[]` |  | yes | Regexes (case-insensitive) of tool names that are never evaluated. |
| `tool_loop.poll_tools` | list_str | `["^(sleep|wait|poll|process_poll|check_status|get_status|process_st...` |  | yes | Regexes of tools that legitimately repeat (sleep, status polling). |
| `tool_loop.tool_categories` | dict | `{"write": ["(write|edit|patch|replace|create_file|append|apply|save...` |  | yes | Regexes mapping tool names to read / write / search / exec. First match in the order write, search, read, exec wins. |
| `progress.enabled` | bool | `true` |  | yes | Disable stagnation scoring. |
| `progress.window_calls` | int | `24` | 8 .. 128 | yes | Number of most recent tool calls the stagnation score looks at. |
| `progress.min_calls` | int | `8` | 3 .. 128 | yes | No stagnation score before this many calls. |
| `progress.novelty_floor` | float | `0.35` | 0.05 .. 1.0 | yes | Fraction of distinct (action, result) pairs below which novelty counts against progress. |
| `progress.error_streak_start` | int | `3` | 2 .. 50 | yes | Consecutive failures from which the error signal starts to grow. |
| `progress.no_change_calls` | int | `15` | 3 .. 500 | yes | Calls without any state change from which that signal starts to grow. |
| `progress.alert_score` | float | `0.6` | 0.1 .. 1.0 | yes | Weighted stagnation score at which a `stagnation` detection is produced (confidence = min(0.9, score)). |
| `progress.weights` | dict | `{"novelty": 0.35, "errors": 0.25, "duplicate_results": 0.2, "no_sta...` |  | yes | Signal weights: novelty, errors, duplicate_results, no_state_change. |
| `intervention.log_threshold` | float | `0.5` | 0.0 .. 1.0 | yes | Detections below this confidence are kept for corroboration but not logged. |
| `intervention.alert_threshold` | float | `0.6` | 0.0 .. 1.0 | yes | Minimum combined confidence for a warn-level directive. |
| `intervention.auto_threshold` | float | `0.85` | 0.0 .. 1.0 | yes | Minimum combined confidence for automatic delivery (automatic mode only). |
| `intervention.detection_ttl_s` | float | `120.0` | 1 .. 3600 | yes | How long a detection counts as active evidence for corroboration. |
| `intervention.realert_step` | int | `2` | 1 .. 100 | yes | A repeating detection is re-reported after this many additional repetitions ... |
| `intervention.realert_min_interval_s` | float | `5.0` | 0 .. 3600 | yes | ... and at least this many seconds. |
| `intervention.cooldown_s` | float | `30.0` | 0 .. 3600 | yes | Minimum time between directives (warn) / deliveries (automatic) per session. |
| `intervention.failure_retry_s` | float | `15.0` | 0 .. 3600 | yes | Shorter retry gap after a failed delivery. |
| `intervention.escalate_after_redirects` | int | `2` | 1 .. 20 | yes | Delivered redirects that failed to stop the loop before an interrupt is requested. |
| `intervention.escalation_grace_s` | float | `10.0` | 0 .. 3600 | yes | Seconds a redirect must have been delivered before escalation is considered. |
| `intervention.allow_interrupt` | bool | `true` |  | yes | Set false to forbid interrupts entirely (redirect only). |
| `intervention.recovery_quiet_s` | float | `60.0` | 1 .. 86400 | yes | Quiet period (no loop evidence) after which a session counts as recovered. |
| `intervention.recovery_min_events` | int | `10` | 0 .. 100000 | yes | ... and minimum number of events since the intervention. |
| `intervention.directive_ttl_s` | float | `600.0` | 5 .. 86400 | yes | Unacknowledged directives expire after this long. |
| `intervention.message` | str | `"You appear to be stuck in a repetitive loop. Stop repeating previo...` |  | yes | Corrective text for redirect directives (default: the required text). |
| `intervention.interrupt_message` | str | `"Watchdog requests that this session's current generation/turn be i...` |  | yes | Text of interrupt directives. |
| `adapter.type` | str | `"queue"` | queue / webhook / null | file only | `queue` (pull, default), `webhook` (push to a loopback URL you implement), `null` (no delivery). |
| `adapter.redirect_url` | str | `""` |  | file only | Webhook only. http:// on a loopback host. File-only (not changeable via API). |
| `adapter.interrupt_url` | str | `""` |  | file only | Webhook only. Same rules. Empty = interrupts cannot be pushed. |
| `adapter.timeout_s` | float | `3.0` | 0.2 .. 30 | file only | Webhook request timeout (requests run on a background thread, ingestion never waits). |
| `adapter.auth_env` | str | `""` |  | file only | Name of an environment variable whose value is sent as `Authorization: Bearer ...` to the webhook. |
| `sessions.max_sessions` | int | `64` | 2 .. 1024 | yes | Tracked sessions; the least recently seen one is evicted beyond this. |
| `sessions.idle_ttl_s` | float | `3600.0` | 30 .. 604800 | yes | Sessions without events for this long are dropped. |
| `sessions.max_events` | int | `2000` | 100 .. 100000 | yes | Size of the in-memory event ring served by `GET /events` (the log files keep everything). |
| `logging.json_file` | str | `"watchdog.jsonl"` |  | file only | JSON-lines event log (file name inside `paths.log_dir`). |
| `logging.text_file` | str | `"watchdog.log"` |  | file only | Human-readable log. |
| `logging.max_bytes` | int | `5242880` | 65536 .. 1073741824 | file only | Rotate each log file at this size ... |
| `logging.backups` | int | `3` | 0 .. 20 | file only | ... keeping this many rotated files. |
| `logging.capture_text` | bool | `false` |  | yes | Opt-in diagnostic capture: adds a truncated sample of the *repeated* sequence to events. Default off. |
| `logging.capture_chars` | int | `200` | 0 .. 2000 | yes | Maximum characters of that sample. |
| `collector.llama_log_path` | str | `""` |  | file only | Opt-in: tail this llama.cpp/Strata log for speed samples. Empty = disabled. Verify the format first with `tools/check_llama_log.py`. |
| `collector.llama_log_from_start` | bool | `false` |  | file only | Replay the file from its beginning instead of attaching at the end. |
| `collector.poll_interval_s` | float | `1.0` | 0.5 .. 60 | file only | Poll interval of the log tailer (the only polling in the service). |
