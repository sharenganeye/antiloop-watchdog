# HERMES_WATCHDOG_INTEGRATION

Audience: a Hermes agent (or engineer) who has to finish the integration without reading the source.
Everything below is implemented and covered by `tests/`, **except where a section is explicitly marked UNVERIFIED**.

## 0. Read this first: what is verified and what is not

| Area | Status |
|---|---|
| Watchdog HTTP API, detectors, modes, directives, logging, persistence | Implemented and tested (see `TEST_REPORT.md`) |
| Pull delivery (`queue` adapter): Hermes polls directives and acknowledges them | Implemented and tested with the bundled Python client |
| Push delivery (`webhook` adapter) | Tested against `examples/webhook_receiver_example.py`, **not** against Hermes |
| Hermes' own event stream (tokens, tool calls), session id, "inject a message into a running session", "cancel this session's current turn" | **UNVERIFIED.** No Hermes was available when this was built. No Hermes API was assumed or invented. Section 12 lists exactly what must be found out. |

The watchdog never calls Hermes unless you configure a webhook URL. By default it only *stores* directives and
Hermes *fetches* them. It cannot stop the gateway, restart llama.cpp or touch any process.

## 1. What the watchdog does

It receives a stream of small JSON events about each Hermes session (generated text, tool calls and their results,
speed measurements), keeps bounded per-session state, and detects:

* repeated text (rolling 512-token window, 64-token sequence, 3 repetitions) and repeated sentences,
* repeated tool calls (same failing command, same read without change, identical edit, same search, A-B-A-B cycles),
* stagnation (low novelty, error streaks, duplicate results, no state change for many calls),
* a speed indicator (70 / 80 tok/s) that can only *corroborate* another detection, never trigger anything alone.

It then, depending on the session's **mode**, logs, exposes a warning with a corrective message, or delivers that
message to the offending session and escalates to an interrupt request if redirection keeps failing.
No LLM is involved anywhere.

## 2. Connecting

* Base URL: `http://127.0.0.1:8791` (config `api.host` / `api.port`; loopback only, anything else is refused).
  Use the literal `127.0.0.1`; the Host header must be `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`.
* Token: file `state/token` (created on first start, mode 0600), or environment variable `WATCHDOG_TOKEN` (min 16 chars)
  set for **both** the watchdog and Hermes.
* Send it as `Authorization: Bearer <token>` (or header `X-Watchdog-Token: <token>`).
* Only `GET /health` works without a token.

Python (standard library only, importable from the project directory):

```python
import sys; sys.path.insert(0, r"<project dir>")            # or run Hermes tooling from the project dir
from antiloop_watchdog.client import WatchdogClient, WatchdogUnavailable

wd = WatchdogClient.from_files(r"<project dir>/state")      # reads state/token; base_url defaults to 127.0.0.1:8791
print(wd.health())          # {'status': 'ok', 'service': 'antiloop-watchdog', 'version': '1.0.0', 'uptime_s': 3.2}
```

curl:

```bash
TOKEN=$(cat state/token); URL=http://127.0.0.1:8791
curl -s -H "Authorization: Bearer $TOKEN" $URL/status
```

**Fail open.** Every call can raise `WatchdogUnavailable` (service down, timeout). Hermes must catch it and carry on;
the watchdog is a safety net, never a dependency. Default client timeout is 2 s; keep it short.

## 3. Health checks

* `GET /health` (no token): `{"status":"ok","service":"antiloop-watchdog","version":"1.0.0","uptime_s":12.3}`.
* `GET /status` (token): version, uptime, default mode, adapter info, session counts per mode, counters, last detection,
  process RSS/CPU, config warnings. `adapter.verified_against_hermes` is always `false` until you change the code
  after verifying a real Hermes adapter.
* Scripts: `scripts/healthcheck.sh`, `scripts/healthcheck.ps1`, or `python tools/healthcheck.py --status` (exit code 0/1).

## 4. How Hermes identifies its own session

Put the same `session_id` into **every** event for that session.

* Allowed: `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`. Must be stable for the whole life of the session and different
  for the two concurrent sessions. Use whatever Hermes already uses to tell sessions apart (UNVERIFIED which field
  that is; find it with section 12).
