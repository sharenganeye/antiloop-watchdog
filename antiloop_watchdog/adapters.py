"""Hermes adapter interface.

IMPORTANT: no Hermes API was verified while building this project. The adapters below are
generic and make no claim about what Hermes supports:

  QueueAdapter    (default) pull model. The watchdog stores a directive; a Hermes-side
                  shim fetches it with GET /sessions/{id}/directives and applies it.
  WebhookAdapter  push model. POSTs a JSON directive to a loopback URL that *you* configure
                  and that *you* must implement on the Hermes side.
  NullAdapter     no delivery path; directives remain queued and are visible via the API.

The adapter interface is deliberately session-scoped: there is no method that can stop the
gateway, restart llama.cpp or touch another session. Every call carries exactly one
session id.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .schema import DIRECTIVE_SCHEMA, ConfigError
from .util import iso

import time

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


@dataclass
class AdapterResult:
    ok: bool
    status: str
    detail: str = ""


class HermesAdapter:
    name = "base"
    supports_redirect = False
    supports_interrupt = False
    push = False          # True when the adapter delivers by itself (not via pull)
    blocking = False      # True when calls may block on the network (run off-thread)

    def info(self) -> dict:
        return {"type": self.name, "supports_redirect": self.supports_redirect,
                "supports_interrupt": self.supports_interrupt, "push": self.push,
                "verified_against_hermes": False}

    def redirect(self, session_id: str, directive: dict) -> AdapterResult:
        return AdapterResult(False, "adapter_unavailable", "no delivery path configured")

    def interrupt(self, session_id: str, directive: dict) -> AdapterResult:
        return AdapterResult(False, "adapter_unavailable", "no delivery path configured")


class NullAdapter(HermesAdapter):
    name = "null"


class QueueAdapter(HermesAdapter):
    name = "queue"
    supports_redirect = True
    supports_interrupt = True

    def redirect(self, session_id: str, directive: dict) -> AdapterResult:
        return AdapterResult(True, "queued_for_pull")

    def interrupt(self, session_id: str, directive: dict) -> AdapterResult:
        return AdapterResult(True, "queued_for_pull")


class WebhookAdapter(HermesAdapter):
    name = "webhook"
    push = True
    blocking = True

    def __init__(self, redirect_url: str, interrupt_url: str, timeout_s: float, auth_env: str = ""):
        self.redirect_url = self._check(redirect_url)
        self.interrupt_url = self._check(interrupt_url)
        self.timeout_s = timeout_s
        self.auth_env = auth_env
        self.supports_redirect = bool(self.redirect_url)
        self.supports_interrupt = bool(self.interrupt_url)

    @staticmethod
    def _check(url: str) -> str:
        if not url:
            return ""
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "http" or (parts.hostname or "") not in LOOPBACK_HOSTS:
            raise ConfigError(f"webhook URL must be http:// on a loopback host, got {url!r}")
        return url

    def _post(self, url: str, session_id: str, directive: dict) -> AdapterResult:
        payload = {"schema": DIRECTIVE_SCHEMA, "session_id": session_id, "directive_id": directive["id"],
                   "action": directive["action"], "message": directive["message"],
                   "reason": directive.get("reason", {}), "sent_at": iso(time.time())}
        headers = {"Content-Type": "application/json", "User-Agent": "antiloop-watchdog"}
        token = os.environ.get(self.auth_env, "") if self.auth_env else ""
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                if 200 <= resp.status < 300:
                    return AdapterResult(True, "pushed", f"http {resp.status}")
                return AdapterResult(False, "push_failed", f"http {resp.status}")
        except urllib.error.HTTPError as exc:
            return AdapterResult(False, "push_failed", f"http {exc.code}")
        except Exception as exc:  # connection refused, timeout, ...
            return AdapterResult(False, "push_failed", f"{type(exc).__name__}: {str(exc)[:150]}")

    def redirect(self, session_id: str, directive: dict) -> AdapterResult:
        if not self.redirect_url:
            return AdapterResult(False, "adapter_no_redirect", "adapter.redirect_url not configured")
        return self._post(self.redirect_url, session_id, directive)

    def interrupt(self, session_id: str, directive: dict) -> AdapterResult:
        if not self.interrupt_url:
            return AdapterResult(False, "adapter_no_interrupt", "adapter.interrupt_url not configured")
        return self._post(self.interrupt_url, session_id, directive)


def build_adapter(cfg: dict):
    """Returns (adapter, warning_or_None). Falls back to the null adapter on bad configuration."""
    ac = cfg["adapter"]
    try:
        if ac["type"] == "queue":
            return QueueAdapter(), None
        if ac["type"] == "webhook":
            return WebhookAdapter(ac["redirect_url"], ac["interrupt_url"], ac["timeout_s"], ac["auth_env"]), None
        return NullAdapter(), None
    except ConfigError as exc:
        return NullAdapter(), f"adapter disabled: {exc.message}"
