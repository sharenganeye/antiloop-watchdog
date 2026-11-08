#!/usr/bin/env python
"""Hermes shell-hook -> antiloop-watchdog bridge (single file, stdlib only, fail-open).

Wire it up in a profile's config.yaml:

    hooks:
      pre_tool_call:  [{command: "<py> <this> "}]
      post_tool_call: [{command: "<py> <this> "}]
      post_llm_call:  [{command: "<py> <this> "}]
      on_session_end: [{command: "<py> <this> "}]
      pre_llm_call:   [{command: "<py> <this> "}]

Hermes pipes one JSON payload on stdin and reads optional JSON on stdout.
- every event except pre_llm_call: forward as a watchdog event, print nothing.
- pre_llm_call: pull this session's pending directives and, if any, print
  {"context": "<corrective message>"} so Hermes appends it to the current turn's
  user message; then acknowledge it.

Guarantees: never raises, always exits 0, hard timeout on every HTTP call, no
state kept on disk, no text stored (the watchdog hashes/inspects in memory).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import socket
from pathlib import Path

BASE_URL = os.environ.get("WATCHDOG_URL", "http://127.0.0.1:8791")
ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = Path(os.environ.get("WATCHDOG_TOKEN_FILE", ROOT / "state" / "token"))
TIMEOUT_S = 0.8          # per-recv ceiling
TOTAL_S = 1.5            # one wall-clock deadline for connect + read + parse
MAX_RESPONSE = 1_048_576  # never buffer more than this from the watchdog
BREAKER_S = 45           # after a transport failure, stay silent this long (circuit breaker)
STDIN_LIMIT = 4_194_304  # hard ceiling on one hook payload; oversize is recorded, never truncated
MAX_TEXT = 60_000        # watchdog accepts <=64k chars per text_delta
MAX_RESULT = 16_000
BREAKER_FILE = ROOT / "integration" / ".watchdog_down"
BREAK_DIR = ROOT / "integration" / "state"
BREAK_TTL_S = 120        # after an interrupt directive, for THIS session only
BLOCK_MIN_CONF = 0.85    # a detection at or above this refuses the repeated call itself
NATIVE_WARN_COOLDOWN_S = 300
BREAK_MESSAGE = ("Watchdog: this turn was stopped by an interrupt request because the loop "
                 "continued after corrective messages. Do not call more tools now. Summarise what "
                 "is complete, state the blocker, and stop this turn.")
STOPLOOP_PROTOCOL = (
    "STOPLOOP protocol - follow it before your next tool call:\n"
    "1. STOP: do not repeat the action you just took.\n"
    "2. RESTATE the goal in one line.\n"
    "3. LIST what you already tried and the exact failure or duplicate you produced.\n"
    "4. CHOOSE a materially different approach (different tool, different arguments, different "
    "file, smaller scope) - or state that you are blocked and why.\n"
    "5. Say the new step in one line, then execute it.\n"
    "The full checklist is the 'stoploop' skill: skill_view(name='stoploop')."
)
SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _break_path(sid: str) -> Path:
    return BREAK_DIR / ("break-" + hashlib.sha1(sid.encode("utf-8", "replace")).hexdigest()[:20])


def _break_record(sid: str) -> dict | None:
    try:
        data = json.loads(_break_path(sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if time.time() - float(data.get("ts") or 0) >= BREAK_TTL_S:
        return None
    return data


def _canon(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _break_blocks(sid: str, tool: str, args) -> bool:
    """Surgical block: refuse the repeated call, allow a different one (a real change of approach)."""
    rec = _break_record(sid)
    if rec is None:
        return False
    if not rec.get("tool"):          # just armed: the next call is the one that repeats
        return True
    if rec.get("tool") != tool:
        return False
    return _canon(rec.get("args")) == _canon(args)


def _break_arm(sid: str, tool: str | None = None, args=None, message: str = "") -> None:
    try:
        BREAK_DIR.mkdir(parents=True, exist_ok=True)
        _break_path(sid).write_text(
            json.dumps({"ts": time.time(), "tool": tool, "args": args, "message": message},
                       ensure_ascii=False, default=str),
            encoding="utf-8")
    except OSError:
        pass


def _break_complete(sid: str, tool: str, args) -> None:
    """Remember which call was blocked so only identical repeats stay blocked."""
    rec = _break_record(sid)
    if rec is not None and not rec.get("tool"):
        _break_arm(sid, tool, args)


def _native_warn_path(sid: str) -> Path:
    return BREAK_DIR / ("nw-" + hashlib.sha1(sid.encode("utf-8", "replace")).hexdigest()[:20])


def _native_warn_due(sid: str) -> bool:
    try:
        return (time.time() - _native_warn_path(sid).stat().st_mtime) >= NATIVE_WARN_COOLDOWN_S
    except OSError:
        return True


def _native_warn_stamp(sid: str) -> None:
    try:
        BREAK_DIR.mkdir(parents=True, exist_ok=True)
        _native_warn_path(sid).write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


def _breaker_open() -> bool:
    """True when the last transport attempt failed recently -> skip HTTP, fail fast."""
    try:
        age = time.time() - BREAKER_FILE.stat().st_mtime
    except OSError:
        return False
    return age < BREAKER_S


def _atomic_write(path: Path, text: str) -> None:
    """Two sessions race on the breaker file; a half-written marker must never be read."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp%d" % os.getpid())
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _breaker_trip() -> None:
    _atomic_write(BREAKER_FILE, str(int(time.time())))