* Not allowed: ids starting with `slot-` or `__` (reserved for the llama.cpp log tailer and the "unattributed" bucket).
* Events **without** a `session_id` are filed under `__unattributed__`. That bucket is detected and logged, but
  **automatic mode is impossible for it** (a request for `automatic` is refused with 409 `automatic_not_allowed`,
  and a default mode of `automatic` is downgraded to `warn` for it). This is the "no identification -> no
  automatic intervention" rule. Set `require_session_id: true` to reject anonymous events outright.
* Send `{"type":"session_end","session_id":"..."}` when a session finishes so its state is released.

## 5. Sending events

`POST /events` with one event object, or `{"events":[...]}` (1-100 per call). The response tells you immediately
whether this event produced new alerts:

```json
{"session_id":"hermes-1","accepted":true,"detections":[ <event objects, see section 6> ],
 "pending_directives":1,"mode":"warn"}
```

Batch responses: `{"accepted":2,"rejected":[{"index":1,"code":"invalid_event_type","error":"..."}],"detections":[...]}`.

| `type` | Required fields | Optional | Notes |
|---|---|---|---|
| `text_delta` | `session_id` | `text` (<=64k chars), `kind`, `final`, `tokens` | `kind`: `reasoning`, `answer` (default), `code`, `tool_output`, `file_content`. Only reasoning/answer/code are inspected; `tool_output` and `file_content` are ignored for text repetition (legitimately repetitive). Chunks may split words; the service reassembles. Send `final:true` (empty text is fine) at the end of a message to flush. |
| `generation` | `session_id`, `tokens`, `duration_s` | `phase` | Measured block speed. `phase`: `thinking`, `answering`, `tool_call`, `decode` (default), `prefill`. Blocks < 5 tokens or < 1 s, prefill blocks and anything >= 400 tok/s are ignored for generation speed. |
| `tool_call` | `session_id`, `tool` | `args`, `call_id`, `tokens`, `status`, `result_text`, `result_hash` | Send when the call is issued, or send it complete (with `status` + `result_text`) after execution in one event. `tokens` = tokens the model spent producing this call (feeds wasted-token estimates). |
| `tool_result` | `session_id`, `call_id` (or `tool`) | `status`, `result_text`, `result_hash`, `args`, `tokens` | Completes a `tool_call` with the same `call_id`. `status`: `ok`, `error`, `unknown`. |
| `state` | `session_id` | `changed` (default true) | "The task state changed" (a todo ticked, tests went green, files written outside tool calls). Starts a new epoch so earlier repetition no longer counts. |
| `session_end` | `session_id` | | Releases the session. |

Privacy: raw `args` / `result_text` / `text` are hashed or inspected in memory and **never stored or logged**
(unless you enable `logging.capture_text`, which only adds a truncated sample of a *repeated* sequence).

What makes detection work well:

1. Stream reasoning/answer text as `text_delta` (batch to >= 20 tokens or 0.25-0.5 s per request; the service
   handled thousands of events/s in the sandbox, but a per-token HTTP call is pointless).
2. Report every tool call with its **real arguments** and, when available, `status` and `result_text` (error text
   makes "same failure again" reliable; volatile keys such as ids/timestamps are stripped automatically).
3. Send `state` when something genuinely progressed that is not a write tool call.
4. Optionally send `generation` blocks with measured timings; without them there is simply no speed indicator.

Python:

```python
def on_stream_chunk(session_id, text, phase):               # phase: "thinking" | "answering"
    try:
        wd.text_delta(session_id, text, kind="reasoning" if phase == "thinking" else "answer")
    except WatchdogUnavailable:
        pass

def on_tool_finished(session_id, call_id, tool, args, ok, output):
    try:
        r = wd.tool_call(session_id, tool, args, call_id=call_id,
                         status="ok" if ok else "error", result_text=output)
        for det in r["detections"]:
            log.warning("watchdog: %s confidence=%s", det["detection_type"], det["confidence"])
    except WatchdogUnavailable:
        pass
```

## 6. Retrieving alerts: events and their JSON fields

Three equivalent ways, use whichever fits:

1. **Inline**: the `detections` array in every `POST /events` response (new alerts caused by that event).
2. **Poll the stream**: `GET /events?since_id=<last>&session_id=<id>&event_type=detection&limit=100` ->
   `{"events":[...],"next_since_id":N}`. Remember `next_since_id`. The ring holds the last 2000 events;
   the JSONL log keeps everything.
3. **Directives** (section 7): ready-made corrective messages.

Every event has exactly these fields (null when not applicable); schema id `antiloop.event.v1`:

