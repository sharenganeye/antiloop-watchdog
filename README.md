# Antiloop — anti-loop watchdog for local LLM agents (Hermes + llama.cpp / Strata)

LLMs running locally on your own hardware sometimes get stuck. Two failure modes matter, and they
need different answers:

1. **Tool loops.** The agent issues the same failing command, re-reads the same file, or repeats
   the same edit over and over. Tokens burn, nothing progresses.
2. **Text loops.** The model starts repeating one word or one sentence and keeps going until the
   token budget ends — the classic degeneration of a quantised model under greedy decoding.

Antiloop covers both. It is a small, standard-library-only Python service plus a native Hermes
integration. No API keys, no cloud, no GPU, no external database.

```
┌─────────────┐   stream/tool events    ┌──────────────────┐   directive    ┌──────────────┐
│  Hermes     │ ──────────────────────► │  antiloop        │ ─────────────► │  Hermes      │
│  agent      │ ◄────────────────────── │  watchdog        │  (context /    │  agent       │
│  session    │   block / stoploop      │  :8791 loopback  │   tool block)  │  session     │
└─────────────┘                         └──────────────────┘                └──────────────┘
```

## What it does

**Watchdog service** — bounded per-session state, no LLM involved:

| Detector | Catches |
|---|---|
| text repetition | a 64-token sequence repeated 3× in a 512-token window; the same sentence 3× |
| tool loops | same failing command 3×, unchanged re-read 4×, identical edit 3×, same search 3×, A-B cycles |
| stagnation | error streaks, duplicate results, low novelty, no state change |
| speed indicator | block throughput against a baseline; corroborates, never triggers alone |

Per-session modes: `observe` (record only) → `warn` (queue a corrective message) →
`automatic` (deliver it, escalate to an interrupt request after two failed redirects). Automatic
mode is refused for any session without an explicit id. The service cannot stop the gateway, cannot
restart llama.cpp and cannot touch a process.

**Hermes integration** — three layers, all fail-open:

| Layer | Mechanism | Effect |
|---|---|---|
| L1 | `pre_llm_call` hook | injects a STOPLOOP recovery protocol into the current turn |
| L2 | `pre_tool_call` hook | blocks only the *repeated* tool call, lets a different one through |
| L3 | native cross-check | Hermes' own repeated-failure warning triggers an early intervention |
| A | `on_stream_delta` plugin | detects a word/sentence loop ~1 s into generation, records it |
| B | `transform_llm_output` plugin | truncates a looped answer before it enters the transcript |

L2 is a **tool-execution block, not a model-generation cancel**. A true interrupt is not available
through hooks, and this project does not pretend otherwise. See
[`integration/HERMES_INTEGRATION.md`](integration/HERMES_INTEGRATION.md).

**Server-side prevention.** The most effective single measure against text loops is not a watchdog
at all: it is sampling. Greedy decoding on a low-bit quantisation is the textbook repetition
generator. Measured on a 1-bit Qwen pack with a pathological prompt:

| Decoding | Repetitions before the model stopped | Tokens used |
|---|---|---|
| engine default (greedy) | 220 (ran to the limit) | 220 |
| `temperature 0.6 · top_p 0.9 · repeat_penalty 1.1 · repeat_last_n 512` | 37 | 38 |

## Install

Nothing to install for the runtime — Python 3.9+ and the standard library.

```bash
git clone https://github.com/sharenganeye/antiloop-watchdog.git
cd antiloop-watchdog

# Windows
scripts\start_watchdog.ps1          # background, hidden, no service registration
scripts\healthcheck.ps1 --status

# Linux / WSL / macOS
./scripts/start_watchdog.sh
./scripts/healthcheck.sh --status
```

Then wire it into Hermes: see **[INTEGRATION_MANUAL.md](INTEGRATION_MANUAL.md)** — a complete,
step-by-step procedure (hooks, consent allowlist, plugin, verification canary, rollback).

Run the tests first if you want proof:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q        # 98 passed, 1 skipped (POSIX-only SIGTERM test)
```

## Safety properties

- Binds to `127.0.0.1` only, bearer token, Host-header check.
- Cannot stop, restart or signal any process. No shell, no eval of received text.
- Per-session state; a directive for one session is never applied to another.
- Prompts and outputs are hashed or inspected in memory, never stored (unless you opt in).
- Every Hermes-side hook fails open with a 0.8 s ceiling and a 45 s circuit breaker: if the
  watchdog dies, your agent keeps working and pays one short delay per minute, not a stall.

## Requirements

Python 3.9+ (developed on 3.12/3.14). Optional: `psutil` for the resource-measurement simulator,
`pytest` for the test suite. Nothing else.

## Documentation

| File | Contents |
|---|---|
| [INTEGRATION_MANUAL.md](INTEGRATION_MANUAL.md) | the full integration procedure, from zero, with verification and rollback |
| [HERMES_WATCHDOG_INTEGRATION.md](HERMES_WATCHDOG_INTEGRATION.md) | the API, event schema, modes, directives |
| [ARCHITECTURE.md](ARCHITECTURE.md) | internal design and state model |
| [CONFIGURATION.md](CONFIGURATION.md) | every configuration key |
| [TEST_REPORT.md](TEST_REPORT.md) | test coverage and measured overhead |
| [integration/HERMES_INTEGRATION.md](integration/HERMES_INTEGRATION.md) | what was verified on real Hermes, and what is not possible |
| [docs/PERFORMANCE.md](docs/PERFORMANCE.md) | measured per-event and per-turn cost, and why the model speed is unaffected |
| [Press kit](https://sharenganeye.github.io/antiloop-watchdog/press.html) | one-page brief for a video, article or technical review |

## Support

Antiloop is free and MIT-licensed, and it stays that way. If it earns its place in your setup and
you would like to support its development, there is an optional support page:

**[Support the project →](https://sharenganeye.github.io/antiloop-watchdog/donate.html)**

Nothing is gated behind it and nothing is expected of you — same code, same licence either way.
A bug report or a false positive you hit is just as welcome.

## License

MIT — see [LICENSE](LICENSE).
