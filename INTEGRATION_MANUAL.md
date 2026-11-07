# Integration manual — wiring Antiloop into Hermes

This is the complete procedure, written so that it can be followed again from zero: on a new
machine, in a new profile, or after a Hermes update that broke something. Every step has a
verification, and every verification has an expected output.

Nothing here needs the internet, an API key, or a GPU.

---

## 0. What you need

| Requirement | Check |
|---|---|
| Python 3.9+ | `python -V` |
| Hermes with shell hooks (v0.22.x does) | `hermes hooks --help` prints `list, test, revoke, doctor` |
| The Antiloop project on disk | `python -m antiloop_watchdog --check-config` |
| A profile you can edit | `%LOCALAPPDATA%\hermes\profiles\<profile>\config.yaml` (or `~/.hermes/…`) |

Paths used below:

- `ANTILOOP` = the project directory (e.g. `C:/tools/antiloop-watchdog`)
- `PROFILE` = the Hermes profile home (e.g. `C:/Users/you/AppData/Local/hermes/profiles/myprofile`)

---

## 1. Start the service

```powershell
cd ANTILOOP
.\scripts\start_watchdog.ps1        # Windows
```

```bash
cd ANTILOOP && ./scripts/start_watchdog.sh    # POSIX
```

**Verify**

```bash
curl -s http://127.0.0.1:8791/health
# {"status":"ok","service":"antiloop-watchdog","version":"1.0.1","uptime_s":3.2}
```

First start creates `state/token` (mode 0600) and `state/watchdog.pid`.

> Windows note: `start_watchdog.ps1` starts the service correctly but its own process may not
> return. Verify with the health call, not with the script's exit.

---

## 2. Hermes shell hooks (layers L1–L3)

### 2.1 Add the block

Append to `PROFILE/config.yaml`:

```yaml
hooks:
  pre_llm_call:
    - command: '"ANTILOOP/.venv/Scripts/python.exe" "ANTILOOP/integration/hermes_hook.py"'
      timeout: 5
  pre_tool_call:
    - command: '"ANTILOOP/.venv/Scripts/python.exe" "ANTILOOP/integration/hermes_hook.py"'
      timeout: 5
  post_tool_call:
    - command: '"ANTILOOP/.venv/Scripts/python.exe" "ANTILOOP/integration/hermes_hook.py"'
      timeout: 5
  post_llm_call:
    - command: '"ANTILOOP/.venv/Scripts/python.exe" "ANTILOOP/integration/hermes_hook.py"'
      timeout: 5
  post_api_request:
    - command: '"ANTILOOP/.venv/Scripts/python.exe" "ANTILOOP/integration/hermes_hook.py"'
      timeout: 5
  on_session_end:
    - command: '"ANTILOOP/.venv/Scripts/python.exe" "ANTILOOP/integration/hermes_hook.py"'
      timeout: 5
```

Replace `ANTILOOP` with the real path, using forward slashes. If you have no project `.venv`, use
any Python 3.9+ interpreter — the bridge is standard library only.

### 2.2 Consent (the allowlist)

Hermes asks once, interactively, for each `(event, command)` pair and stores the answer in
`PROFILE/shell-hooks-allowlist.json`. Non-interactive runs (`chat -q`, cron, gateway) never prompt,
so add the entries directly:

```json
{
  "approvals": [
    {"event": "pre_llm_call",   "command": "<exact command string>"},
    {"event": "pre_tool_call",  "command": "<exact command string>"},
    {"event": "post_tool_call", "command": "<exact command string>"},
    {"event": "post_llm_call",  "command": "<exact command string>"},
    {"event": "post_api_request", "command": "<exact command string>"},
    {"event": "on_session_end", "command": "<exact command string>"}
  ]
}
```

The command string must match the configured one **exactly**, including both quotes.

### 2.3 Verify

```bash
hermes -p PROFILE hooks list     # 6 hooks, each "(timeout=5s, ✓ allowed)"
hermes -p PROFILE hooks doctor   # 18/18: exists, allowlisted, clean exit, valid JSON
hermes -p PROFILE hooks test post_tool_call
```

The last one fires a synthetic event; `curl -s -H "Authorization: Bearer $(cat ANTILOOP/state/token)" \
http://127.0.0.1:8791/sessions` must show a new `test-session` entry.

---

## 3. Hermes plugin (layers A + B — text loops)

Copy the plugin into the profile and enable it by name:

```bash
mkdir -p PROFILE/plugins/antiloop-stream
cp ANTILOOP/integration/hermes_plugin/antiloop_stream/* PROFILE/plugins/antiloop-stream/
```

```yaml
# PROFILE/config.yaml
plugins:
  enabled:
    - antiloop-stream
```

### 3.1 The two rules that silently break this step

1. **The directory name must equal the manifest `name`.** `antiloop-stream/` containing
   `name: antiloop-stream`. A mismatch (`antiloop_stream/`) is skipped without an error.
2. **Hermes caches a plugin manager per profile home for the life of the process.** Shell hooks
   re-read config within ~30 s; plugins do not. A plugin added to a profile that the running
   gateway has already used stays inactive until that process restarts. A profile used for the
   first time loads it immediately.

### 3.2 Verify