```json
{
  "schema": "antiloop.event.v1", "id": 4, "ts": "2026-11-07T05:24:42.534Z", "ts_epoch": 1794029082.535,
  "session_id": "curl-demo-1", "event_type": "detection", "mode": "warn", "tps": null,
  "detection_type": "tool_repeat_failed", "confidence": 0.88,
  "sequence_fingerprint": null, "tool_fingerprint": "a75d15694bb3ae75", "repetitions": 3,
  "intervention_action": null, "intervention_result": null, "directive_id": null,
  "wasted_tokens_est": null,
  "details": {"combined_confidence": 0.88, "families": ["tool"], "speed_level": "normal",
              "evidence": {"tool": "terminal", "category": "exec", "epoch_repeats": 3, "target_fp": "fdea65af231c",
                           "identical_error": true, "failures": 3}}
}
```

| Field | Meaning |
|---|---|
| `event_type` | `session_started`, `session_ended`, `detection`, `intervention`, `intervention_result`, `speed_indicator`, `mode_change`, `session_reset`, `recovered`, `directive_acked`, `config_changed`, `service_started`, `service_stopped` |
| `mode` | Effective mode of the session at that moment |
| `tps` | Rolling token speed (falls back to the last block); null if unknown |
| `detection_type` | `text_repeat`, `sentence_repeat`, `tool_repeat_failed`, `tool_repeat_read`, `tool_repeat_edit`, `tool_repeat_search`, `tool_repeat_call`, `tool_cycle`, `stagnation` |
| `confidence` | 0-1 confidence of this detection (`details.combined_confidence` includes corroboration) |
| `sequence_fingerprint` | Hash of the repeated text unit (stable across rotations of the loop) |
| `tool_fingerprint` | Hash of the normalised tool action (or cycle) |
| `repetitions` | Number of suspected repetitions |
| `intervention_action` / `intervention_result` | `redirect` / `interrupt`, and e.g. `queued_for_pull`, `pushed`, `dispatch_pending`, `push_failed: ...`, `adapter_unavailable: ...`; for `directive_acked`: `applied`/`ignored`/`failed` |
| `directive_id` | Links intervention events to directives |
| `wasted_tokens_est` | Tokens spent on the repeated part, **only where measurable** (text: `(reps-1) x period`; tools: sum of reported `tokens` of repeats); otherwise null |

Human-readable and JSON logs: `logs/watchdog.log`, `logs/watchdog.jsonl` (rotating, 5 MiB x 3).

## 7. Modes, directives, and how Hermes applies them

### Modes (per session; default `observe`)

| Mode | The watchdog... | Typical use |
|---|---|---|
| `observe` | collects, detects, logs evidence. Creates no directive, sends nothing, ever. | Start here. Review logs for false positives. |
| `warn` | additionally creates a **directive** (the corrective message) when combined confidence >= 0.60. Exposed via the API. Never pushed, never interrupts. | Hermes decides what to do with the warning. |
| `automatic` | like `warn`; once confidence >= 0.85 it **delivers** a redirect through the adapter (push) or marks it ready for pull. Escalates to `interrupt` only after 2 *delivered* redirects failed to stop the loop. Impossible for sessions without an explicit id. | After observe/warn proved reliable. |

Set the default: `POST /config {"default_mode":"warn"}` (persisted). Set one session:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -X POST $URL/sessions/hermes-1/mode -d '{"mode":"automatic"}'     # observe | warn | automatic | default
```

Per-session modes survive restarts (`state/state.json`). Setting a mode creates the session record if it does not
exist yet. `"mode":"default"` removes the override.

### Directives (the corrective messages)

```json
{"schema":"antiloop.directive.v1","id":"d-e40cbaaa1ed1","session_id":"hermes-1","action":"redirect",
 "message":"You appear to be stuck in a repetitive loop. Stop repeating previous reasoning and actions. Review what has already been completed, identify the blocker, preserve completed work, and select a different concrete approach. Do not restart the same failed sequence.",
 "source":"warn","status":"pending","delivery":"queued","created_at":"...","expires_at":"...",
 "delivered_at":null,"acked_at":null,"ack_result":null,
 "reason":{"detection_type":"tool_repeat_failed","confidence":0.88,"families":["tool"],"repetitions":3,
           "sequence_fingerprint":null,"tool_fingerprint":"a75d1569...","speed_level":"normal"}}
