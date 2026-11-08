# Antiloop protection architecture

Two independent layers, because a loop can happen in two different places. Neither layer can cover
the other's failure, and that is the whole reason both exist.

| | Layer A — model output | Layer B — agent actions |
|---|---|---|
| what it catches | the model repeating itself (same sentence or phrase) | the agent repeating the same failing operation |
| where it runs | in-process plugin, `integration/hermes_plugin/antiloop_stream/` | shell hooks + detached watchdog, `integration/hermes_hook.py` |
| how it stops it | `transform_llm_output` replaces the looped tail before the turn is persisted | `pre_tool_call` refuses the repeated call before it executes |
| evidence in this repo | `tests/test_stream_finalize.py`, detector harness | `tests/test_hook.py`, `tests/test_hostile_peers.py` |

## Layer A — model output

The plugin watches `on_stream_start` / `on_stream_delta` / `on_stream_end`, keeps a bounded per-turn
buffer keyed by `(session_id, turn_id)`, and runs the detector every `CHECK_EVERY_CHARS` (120) characters.
Thresholds: `SENTENCE_REPEATS = 3`, `SENTENCE_MIN_WORDS = 8`, `PHRASE_REPEATS = 4` over 8-token units.
A structured-text veto refuses to fire on lists, tables and code fences, because those repeat legitimately.

On a confirmed detection the turn is flagged and `transform_llm_output` replaces the response with the
meaningful prefix plus an explicit truncation marker. That transform runs **before** the assistant row is
first persisted, so the text the user sees is the text stored — truncating later would be impossible.

The finalisation flush: `on_stream_delta` only analyses every 120 characters, so a reply that ends inside
the last sub-window was never examined. `on_stream_end` now analyses the residual buffer once before
releasing the turn, and claims the fire under the lock so a concurrent delta and end cannot both record.
Without this, a 285-character three-sentence reply escaped detection entirely.

## Layer B — agent actions

`hermes_hook.py` is spawned by Hermes for each hook event, receives one JSON object on stdin and prints at
most one JSON object on stdout. Events are forwarded to the watchdog, which fingerprints `(session, tool,
normalised args, outcome)` and decides whether the agent is looping.

Enforcement: when a detection at confidence >= 0.85 arrives, the hook writes a *surgical break* for that
exact tool call. The next identical `pre_tool_call` is refused with `{"action": "block", ...}` and the
recovery protocol; a different call passes untouched, so the agent is forced to change approach rather
than being frozen.

**Enforcement requires `automatic` mode.** `observe` records only. `warn` records and arms the break but
never refuses. This is deliberate: a supervisor watching a session must not have its tools taken away.

Detections arm the break in *every* mode, because the watchdog deduplicates detections with a cooldown —
a detection seen while a session was still advisory has to leave something armed, or switching to
`automatic` later would have no fresh detection to act on. The session mode is cached in a small
`md-<hash>` file so `pre_tool_call` never needs an extra HTTP round trip to learn it.

## Findings that shaped this design

1. **Final-buffer cadence defect.** Analysing only every N characters means short repetitive replies are
   never examined. Fixed by analysing the residual at stream end.
2. **Stale module / process lifetime.** Plugin and hook configuration load once per process. A gateway
   that has been running since before a deployment keeps executing the old module, silently. Every
   "detection never fires" symptom in development traced back to this. A deployment is not complete until
   the process serving traffic has started after it.
3. **Gateway plugin loading.** The process that streams and the process that loads plugins for tool
   workers are not the same thing; capability-check log lines prove a plugin *loads*, not that its stream
   hooks are registered in the process that owns the token stream.
4. **Plugin hooks vs shell hooks.** Plugin hooks run in-process and can transform output
   (`transform_llm_output`). Shell hooks run as child processes and can veto a tool call (`pre_tool_call`)
   but cannot modify a response. They also never need plugin loading, which is why Layer B keeps working
   when Layer A is stale.
5. **Observation cannot replace transformation.** `post_llm_call` sees the complete final response with
   real session and turn ids, but by then the text has been persisted and is about to be delivered.
   Detecting there can only constrain the *next* turn.
6. **Why the shell prototype could not intercept the current response.** There is no shell-hook seam that
   rewrites an assistant message; `transform_llm_output` is plugin-only.
7. **Why the two layers are separate.** Output repetition is a generation problem inside one turn; tool
   looping is a control problem across turns. Different processes, different enforcement points.

## Evidence status — read this before trusting a claim

Live-verified on the target machine (2026-11-08):

- Layer A: four genuine detections recorded with real session and turn ids; the truncation marker was
  observed in delivered replies.
- Layer B: three identical failing `terminal` calls produced `tool_repeat_failed` at confidence 0.88 in
  `automatic` mode; the identical fourth call was refused with `{"action":"block"}` and never executed;
  a different command in the same session ran normally.
- Global deployment: all twelve profiles carry the same plugin hash; a newly created profile provisioned
  with `integration/provision_profiles.py` inherited the plugin, four hooks and four approvals.

Harness-only (not yet observed in a live turn): concurrent delta/end de-duplication, TTL expiry of the
break under real load, the exact latency contribution of the hook on a slow machine.

## Deployment

1. Copy the plugin: `integration/hermes_plugin/antiloop_stream/` into `<profile>/plugins/antiloop-stream/`.
2. Ensure hooks + allowlist: `python integration/provision_profiles.py --all` (idempotent; `--check` to
   report only).
3. Restart whatever serves conversations. Configuration written to disk does not reach a running process.
4. Watchdogs: run `integration/start_watchdogs.ps1` (idempotent - probes `/health` before starting). It is
   safe to place in the user's Startup folder for reboot persistence.

Rollback: restore the plugin copy and `config.yaml` / `shell-hooks-allowlist.json` from the backup taken
before deployment, then restart the serving process. Detection state lives in the watchdog state directory
and under `integration/state/`; deleting it is always safe.

## Known limitations

- Layer A cannot stop token generation; it removes the repeated text from what the user receives.
- Layer B needs `automatic` mode to refuse a call; in `observe` and `warn` it only records and arms.
- A tool loop whose arguments differ slightly every iteration is not blocked by the surgical break; it
  needs the engine's own fingerprint families.
- `post_llm_call`-based detection is redundant whenever the plugin's transform is active on the same
  surface, because the transform runs first and leaves nothing repetitive to observe.
