# What was verified against a real Hermes (and what cannot be done)

Written after integrating on Hermes v0.22.5 (2026). Everything below was measured, not assumed.

## Verified

| Claim | Evidence |
|---|---|
| Shell hooks deliver per-turn and per-tool events with a stable `session_id` | 19 events from one live session; three identical failing calls produced `tool_repeat_failed` (0.88) |
| `pre_llm_call` can inject a corrective text into the running turn | `{"context": …}` returned and consumed by Hermes |
| `pre_tool_call` can refuse a call | `{"action":"block","message":…}` stops execution and reaches the model |
| A detection can be turned into a refusal **inside** one turn | the ingest reply carries `detections` + `mode`; the block is armed there |
| `on_stream_delta` fires per token in CLI worker sessions | one delta per token, `session_id` + `turn_id` on every event |
| `transform_llm_output` can replace the persisted answer | 1759-char looped answer stored as 246 chars, marker present |
| Per-session isolation | directives are pulled by session id only; two concurrent sessions never crossed |

## Not available

- **True generation interrupt.** `AIAgent.interrupt()` / `request_hard_interrupt()` exist in-process
  only. Hermes binds an `InterruptScope` for the web-dashboard route; measured **absent** in CLI
  worker sessions and in the shared gateway process they run in. Cancelling from a hook subprocess
  is therefore impossible without a core patch, which updates would wipe.
- **Token-level text through shell hooks.** Hooks see text once per turn; per-token text requires
  the in-process plugin (layer A), which is what this project ships.
- **Strata / llama.cpp log tailer.** `strata.exe` has no log flag; the wrapper can write engine
  stderr to a file via a `log` key in its config JSON, but its line format (`[strata] done: N tokens
  in X s`) is not the llama.cpp `eval time = … ms / N tokens` form the parser expects. The
  `post_api_request` hook already provides tokens + duration, so the tailer stays off.

## Architectural facts worth remembering

1. CLI `hermes -p X chat -q` sessions execute **inside the shared gateway process**, not in their
   own. One process, many sessions — never design a session-scoped action around process identity.
2. Hermes caches a plugin manager per profile home for the process lifetime. Shell hooks hot-reload
   in ~30 s; plugins need a restart.
3. `pre_llm_call` fires once per turn. Any "guide the model mid-turn" design must ride a tool call.
4. Fail-open is not optional: an agent that stalls because its watchdog died is worse than a loop.

## Design consequences

- L2 blocks the **repeated** call and lets a different one through — a blanket block would stop
  legitimate work that is not looping.
- The refusal message carries the whole recovery protocol, so it does not depend on the model
  choosing to open a skill.
- Detection lives in memory keyed by `(session_id, turn_id)`, with a TTL longer than the turn, so
  the output transform can still see it after `on_stream_end`.
