# Performance — what Antiloop actually costs

Everything here is measured on the target machine (Windows 11, 16 logical cores, Python 3.14,
llama.cpp/Strata serving a 1-bit Qwen pack with a GPU expert cache). Numbers that were not measured
are labelled as estimates; nothing on this page is asserted from intuition.

## Per-event cost of the Hermes bridge

The bridge is a shell hook, so every event costs one interpreter start. That start is the whole
budget:

| | before | now |
|---|---|---|
| bare `python -c pass` spawn (floor) | 32.7 ms | — (unchanged, it is the OS) |
| one hook event, urllib client | **95–138 ms** | — |
| one hook event, raw-socket client | — | **55–60 ms** |
| watchdog `POST /events` round trip | 2–4 ms | 2–4 ms |

The difference is imports: `urllib.request` pulled in `http.client`, `email`, `ssl` and friends —
about 55 ms of the interpreter's start-up, paid on every event. `integration/hermes_hook.py` now
speaks HTTP/1.1 over a plain socket, which needs no third-party module and no import beyond the
standard ones already loaded (`json`, `socket`, `hashlib`).

The HTTP/1.1 client handles `Content-Length` and chunked responses; a non-2xx status counts as "the
service is up and refused", not as a transport failure, so the circuit breaker keeps its old
meaning.

## What the numbers mean for a turn

A tool-using turn fires 5–9 hook events (`pre_llm_call`, `post_llm_call`, `post_api_request`, and
`pre`/`post_tool_call` per tool call). At 55–60 ms each that is **0.3–0.5 s per turn**; with the
urllib client it was 0.7–1.2 s.

An interleaved A/B on identical prompts (hooks on/off alternating, three pairs, same session
shape: 1 user message, 2 tool calls):

| run | hooks | no hooks |
|---|---|---|
| 1 | 21.28 s | 21.40 s |
| 2 | 20.24 s | 20.67 s |
| 3 | 20.08 s | 22.75 s |
| **median** | **20.24 s** | **21.40 s** |

The hook runs came out 1.07 s *faster* on median, which is not a real speed-up — it is the point:
at 55–60 ms per event the bridge's cost has dropped below the turn-to-turn variance (1–2.7 s here)
and is no longer measurable in a turn.

For comparison, the same A/B with the urllib client, measured earlier and not interleaved:
with hooks 20.28 s / 21.15 s, without hooks 18.54 s / 19.21 s — roughly 1.7 s of overhead that is
now gone.

## The streaming detector is free

| component | cost |
|---|---|
| `on_stream_delta` (600 deltas, ~2400 chars, normal prose) | 0.059 ms per delta → 0.3 % of one core at 50 tok/s |
| `analyse()` on 12 000 chars | 1.27 ms — runs once per 120 new chars, not per token |
| `transform_llm_output` on a 3000-char answer | 0.002 ms |
| watchdog service, idle | 19.7 MB RSS, 0.036 % CPU since start |

Hermes dispatches plugin stream hooks on background worker threads with bounded queues
(`agent/plugin_stream_hooks.py`), so plugin code never runs inline on the token path.

## Why the plugin is not used for the tool-loop layer

`plugins.isolation: host` is the effective setting on the reference install: non-bundled plugins are
loaded by a plugin-host process, and a `hermes … chat -q` CLI session never calls the plugin's
`register()`. Verified directly — an instrumented `register()` wrote no marker file across three
live CLI turns, while `hermes plugins list` reported the plugin "enabled".

An in-process variant of the six tool-loop hooks was implemented and passes its own 12 checks
(0.006 ms per event, same STOPLOOP protocol, blocks the repeated call and lets a different one
through), but it cannot run where plugins are hosted out of process. It is therefore **not**
shipped as the default path. The shell-hook bridge is what actually runs in CLI sessions.


## Hardening the client (after review)

The socket client is the one place where a misbehaving watchdog could hurt the agent, so it now
enforces four rules internally:

| rule | why |
|---|---|
| **one wall-clock deadline** (`TOTAL_S = 1.5`) covering connect + send + read + parse | a per-recv timeout lets a peer that dribbles one byte at a time hold the hook open indefinitely |
| **response cap** (`MAX_RESPONSE = 1 MiB`) | the hook never buffers unbounded data from the service |
| **truncated/garbled body → `{}`** | fail open; a half-written response must not raise inside the agent |
| **atomic breaker writes** (`os.replace` via a temp file) | two sessions racing on the breaker file must never read a half-written marker |

Verified against a hostile stub (`integration/test_hostile_peers.py`), one case per failure mode:

```
garbage            returned 0.00 s   no exception
truncated body     returned 0.00 s   no exception
chunked            returned 0.00 s   decoded correctly
oversized (5 MB)   returned 0.01 s   no exception
never-ending       returned 1.51 s   no exception   <- the deadline is what stops it
closes at once     returned 0.00 s   no exception
```

The real service answers `GET /status` in 2.1 ms, and the bridge harness stays at 35/35.

## Default event set

The bridge used to run six events. Two of them ( `post_llm_call`, `post_api_request` ) mostly fed
telemetry, not protection, and each cost a process spawn per turn. The default is now four:

| event | job |
|---|---|
| `pre_tool_call` | surgical block of the repeated call |
| `post_tool_call` | failure detection (the streak that arms the block) |
| `pre_llm_call` | delivers the STOPLOOP redirect into the next turn |
| `on_session_end` | closes the session cleanly |

With `intervention.allow_interrupt: false` in `config/watchdog.config.json` the service no longer
escalates to an interrupt request that this build cannot honour; the block plus the redirect is the
only real enforcement, and the docs now say so.

**One trade-off, stated plainly.** Dropping `post_llm_call` also removes the only text-loop signal
available in CLI sessions: the in-process stream detector that catches repeated sentences loads in
the gateway/plugin host, not in `hermes … chat -q`. Where the bridge runs without the plugin, a
repeated-sentence loop in the final answer is therefore not reported to the watchlog any more. The
tool-loop protection (detect, block, redirect) is untouched. If text-loop coverage in CLI matters
more than ~55 ms per turn, re-add that one event — it is a one-line change.

## Two confounders found while measuring

**1. The desktop window costs model speed.** The Hermes desktop app renders continuously while
llama.cpp wants the same GPU for its expert cache. Same prompt, same profile, alternating phases:

| phase | turns | wall median | model generation (Strata log) |
|---|---|---|---|
| window visible | 4 | 19.47 s | **49.1 tok/s** (n=13) |
| window minimised | 4 | 20.34 s | **55.5 tok/s** (n=12) |

The model generates ~13 % faster with the UI out of the way. Wall time did not follow — it is
dominated by prompt size and tool turns, and one visible turn ran to 31 s. Read the generation row,
not the wall row: the app is competing with the model, and that is not Antiloop's doing.

**2. Claude's 10-pair protocol, applied.** Identical prompt, hooks on/off alternating, a dedicated
profile so no session lease interfered:

```
hooks     23.0 22.3 24.9 21.9 22.7 20.3 20.8 21.6 18.5 22.7
no hooks  20.7 20.3 20.7 21.9 22.3 23.9 21.7 20.3 19.2 22.7
paired difference: mean +0.49 s, 95% CI [-0.76, +1.75]
```

Verdict: **no detectable difference** at this sample size — which is the honest form of the claim.
With four events at 55–60 ms the expected cost is ~0.25 s per turn, and the interval is consistent
with that; it is simply smaller than the ±1.3 s turn-to-turn spread.

A first attempt at this A/B produced 2.4 s "turns" for both arms: every run was rejected by Hermes'
session lease (`active session limit 1/1`, held by the desktop session). Those numbers were
discarded, and the measurement moved to a dedicated profile.

## What did not change: the model

Strata's own request log for the same period holds 22 999 generation samples across 72 server runs:

| | value |
|---|---|
| median generation rate | 45.8 tok/s |
| p25 / p75 | 36 / 54 tok/s |
| draft acceptance (speculative decoding) | 88 %, stable |
| trend across the log | none |

The project's reference baseline (measured before Antiloop existed) is 40–55 tok/s thinking, 34–50
answering, 48–49 overall decode. Those ranges are unchanged, which is the point: Antiloop does not
slow the model, it adds work to the agent around it, and that work is now 2.3× cheaper per event.
