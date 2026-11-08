"""Stdlib-only client for Hermes-side code.  See HERMES_WATCHDOG_INTEGRATION.md.

    from antiloop_watchdog.client import WatchdogClient
    wd = WatchdogClient.from_files("<project>/state")        # reads state/token
    wd.text_delta("hermes-sess-1", "thinking text...", kind="reasoning")
    wd.tool_call("hermes-sess-1", "terminal", {"command": "pytest -x"}, status="error", result_text="...")
    for d in wd.directives("hermes-sess-1"):                  # pull model
        apply_to_session(d["message"]); wd.ack("hermes-sess-1", d["id"], "applied")
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


class WatchdogError(Exception):
    """Base class for every typed watchdog failure."""


class WatchdogUnavailable(WatchdogError):
    """The watchdog could not be reached, or answered with something unusable."""


class WatchdogHTTPError(WatchdogError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class WatchdogClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8791", token: Optional[str] = None, timeout: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("WATCHDOG_TOKEN", "")
        self.timeout = timeout

    @classmethod
    def from_files(cls, state_dir, base_url: str = "http://127.0.0.1:8791", timeout: float = 2.0) -> "WatchdogClient":
        token = (Path(state_dir) / "token").read_text(encoding="utf-8").strip()
        return cls(base_url, token, timeout)

    def _call(self, method: str, path: str, body: Any = None, auth: bool = True) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                try:
                    raw = resp.read().decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise WatchdogUnavailable("watchdog returned undecodable bytes: %s" % exc) from None
                if not raw.strip():
                    raise WatchdogUnavailable("watchdog returned an empty 200 response")
                try:
                    return json.loads(raw)
                except ValueError as exc:
                    raise WatchdogUnavailable("watchdog returned a malformed body: %s" % exc) from None
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read().decode("utf-8")).get("error", {})
            except Exception:
                err = {}
            raise WatchdogHTTPError(exc.code, err.get("code", "http_error"), err.get("message", str(exc)))
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise WatchdogUnavailable(str(exc))

    @staticmethod
    def _sid(session_id: str) -> str:
        return urllib.parse.quote(session_id, safe="")

    # --- service
    def health(self) -> dict: return self._call("GET", "/health", auth=False)
    def status(self) -> dict: return self._call("GET", "/status")
    def metrics(self) -> dict: return self._call("GET", "/metrics")
    def config(self) -> dict: return self._call("GET", "/config")
    def patch_config(self, patch: dict) -> dict: return self._call("POST", "/config", patch)

    # --- sessions
    def sessions(self) -> list: return self._call("GET", "/sessions")["sessions"]
    def session(self, session_id: str) -> dict: return self._call("GET", f"/sessions/{self._sid(session_id)}")
    def set_mode(self, session_id: str, mode: str) -> dict:
        return self._call("POST", f"/sessions/{self._sid(session_id)}/mode", {"mode": mode})
    def reset(self, session_id: str) -> dict: return self._call("POST", f"/sessions/{self._sid(session_id)}/reset", {})
    def intervene(self, session_id: str, action: str = "redirect", message: Optional[str] = None) -> dict:
        body = {"action": action}
        if message:
            body["message"] = message
        return self._call("POST", f"/sessions/{self._sid(session_id)}/intervene", body)

    # --- events
    def events(self, since_id: int = 0, session_id: Optional[str] = None, event_type: Optional[str] = None, limit: int = 100) -> dict:
        q = {"since_id": since_id, "limit": limit}
        if session_id: q["session_id"] = session_id
        if event_type: q["event_type"] = event_type
        return self._call("GET", "/events?" + urllib.parse.urlencode(q))

    def send(self, event: dict) -> dict: return self._call("POST", "/events", event)
    def send_batch(self, events: list) -> dict: return self._call("POST", "/events", {"events": events})

    def text_delta(self, session_id: str, text: str, kind: str = "answer", final: bool = False, tokens: Optional[int] = None) -> dict:
        ev = {"type": "text_delta", "session_id": session_id, "text": text, "kind": kind, "final": final}
        if tokens is not None: ev["tokens"] = tokens
        return self.send(ev)

    def generation(self, session_id: str, tokens: int, duration_s: float, phase: str = "decode") -> dict:
        return self.send({"type": "generation", "session_id": session_id, "tokens": tokens,
                          "duration_s": duration_s, "phase": phase})

    def tool_call(self, session_id: str, tool: str, args: Any = None, call_id: Optional[str] = None,
                  status: str = "unknown", result_text: Optional[str] = None, tokens: Optional[int] = None) -> dict:
        ev = {"type": "tool_call", "session_id": session_id, "tool": tool, "args": args, "status": status}
        if call_id: ev["call_id"] = call_id
        if result_text is not None: ev["result_text"] = result_text
        if tokens is not None: ev["tokens"] = tokens
        return self.send(ev)

    def tool_result(self, session_id: str, call_id: str, status: str, result_text: Optional[str] = None,
                    tool: Optional[str] = None) -> dict:
        ev = {"type": "tool_result", "session_id": session_id, "call_id": call_id, "status": status}
        if result_text is not None: ev["result_text"] = result_text
        if tool: ev["tool"] = tool
        return self.send(ev)

    def state_changed(self, session_id: str) -> dict:
        return self.send({"type": "state", "session_id": session_id, "changed": True})

    def session_end(self, session_id: str) -> dict:
        return self.send({"type": "session_end", "session_id": session_id})

    # --- directives (pull model)
    def directives(self, session_id: str, status: Optional[str] = None, peek: bool = False) -> list:
        q = {}
        if status: q["status"] = status
        if peek: q["peek"] = "1"
        qs = ("?" + urllib.parse.urlencode(q)) if q else ""
        return self._call("GET", f"/sessions/{self._sid(session_id)}/directives{qs}")["directives"]

    def ack(self, session_id: str, directive_id: str, result: str = "applied") -> dict:
        return self._call("POST", f"/sessions/{self._sid(session_id)}/directives/{urllib.parse.quote(directive_id, safe='')}/ack",
                          {"result": result})["directive"]
