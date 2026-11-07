# ARCHITECTURE

## Goals and constraints

An independent, event-driven, lightweight process that detects wasted inference (looping text, repeated tool calls,
stagnation) for several concurrent agent sessions on one local llama.cpp server, **without** another LLM, without
modifying Hermes/Strata, and without any ability to stop processes. Standard library only (Python >= 3.9; developed and
tested on 3.12), so there is nothing to install and nothing to break on a newer interpreter.

## Component map

```
 Hermes-side shim / hooks                         optional: llama.cpp / Strata log file
        | POST /events (JSON)                              | incremental tail (1 s)
        v                                                  v
 +--------------------- api.py (ThreadingHTTPServer, loopback, token) ---------------------+
 |  routes -> validation (schema.py) -> engine.ingest()          collector.py -> engine     |
 +-------------------------------------------+-----------------------------------------------+
                                             v   one RLock
                    engine.py: sessions{ id -> Session }, event ring, counters
                    +---------------------------------------------------------------+
                    | Session (per session id, fully isolated)                      |
                    |   SpeedMonitor     speed.py      indicator only               |
                    |   RepetitionDetector repetition.py  text n-gram + sentences   |
                    |   ToolLoopDetector toolloop.py   fingerprints, epochs, cycles |
                    |   ProgressDetector progress.py   stagnation score             |
                    |   directives, cooldown, escalation, recovery state            |
                    +--------------------------+------------------------------------+
                                               v Detection records
                    engine._process(): de-duplicate, keep best per family, combine
                                               v Combined(confidence, families, speed)
                    intervention.py: mode gate -> directive -> adapter (queue|webhook|null)
                                               v
                    logsetup.py: logs/watchdog.jsonl + logs/watchdog.log (notable events only)
```

| Required component | Implementation |
|---|---|
| Event Collector | `api.py` `POST /events` (+ batch), `collector.py` (optional log tailer) |
| Token Speed Monitor | `speed.py` |
| Repetition Detector | `repetition.py` |
| Tool-Call Loop Detector | `toolloop.py` |
| Progress/Stagnation Detector | `progress.py` (reads `ToolLoopDetector.records`, no duplicate state) |
| Intervention Engine | `intervention.py` |
| Hermes Integration API | `api.py`, `client.py`, `adapters.py` |
| Logging and Diagnostics | `logsetup.py`, `GET /status`, `GET /metrics`, `GET /events` |
| Configuration Manager | `config.py` (+ `state.py` for per-session modes) |
| Dry-run and test framework | `tests/`, observe mode, `tools/simulate.py`, `tools/check_llama_log.py` |

## Detection algorithms

**Text repetition** (`repetition.py`). Text is tokenised into words and punctuation marks (an approximation of model
tokens), appended to a rolling buffer (`window_tokens`, 512). Every `check_interval_tokens` (16) new tokens the most recent
`min_sequence_tokens` (64) tokens are compared, exactly (no hash collisions), with every earlier position in the window. Non-overlapping
occurrences are counted when the period is >= 64; for shorter periods (a sentence repeated forever) the periodic chain
length is used. `threshold` (3) repetitions = loop. The fingerprint is the hash of the *least rotation* of the repeating
unit, so it stays the same however the loop is phased (O(n) Booth-style algorithm). Cost per check is one pass over <= 512 tokens;
measured in the sandbox (CPython 3.12, one core): 1.7 us/token for normal prose, 2.6 us/token for a pure loop, 16.8 us/token for the
degenerate case `the the the ...` (at 50 tok/s that is under 0.1 % of one core; `tests/` asserts a generous 50 us bound).
A second mechanism counts identical normalised sentences (>= 8 words); it is intentionally low confidence (0.5-0.8).
False-positive guards: `tool_output`/`file_content` kinds are skipped; fenced code, `kind=code` and punctuation-heavy windows
need 3x the repetitions; punctuation-only runs are ignored; `ignore_patterns` suppress known repeats.
Limit: with window 512 / sequence 64 / threshold 3, loops with a period above ~224 tokens cannot fit and are not detected by this
mechanism (other detectors, or a larger window, cover them).

**Tool-call loops** (`toolloop.py`). Each call is fingerprinted from `tool name + normalised arguments` (volatile keys such as ids and
timestamps removed, whitespace collapsed, long strings hashed); results from a hash of the (normalised, for errors) text or a supplied
hash. Raw arguments/results are not retained. A **state epoch** counter makes legitimate repetition safe: a *new* write call or a
`state` event starts a new epoch and repetition counts only look at the current epoch ("edit, test, edit, test" is progress;
"test, test, test" with nothing changed is not). Rules, evaluated per call/result: repeated failure (identical or changing errors),
identical edit, same read with unchanged result, same search with unchanged result, same non-failing call with unchanged result, and
A-B-(C-D) cycles of period 2-4. Polling tools (`sleep`, status checks) get 3x thresholds; `exempt_tools` are skipped entirely.