def _breaker_reset() -> None:
    try:
        BREAKER_FILE.unlink()
    except OSError:
        pass


def _token() -> str:
    env = os.environ.get("WATCHDOG_TOKEN")
    if env:
        return env.strip()
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _session_id(raw) -> str:
    sid = str(raw or "").strip()
    if not sid:
        return ""
    if SID_RE.match(sid) and not sid.startswith(("slot-", "__")):
        return sid
    return "h-" + hashlib.sha1(sid.encode("utf-8", "replace")).hexdigest()[:20]


def _quote(value: str) -> str:
    safe = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    return "".join(ch if ch in safe else "%%%02X" % ord(ch) for ch in str(value))


def _endpoint():
    rest = BASE_URL.split("://", 1)[-1]
    hostport = rest.split("/", 1)[0]
    host, _, port = hostport.partition(":")
    return host or "127.0.0.1", int(port or 80)


def _call(method: str, path: str, body=None):
    """Raw HTTP/1.1 over a socket, under ONE wall-clock deadline for the whole exchange.

    Hard rules (they keep the agent safe when the watchdog misbehaves):
    - the deadline covers connect + send + read + parse, not each syscall;
    - the response is capped at MAX_RESPONSE bytes;
    - a truncated or garbled body returns {} instead of raising;
    - a 4xx/5xx means "service up, refused" - not a transport failure;
    - the bearer token is never logged and never written anywhere.
    """
    token = _token()
    if not token or _breaker_open():
        return None
    deadline = time.monotonic() + TOTAL_S

    def remaining() -> float:
        left = deadline - time.monotonic()
        return left if left > 0 else 0.0

    data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    host, port = _endpoint()
    head = (method + " " + path + " HTTP/1.1\r\nHost: " + host + ":" + str(port) +
            "\r\nAuthorization: Bearer " + token +
            "\r\nConnection: close\r\n")
    if body is not None:
        head += "Content-Type: application/json\r\n"
    head += "Content-Length: " + str(len(data)) + "\r\n\r\n"

    try:
        sock = socket.create_connection((host, port), timeout=max(0.05, remaining()))
    except OSError:
        _breaker_trip()
        return None
    try:
        sock.settimeout(max(0.05, remaining()))
        sock.sendall(head.encode("latin-1") + data)
        chunks, size, closed = [], 0, False
        while size < MAX_RESPONSE:
            left = remaining()
            if left <= 0:
                break
            sock.settimeout(left)
            try:
                part = sock.recv(65536)
            except socket.timeout:
                break
            if not part:
                closed = True
                break
            chunks.append(part)
            size += len(part)
        raw = b"".join(chunks)
    except OSError:
        _breaker_trip()
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass

    if not raw:
        _breaker_trip()
        return None
    capped = size >= MAX_RESPONSE          # the cap is a truncation, never a completion
    head_bytes, _, payload = raw.partition(b"\r\n\r\n")
    declared = None
    for line in head_bytes.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            try:
                declared = int(line.split(b":", 1)[1].strip())
            except ValueError:
                declared = None
            break
    status_line = head_bytes.split(b"\r\n", 1)[0]
    parts = status_line.split(b" ")
    code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if code == 503:
        _breaker_trip()                # overloaded, not refusing: treat as unavailable (B2)
        return None
    if code >= 400:
        return None                    # ordinary refusal: service is up, this call is not served

    if b"chunked" in head_bytes.lower():
        out, rest, complete = [], payload, False
        # RFC 7230 token characters, for trailer field names
        TOKEN = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'*+-.^_|~`"
        while True:
            size_line, _, rest = rest.partition(b"\r\n")
            if not size_line:
                break                  # stream ended before the terminator
            try:
                chunk = int(size_line.split(b";")[0].strip(), 16)
            except ValueError:
                break                  # malformed framing
            if chunk == 0:
                # the trailer must be either empty ("\r\n") or valid token:value fields
                # each ending CRLF, then a final CRLF. Nothing may follow the terminator.
                if rest == b"\r\n":
                    complete = True
                elif rest.endswith(b"\r\n\r\n"):
                    fields = rest[:-2].split(b"\r\n")
                    if fields and fields[-1] == b"":
                        fields = fields[:-1]
                    complete = bool(fields)
                    for line in fields:
                        name, sep, _value = line.partition(b":")
                        if (not sep or not name or any(ch not in TOKEN for ch in name)):
                            complete = False
                            break
                break
            if len(rest) < chunk + 2 or rest[chunk:chunk + 2] != b"\r\n":
                break                  # truncated mid-chunk, or a missing CRLF delimiter
            out.append(rest[:chunk])
            rest = rest[chunk + 2:]
        if not complete:
            _breaker_trip()            # a partial or malformed chunked body is never a reply
            return None
        payload = b"".join(out)
    elif declared is not None:
        if capped or len(payload) < declared:
            _breaker_trip()            # the declared body did not fully arrive
            return None
    else:
        if capped or not closed:
            _breaker_trip()            # close-delimited responses need an observed close
            return None

    if not payload:
        _breaker_trip()
        return None
    try:
        body = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        _breaker_trip()                # garbled body is a failure, not an empty success
        return None
    _breaker_reset()
    return body


