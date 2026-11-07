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