**Stagnation** (`progress.py`). Four normalised signals (novelty of (action, result) pairs, error streak, duplicate results, calls since
the last state change) combined with weights 0.35/0.25/0.2/0.2; reported above 0.6. A long read-only exploration of distinct files
scores low on three of the four signals, so it does not alert by itself.

**Speed** (`speed.py`). Token-weighted rolling speed over 60 s from `generation` blocks. Ignored: prefill phase, blocks < 5 tokens,
blocks < 1 s, anything >= 400 tok/s (mislabelled prefill; measured prefill is 1500-1800, decode <= 71). Needs >= 3 blocks, >= 5 s and
>= 64 tokens before a rolling value exists. Levels: warning >= 70, critical >= 80. The speed monitor **never emits a detection**; the
engine only adds +0.05/+0.10 to the combined confidence of an *already existing* detection (>= 0.4).

## Fusion

Each session keeps the strongest recent detection per family (`text`, `tool`, `progress`) for `detection_ttl_s` (120 s).
Combined confidence = strongest + family bonus (+0.10 per additional family; tool+progress only +0.05, because progress is derived
from the same tool records) + speed bonus, capped at 0.99. Repeating detections are re-reported only after +2 repetitions and
>= 5 s, so a running loop does not flood the log.

## Intervention state machine (per session)

```
observe:    detection -> log                                   (no directive, ever)
warn:       conf >= 0.60 and cooldown ok -> directive(pending, source=warn) -> log
automatic:  conf >= 0.85 and session identified and cooldown ok
              -> directive -> adapter.redirect()  (queue: stays pending for pull | webhook: async push)
              -> on success: in_intervention=true, detectors of THIS session reset
              -> if 2 delivered redirects failed and last redirect delivered >= 10 s ago and allow_interrupt
                 and adapter supports it: adapter.interrupt() instead of another redirect
            0.60 <= conf < 0.85, or unidentified session: behaves like warn
quiet 60 s and >= 10 events after an intervention -> `recovered`, escalation counters reset
```

Warn-level and automatic-level cooldowns are tracked separately so a queued warning never delays a real delivery.
Failed deliveries use the shorter `failure_retry_s`. Webhook calls run on one background thread
(`intervention.py::_worker`), so a slow Hermes cannot stall ingestion or other sessions.

## Multi-session isolation

All mutable state lives in `Session` objects keyed by `session_id`; the only shared pieces are the config snapshot, the event ring and
counters. Events without an id go to `__unattributed__` (never eligible for automatic mode); log-tailer samples go to `slot-N`
pseudo sessions (speed only, never eligible). Ids starting with `slot-`/`__` cannot be supplied through the API. The controller asserts
`directive.session_id == session.id` before every dispatch, and the adapter interface exposes no operation that is not scoped to one
session.

## Concurrency and resource design

* Threads: HTTP accept thread + one short-lived thread per connection; one tick thread (wakes every 5 s for housekeeping, or every
  `collector.poll_interval_s` when the log tailer is enabled; that is the only polling); one dispatch thread, created lazily for webhooks.
* One `RLock` guards all engine state; handlers do microseconds of work under it. `ConfigManager.data` is copy-on-write, so detectors
  read configuration lock-free and always see a consistent snapshot.
* Bounded memory: rolling text buffer <= window+128 tokens per session; 64 tool records; 64 sentence fingerprints; 32 directives;
  event ring 2000; 64 sessions (LRU eviction); idle sessions dropped after 1 h.
* Disk: only notable events are logged (detections, interventions, mode changes, ...), never per-event traffic; logs rotate at 5 MiB x 3.
  Config/state writes are atomic (temp file + `os.replace`) and happen only on change.
* Writes use one buffered segment with `TCP_NODELAY`; without this, Nagle/delayed-ACK interaction capped throughput at ~45 events/s
  (found while measuring; see `TEST_REPORT.md` for the measured result after the fix).
* No inference, no GPU libraries, no third-party packages.

## Security model

Loopback bind enforced (non-loopback `api.host` refuses to start); bearer token (256-bit, file mode 0600, constant-time comparison)
for everything except `/health`; Host-header allow-list (DNS-rebinding); JSON only, body size cap, strict validation with bounded
field sizes; adapter targets, bind address, ports, paths and the log-tail path are file-only settings, not changeable over HTTP;
webhook URLs must be `http://` on loopback; no endpoint can execute commands, signal processes or read files.
Prompts and model output are never stored; fingerprints only (optional truncated sample of a *repeated* sequence if
`logging.capture_text` is enabled).

## Extension points

* New detector: return `Detection` objects from a class with the same per-session lifecycle and register it in `Session`/`engine.ingest`.
* New delivery path: subclass `HermesAdapter` (`redirect`, `interrupt`), set `push`/`blocking`, add it to `adapters.build_adapter`.
  Keep it session-scoped. A verified Hermes adapter belongs here once section 12 of `HERMES_WATCHDOG_INTEGRATION.md` is answered.
* New event source: anything that can call `engine.ingest(event)` (see `collector.py` for an in-process example).