def _as_text(value, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = str(value)
    return value[:limit]


def _build_event(payload: dict) -> dict | None:
    event = payload.get("hook_event_name") or payload.get("event") or ""
    sid = _session_id(payload.get("session_id"))
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    tool = payload.get("tool_name")
    args = payload.get("tool_input")
    call_id = extra.get("tool_call_id") or extra.get("call_id") or ""

    if not sid:
        return None

    if event == "pre_tool_call" and tool:
        ev = {"type": "tool_call", "session_id": sid, "tool": str(tool)}
        if args is not None:
            ev["args"] = args
        if call_id:
            ev["call_id"] = str(call_id)
        return ev

    if event == "post_tool_call" and (tool or call_id):
        status_raw = str(extra.get("status") or payload.get("status") or "").lower()
        result = extra.get("result", payload.get("result"))
        failed = status_raw in {"error", "failed", "failure", "blocked", "cancelled"}
        if not status_raw and isinstance(result, str):
            failed = _looks_failed(result)
        ev = {
            "type": "tool_result",
            "session_id": sid,
            "call_id": str(call_id) if call_id else str(tool),
            "status": "error" if failed else "ok",
        }
        if tool:
            ev["tool"] = str(tool)
        if args is not None:
            ev["args"] = args
        text = _as_text(result, MAX_RESULT)
        if text:
            ev["result_text"] = text
        return ev

    if event == "post_llm_call":
        text = _as_text(payload.get("assistant_response") or extra.get("assistant_response"), MAX_TEXT)
        return {"type": "text_delta", "session_id": sid, "text": text, "kind": "answer", "final": True}

    if event == "on_session_end":
        return {"type": "session_end", "session_id": sid}

    if event == "post_api_request":
        usage = payload.get("usage") or extra.get("usage") or {}
        tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
        duration = payload.get("api_duration") or extra.get("api_duration")
        try:
            tokens_i = int(tokens)
            duration_f = float(duration)
        except (TypeError, ValueError):
            return None
        if tokens_i <= 0 or duration_f <= 0:
            return None
        return {"type": "generation", "session_id": sid, "tokens": tokens_i,
                "duration_s": round(duration_f, 3), "phase": "decode"}

    return None


def _looks_failed(text: str) -> bool:
    head = text[:400].lower()
    for marker in ("[tool_error]", '"error":', "traceback (most recent call last)", "no such file or directory"):
        if marker in head:
            return True
    return False


def _directive_context(sid: str) -> str:
    """Pull queued directives for this session only; return the text to inject."""
    data = _call("GET", "/sessions/" + _quote(sid) + "/directives")
    if not isinstance(data, dict):
        return ""
    directives = data.get("directives") or []
    messages = []
    for d in directives:
        if not isinstance(d, dict) or d.get("action") != "redirect":
            continue
        if d.get("session_id") and d["session_id"] != sid:
            continue  # never apply a directive to another session
        msg = str(d.get("message") or "").strip()
        if not msg:
            continue
        messages.append(msg)
        did = d.get("id")
        if did:
            _call("POST", "/sessions/%s/directives/%s/ack" % (
                _quote(sid), _quote(str(did))),
                {"result": "applied"})
    if not messages:
        return ""
    return "[ANTILOOP] Repetition detected in this session.\n" + "\n".join(messages) + "\n\n" + STOPLOOP_PROTOCOL


def _interrupt_block(sid: str, mode: str = "") -> str:
    """Interrupt-lite: an `interrupt` directive cannot cancel the turn from a subprocess, but a
    pre_tool_call block can refuse the next tool call and hand the model a stop instruction.
    Returns the block message, or '' when no interrupt is pending."""
    if str(mode or "").lower() != "automatic":
        return ""                          # D1: warn is advisory - no blocking path may run in it
    data = _call("GET", "/sessions/" + _quote(sid) + "/directives?status=pending")
    if not isinstance(data, dict):
        return ""
    for d in data.get("directives") or []:
        if not isinstance(d, dict) or d.get("action") != "interrupt":
            continue
        if d.get("session_id") and d["session_id"] != sid:
            continue
        did = d.get("id")
        msg = str(d.get("message") or "").strip()
        if did:
            _call("POST", "/sessions/%s/directives/%s/ack" % (
                _quote(sid), _quote(str(did))),
                {"result": "applied"})
        _break_arm(sid)
        return (msg + "\n\n" + BREAK_MESSAGE) if msg else BREAK_MESSAGE
    return ""


def _block_from_detection(resp, sid: str, tool: str, args) -> str:
    """The ingest reply already carries this event's detections and the session mode.

    `pre_llm_call` fires only once per turn, so a loop that lives inside one turn can never
    receive a pulled directive: the corrective text must ride the tool call itself. In `warn`
    and `automatic` the strong detection refuses the repeated call and hands the model the
    recovery protocol; in `observe` nothing is refused.
    """
    if not isinstance(resp, dict):
        return ""
    if str(resp.get("mode") or "").lower() != "automatic":
        return ""                          # warn is advisory: it may report, never refuse
    best = None
    for d in resp.get("detections") or []:
        try:
            conf = float(d.get("confidence") or 0)
        except (TypeError, ValueError):
            continue
        if conf >= BLOCK_MIN_CONF and (best is None or conf > best[1]):
            best = (d.get("detection_type") or "loop", conf)
    if best is None:
        return ""
    msg = ("[ANTILOOP] This exact action has already repeated (%s, confidence %.2f). "
           "The call was NOT executed.\n\n" % best) + STOPLOOP_PROTOCOL
    _break_arm(sid, tool, args, msg)
    return msg


def _native_loop_warning(text: str) -> bool:
    """Hermes' own loop guard wording, seen live: 'repeated_exact_failure_warning' plus
    'this looks like a loop' / 'Do not repeat it'."""
    head = text[:2000].lower()
    return ("repeated_exact_failure_warning" in head
            or "looks like a loop" in head
            or "this is the 3rd consecutive identical call" in head)


def _record_failure(kind: str) -> None:
    """Count a dropped event so silent degradation becomes visible (never raises)."""
    try:
        path = Path(os.environ.get("ANTILOOP_FAILURE_LOG")
                    or (ROOT / "integration" / "state" / "hook_failures.json"))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[kind] = int(data.get(kind, 0)) + 1
        _atomic_write(path, json.dumps(data, ensure_ascii=False))
    except Exception:
        pass


def main() -> None:
    try:
        raw = sys.stdin.read(STDIN_LIMIT + 1)
        if len(raw) > STDIN_LIMIT:
            _record_failure("oversize_payload")
            return
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return
        event = payload.get("hook_event_name") or ""

        if event == "pre_llm_call":
            sid = _session_id(payload.get("session_id"))
            if not sid:
                return
            text = _directive_context(sid)
            if text:
                sys.stdout.write(json.dumps({"context": text}, ensure_ascii=False))
            return

        ev = _build_event(payload)
        if not ev:
            return

        if ev.get("type") == "tool_call":
            sid = ev["session_id"]
            tool = ev.get("tool", "")
            args = ev.get("args")
            # A surgical break marker refuses only the call that repeats; a different call passes,
            # which is exactly "stop the current action, allow a rethink".
            if _break_blocks(sid, tool, args):
                _break_complete(sid, tool, args)
                rec = _break_record(sid) or {}
                msg = rec.get("message") or (BREAK_MESSAGE + "\n\n" + STOPLOOP_PROTOCOL)
                sys.stdout.write(json.dumps({"action": "block", "message": msg},
                                            ensure_ascii=False))
                return
            resp = _call("POST", "/events", ev)
            # A strong detection in warn/automatic refuses THIS already-repeated call and hands
            # the model the recovery protocol (pre_llm_call cannot reach a loop inside one turn).
            block = _block_from_detection(resp, sid, tool, args)
            if block:
                sys.stdout.write(json.dumps({"action": "block", "message": block},
                                            ensure_ascii=False))
                return
            # Zero extra HTTP cost: only look for directives when the ingest reply says some exist.
            if isinstance(resp, dict) and (resp.get("pending_directives") or 0) > 0:
                block = _interrupt_block(sid, str(resp.get("mode") or ""))
                if block:
                    _break_complete(sid, tool, args)
                    sys.stdout.write(json.dumps({"action": "block", "message": block},
                                                ensure_ascii=False))
            return

        if ev.get("type") == "tool_result":
            # Cross-check: Hermes' own loop guard already annotates repeated identical failures.
            # Use it as an early trigger instead of waiting for the engine's own threshold.
            text = str(ev.get("result_text") or "")
            sid = ev["session_id"]
            if ev.get("status") == "error" and _native_warn_due(sid) and _native_loop_warning(text):
                r = _call("POST", "/sessions/%s/intervene" % _quote(sid),
                          {"action": "redirect",
                           "message": "Hermes reported a repeated identical failure. " + STOPLOOP_PROTOCOL})
                if isinstance(r, dict):
                    _native_warn_stamp(sid)

            resp = _call("POST", "/events", ev)
            # The failed-repeat detection is computed from the RESULT, so it arrives on this
            # reply, not on the tool_call one: arm the surgical break for the next identical call.
            if ev.get("status") == "error":
                _block_from_detection(resp, sid, ev.get("tool", ""), ev.get("args"))
            return

        _call("POST", "/events", ev)
    except Exception:
        return


if __name__ == "__main__":
    main()
    sys.exit(0)
