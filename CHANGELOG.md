# Changelog

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