```

* `action`: `redirect` (inject `message` into **this** session as a corrective instruction) or `interrupt`
  (stop **this** session's current turn; only created in `automatic` mode after failed redirects, or on explicit request).
* `status`: `pending` -> `delivered` (fetched via GET, or pushed successfully) -> `acked`; or `expired` (10 min default) or `superseded`.
* `delivery`: `queued` (waiting for pull), `pushed`, `push_failed`, `unavailable`.

**Pull loop (recommended, no Hermes-side server needed):**

```python
def pump(session_id):                                        # call between agent steps; one cheap GET
    try:
        for d in wd.directives(session_id):                  # marks pending directives 'delivered'
            assert d["session_id"] == session_id             # never apply a directive to another session
            ok = apply_to_hermes_session(session_id, d)      # <-- UNVERIFIED Hermes call, see section 12
            wd.ack(session_id, d["id"], "applied" if ok else "failed")   # 'applied' | 'ignored' | 'failed'
    except WatchdogUnavailable:
        pass
```

`GET /sessions/{id}/directives?peek=1` reads without marking delivered. `?status=expired` etc. filters.
**Acknowledge honestly**: escalation to `interrupt` happens only if a redirect was delivered/acknowledged and the loop
still persists. Acknowledging with `failed` blocks escalation for that redirect and shortens the retry gap
(`failure_retry_s`) so a corrected attempt can be delivered sooner.

**Push option (webhook adapter):** in `config/watchdog.config.json` set
`"adapter": {"type":"webhook","redirect_url":"http://127.0.0.1:8795/redirect","interrupt_url":"http://127.0.0.1:8795/interrupt","timeout_s":3,"auth_env":"HERMES_HOOK_TOKEN"}`
(loopback URLs only; file-only setting; restart required). The watchdog POSTs
`{"schema":"antiloop.directive.v1","session_id","directive_id","action","message","reason","sent_at"}` with
`Authorization: Bearer $HERMES_HOOK_TOKEN` and treats any 2xx as success. Calls run on a background thread; a slow
or dead receiver never blocks ingestion. `examples/webhook_receiver_example.py` is the reference receiver.
The directive is **also** kept in the pull queue.

## 8. Requesting an intervention yourself

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -X POST $URL/sessions/hermes-1/intervene -d '{"action":"redirect","message":"Try a different approach."}'
# -> {"directive": {... "source":"manual" ...}}
```

* `message` is optional (<= 2000 chars; default is the standard text). `action`: `redirect` or `interrupt`.
* Allowed only if the session's mode permits it: `observe` -> 409 `mode_observe`; `warn` -> redirect is queued
  (409 for `interrupt`); `automatic` -> redirect/interrupt are delivered through the adapter. Unknown session -> 404.
* `POST /sessions/{id}/reset` clears detectors, speed state and open directives (use after you changed approach manually).

## 9. Not interrupting other sessions

Guarantees built in (all tested in `tests/test_multisession.py`, `tests/test_intervention.py`):

* Detector state, counters, directives, cooldowns and escalation are per session; identical commands in two sessions are never added up.
* Every adapter call carries exactly one session id, and the engine asserts directive and session match before dispatch.
* The adapter interface has no global operation: no "stop gateway", no "restart llama.cpp", nothing that is not scoped to one session.
* No session id -> no automatic intervention. Pseudo sessions `slot-N` (log tailer, speed only) can never be targeted.
* Intervening in one session resets only that session's detectors.

Your obligations on the Hermes side: apply a directive only to the session named in `directive.session_id`, and
implement `interrupt` as "cancel this session's current turn", never as a process or gateway operation.

## 10. Recovering from failures

| Situation | What happens / what to do |
|---|---|
| Watchdog unreachable | Client raises `WatchdogUnavailable`; Hermes continues normally. Restart with `scripts/start_watchdog.*`. Config, token and per-session modes persist; in-memory detector state and the event ring are rebuilt from new events (the JSONL log keeps history). |
| Push failed / webhook down | Event `intervention` with `push_failed: ...` (or `intervention_result` event for async pushes), directive stays `pending`/`push_failed` in the pull queue, and a retry is allowed after `failure_retry_s` (15 s) instead of the full cooldown. Fix the receiver or switch to pull. |
| `adapter.type` is `null` | Directives are created and queued, `delivery:"unavailable"`; nothing is pushed. |
| Directive never fetched | It expires after `directive_ttl_s` (600 s); a new directive of the same action supersedes older pending ones. |
| Loop persists after a delivered redirect | Next detection after cooldown (30 s) sends another redirect; after 2 delivered redirects and a 10 s grace the next detection requests `interrupt` (if `allow_interrupt`). |
| Loop stopped | After 60 s without loop evidence (and >= 10 events) an event `recovered` is emitted and escalation counters reset. |
| False positive | Put the tool in `tool_loop.exempt_tools` / `poll_tools`, the phrase in `repetition.ignore_patterns`, or raise a threshold with `POST /config`; set the session to `observe`. |
| Stuck or confusing state | `POST /sessions/{id}/reset`, or `POST /sessions/{id}/mode {"mode":"observe"}`. |

