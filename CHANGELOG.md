# Changelog

## 1.1.0-beta.1 - 2026-11-08  (PRE-RELEASE)

Beta: the patch is fully tested but has NOT been installed and exercised end to end on a live
Hermes profile. The stable 1.1.0 version number is reserved for the release that passes the
installation and performance gate.

P1 hardening round: eight findings from external review plus four defects found while reviewing the
patch itself. Backwards compatible with 1.0.x; no configuration change is required.

### Hook and client (integration/hermes_hook.py)
- An incomplete HTTP reply is no longer treated as success. Completion is now proven, not assumed:
  a Content-Length body must arrive in full, a chunked body must carry every chunk delimiter, a
  valid trailer and the zero-chunk terminator, and a close-delimited body needs an observed close.
  Reaching the response cap counts as truncation, never as completion.
- Trailer field names are validated against the RFC 7230 token character set; an empty name, a
  space, a control byte or a non-ASCII byte is rejected.
- An oversize stdin payload is dropped unparsed and counted instead of being truncated into
  invalid JSON (4 MiB ceiling).
- A 503 counts as unavailability and opens the breaker; an ordinary 4xx still does not.
- All three blocking paths require automatic mode, so warn stays advisory.

### Watchdog server (antiloop_watchdog/api.py)
- Listen backlog raised from 5 to 128.
- Bounded worker pool (64) with a 250 ms wait before refusing.
- Slot ownership is tracked and released exactly once under a lock; a refused connection can no
  longer release another worker's slot.
- The handler timeout is actually applied to the connection (it was dead configuration).
- The overload refusal drains a bounded amount of the request first, so clients receive the 503
  instead of a connection reset.

### Client and detector
- Typed errors: WatchdogError base with WatchdogUnavailable and WatchdogHTTPError; empty, malformed
  and undecodable replies all raise instead of returning None or leaking JSONDecodeError.
- Repetition detection matches case-insensitively, so a capitalised loop is truncated instead of
  being detected and ignored.
- An unclosed code fence no longer latches across turns; repetition history is preserved so loops
  spanning a turn boundary are still caught.

### Tests
- 49 new regression tests (tests/test_p1_regressions.py and test_p1_followup*.py). The eight
  regression tests were written before the patch and all failed against 1.0.1.
- Full suite: 147 passed, 1 skipped (the POSIX-only SIGTERM test).

### Notes
- Version 1.0.0 and 1.0.1 remain available as tags and releases.
- This release is not yet installed on any machine; installing it is a separate step.

## 1.0.1 — 2026-11-07

Performance and hardening pass, driven by a measured slowdown report.

### Changed

- **Hook bridge talks HTTP/1.1 over a socket** instead of `urllib`. `urllib.request` dragged in
  `http.client`, `email` and `ssl` — about 55 ms of interpreter start-up, paid on every hook event.
  Measured: **95–138 ms → 55–60 ms per event**.
- **One wall-clock deadline** (1.5 s) covers connect + send + read + parse. The previous per-recv
  timeout let a peer that dribbles bytes hold the hook open.
- **Response capped at 1 MiB**, truncated or garbled bodies fail open to `{}`, breaker state written
  atomically (`os.replace`) because two sessions race on it.
- **Default event set cut from six to four**: `pre_llm_call`, `pre_tool_call`, `post_tool_call`,
  `on_session_end`. `post_api_request` and `post_llm_call` fed telemetry rather than protection.
  Trade-off: without `post_llm_call` a CLI session no longer reports repeated-sentence loops to the
  watchdog (that detector lives in the plugin host). Re-add the event if you want it back.
- **`intervention.allow_interrupt: false`** by default: this build cannot honour an interrupt, so the
  service no longer escalates to one. The surgical block plus the STOPLOOP redirect is the
  enforcement path.

### Added

- `integration/test_hostile_peers.py` — garbage, truncated, chunked, oversized, never-ending and
  immediate-close peers. Every case returns without raising; the never-ending one stops at the
  deadline (1.51 s).
- `docs/PERFORMANCE.md` — per-event and per-turn costs with the measurement method, plus two
  confounders: the desktop UI costs the *model* ~13 % throughput (49.1 vs 55.5 tok/s), and the
  hooks themselves show no detectable turn-level cost (10 interleaved pairs, +0.49 s,
  95 % t-interval [−1.03, +2.02]).
- The integration harness is now policy-aware: it reads `/config` and asserts the interrupt behaviour
  that the policy actually enables.

### Fixed

- `tests/test_service_process.py` asserted the version as a literal; it now reads the package's own
  `VERSION`.

## 1.0.0 — 2026-11-07

First public release: watchdog service, Hermes shell-hook bridge, streaming text-loop detector,
final-output truncation, test suites, integration manual, docs site.