```bash
hermes -p PROFILE plugins list | grep antiloop-stream      # "enabled"
```

Then one real turn in that profile, and check the record it writes:

```bash
cat PROFILE/antiloop-stream/detections.jsonl
# no file = the plugin has not been exercised yet (or is not loaded: see 3.1)
```

If you create a throw-away profile for testing, remove it with `hermes profile delete -y <name>` —
deleting the directory by hand leaves its databases locked by the running gateway.

A deliberately looping prompt must produce exactly two JSONL lines: one `phrase`/`sentence`
detection and one `output_truncated` with `before_chars` much larger than `after_chars`.

---

## 4. The canary (run after every Hermes update)

```bash
python ANTILOOP/integration/verify_integration.py
```

Nine checks: service health, bridge present + hash, six configured events **and** consent in every
listed profile, `hooks doctor` clean, and a synthetic event that must actually be ingested. Exit 0
means the integration is intact; a failing line names what to redo.

Edit the `PROFILES` list at the top of that script if your profile names differ.

---

## 5. Rollout: observe → warn → automatic

```bash
TOKEN=$(cat ANTILOOP/state/token)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8791/status         # modes per session
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"mode":"warn"}' http://127.0.0.1:8791/sessions/<session_id>/mode
```

Start everything in `observe`, read `logs/watchdog.jsonl` for a day or two, then move one
**disposable** session to `warn` and run a failure you can recognise. Only after a redirect has
actually changed a turn's course should `automatic` be considered — and never for sessions without
an explicit id. `POST …/mode {"mode":"default"}` removes a per-session override.

---

## 6. What is hard-won knowledge, not documentation

These cost real debugging time; keep them in mind when something looks broken.

| Symptom | Cause | Fix |
|---|---|---|
| Detection fires, but nothing is ever blocked inside one turn | `pre_llm_call` fires **once per turn**, before the tool loop; a loop inside a turn never reaches it | L2 arms the block from the `POST /events` reply (`detections` + `mode`), not from a pulled directive |
| Watches nothing after a plugin install | plugin manager cached per profile home | restart the gateway, or use a fresh profile |
| Plugin enabled but absent from `plugins list` | directory name ≠ manifest name | rename the directory |
| Every hook suddenly slow | watchdog down, connection not refused instantly | circuit breaker in `hermes_hook.py` (45 s), 0.8 s ceiling |
| `python -c` blocked in an unattended session | Hermes approval policy, unrelated to Antiloop | expected behaviour, not a defect |
| `tools/simulate.py` crashes on Windows | old version read `/proc` and sent `SIGINT` | current version has a ctypes branch and `terminate()` |
| CPU% reads 0 and RSS ~4 MB | the venv `python.exe` is a launcher stub; its child does the work | current version aggregates the process tree |

**Not available, by design:** a true model-generation interrupt. Hooks can refuse a tool call;
they cannot cancel a request in flight. `AIAgent.interrupt()` exists but only in-process, and
Hermes binds an `InterruptScope` only in the web-dashboard route — verified absent for CLI worker
sessions, which is where agents actually run. Do not describe L2 as an interrupt.

---

## 7. Surviving `hermes update`

Everything lives outside the `hermes-agent` install tree:

| Piece | Location | Survives update |
|---|---|---|
| `hooks:` block | `PROFILE/config.yaml` | yes (profile data) |
| consent | `PROFILE/shell-hooks-allowlist.json` | yes |
| plugin | `PROFILE/plugins/antiloop-stream/` | yes |
| bridge + service | anywhere outside the install | yes |

The real risks are silent: an event name removed, or a payload key renamed, would not raise — the
pipe would simply carry less. That is what `verify_integration.py` is for. Run it after every
update; if it fails, re-apply section 2 and restart the gateway for section 3.

---

## 8. Uninstall / rollback

```bash
# 1. stop delivering corrections
hermes -p PROFILE hooks revoke "<exact command string>"     # or delete the hooks: block

# 2. remove the plugin
rm -rf PROFILE/plugins/antiloop-stream                      # and its entry under plugins.enabled

# 3. stop the service
#    Windows: end the pid in ANTILOOP/state/watchdog.pid
#    POSIX:   ./scripts/stop_watchdog.sh

# 4. delete state if you want a clean slate
rm -rf ANTILOOP/state ANTILOOP/logs ANTILOOP/antiloop-stream
```

Removing `integration/hermes_hook.py` alone also works: every hook exits non-zero and Hermes fails
open, so the agent keeps running with no protection.

---

## 9. Event mapping (reference)

| Hermes hook | Watchdog event | Why |
|---|---|---|
| `pre_tool_call` | `tool_call` (tool, args, call_id) | tool-loop detection |
| `post_tool_call` | `tool_result` (status, result_text) | failed-repeat / unchanged read |
| `post_llm_call` | `text_delta` (answer, final) | message-level repetition |
| `post_api_request` | `generation` (tokens, duration) | speed indicator |
| `pre_llm_call` | — (pulls directives) | corrective context injection |
| `on_session_end` | `session_end` | releases session state |
| plugin `on_stream_delta` | `text_delta` on detection | token-level loop evidence |

Directive actions: `redirect` (inject the message) and `interrupt` (stop the turn — **not
implemented** in the hook path; only the console route has the capability).