## 11. Endpoint reference

| Method + path | Auth | Purpose |
|---|---|---|
| `GET /health` | no | liveness |
| `GET /status` | yes | service status |
| `GET /metrics` | yes | JSON metrics; `?format=prometheus` for text exposition |
| `GET /sessions` / `GET /sessions/{id}` | yes | session summaries / detail incl. directives |
| `GET /events` | yes | event stream (`since_id`, `limit`, `session_id`, `event_type`) |
| `POST /events` | yes | ingest one event or `{"events":[...]}` |
| `GET /config` / `POST /config` | yes | read / patch runtime-mutable configuration (nested JSON) |
| `POST /sessions/{id}/mode` | yes | `{"mode": observe|warn|automatic|default}` |
| `POST /sessions/{id}/reset` | yes | reset detectors and open directives |
| `POST /sessions/{id}/intervene` | yes | manual directive (section 8) |
| `GET /sessions/{id}/directives` | yes | pull directives (`status`, `peek`) |
| `POST /sessions/{id}/directives/{did}/ack` | yes | `{"result":"applied|ignored|failed"}` |

Errors are `{"error":{"code":"...","message":"..."}}` with 400 (validation), 401, 403 (bad Host), 404, 405, 409 (mode/eligibility conflict), 413, 415, 500.
`examples/curl_examples.sh` and `examples/hermes_shim_example.py` were executed against a live instance while writing this document.

## 12. What still has to be verified on the Hermes side (checklist for the integrating agent)

Run `python tools/discover_environment.py` on the machine that runs Hermes (read-only; writes `reports/environment.json`
with the Hermes version, `--help` text, llama endpoint props). Then establish, from the **installed** Hermes version and
its own documentation, and record the answers in this file:

1. **Event source:** which hook/callback/log gives streamed reasoning and answer text per session, tool calls with their
   arguments, and tool results. (If none exists, a tool-call wrapper plus a stream wrapper must be added on the Hermes side.)
2. **Session id:** which stable per-session identifier is available at those hook points.
3. **Redirect:** the supported way to inject a corrective instruction into a *running* session (next-turn user/system message?).
4. **Interrupt:** the supported way to cancel only that session's current turn, and proof that it cannot affect the other slot or the gateway.
5. Whether Hermes can host a loopback webhook receiver (push) or should poll (pull, recommended first).
6. Whether the Strata log format matches `collector.py` (`python tools/check_llama_log.py <logfile>`); optional, speed indicator only.

> **ANSWERED on the target machine, 2026-11-07.** Checklist items 1, 2, 3 and 5 are answered
> from the installed Hermes v0.22.5 and verified live; 4 (interrupt) is not achievable through
> shell hooks; 6 (Strata log) has no log file to check. Wiring, evidence, defects found and
> remaining gaps: `integration/INTEGRATION_STATUS.md`. The bridge is
> `integration/hermes_hook.py`, its harness `integration/test_hook.py`.

Then: implement the hooks with the fail-open client, run for a while in `observe`, review `logs/watchdog.jsonl`
for false positives, move to `warn`, and enable `automatic` per session only after the redirect call has been tested on a
throw-away session with a deliberately looping prompt. Do not claim an integration works until that test passed.

## 13. Troubleshooting

* `401 unauthorized`: wrong/missing token; read `state/token` (or `WATCHDOG_TOKEN`).
* `403 forbidden_host`: you used a hostname other than `127.0.0.1`/`localhost`.
* `409 automatic_not_allowed`: the session has no explicit `session_id` (or is a `slot-N` pseudo session).
* No detections: send tool calls with real `args`, send text with `kind` reasoning/answer, check `GET /sessions/{id}` -> `events` counters and `speed`.
* Service refuses to start: `python -m antiloop_watchdog --check-config` prints the offending key.
