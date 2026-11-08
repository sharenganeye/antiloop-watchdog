"""P1 follow-up tests requested by supervision review.

Covers: A1 variants, A2 no-prefix-processing, B1/B2 overload behaviour,
C2 cross-turn detection preservation, D1 every blocking path.
"""
import importlib.util
import json
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "integration" / "hermes_hook.py"


def load_hook(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("p1b_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "BREAKER_FILE", tmp_path / ".down")
    monkeypatch.setattr(mod, "BREAK_DIR", tmp_path / "state")
    monkeypatch.setattr(mod, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok", encoding="utf-8")
    return mod


class Peer:
    def __init__(self, behaviour):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        threading.Thread(target=self._serve, args=(behaviour,), daemon=True).start()

    def _serve(self, behaviour):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.hits += 1
            try:
                conn.recv(65536)
                behaviour(conn)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------ A1 variants
def test_a1_short_body_trips_breaker(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)

    def short(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{\"a\": 1}")

    peer = Peer(short)
    monkeypatch.setattr(hook, "BASE_URL", "http://127.0.0.1:%d" % peer.port)
    assert hook._call("POST", "/events", {}) is None
    assert hook._breaker_open() is True
    peer.close()


def test_a1_garbled_body_trips_breaker(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)

    def garbled(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n{oops")

    peer = Peer(garbled)
    monkeypatch.setattr(hook, "BASE_URL", "http://127.0.0.1:%d" % peer.port)
    hook.BREAKER_FILE.unlink(missing_ok=True)
    assert hook._call("POST", "/events", {}) is None
    assert hook._breaker_open() is True
    peer.close()


def test_a1_complete_response_resets_breaker(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)

    def ok(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")

    peer = Peer(ok)
    monkeypatch.setattr(hook, "BASE_URL", "http://127.0.0.1:%d" % peer.port)

    hook._breaker_trip()
    assert hook._breaker_open() is True
    assert hook._call("POST", "/events", {}) is None, "while open, no request must be made"
    assert peer.hits == 0

    # age the marker past the 45 s window: the next complete response must clear it
    import os
    os.utime(hook.BREAKER_FILE, (time.time() - 120, time.time() - 120))
    assert hook._breaker_open() is False
    assert hook._call("POST", "/events", {}) == {}
    assert hook._breaker_open() is False and not hook.BREAKER_FILE.exists(),         "a complete response is the only thing that clears the breaker"
    peer.close()


# ------------------------------------------------------------------ A2 no prefix
def test_a2_oversize_payload_is_not_processed_at_all(tmp_path):
    """A payload above the ceiling must not be parsed, and nothing may reach the watchdog."""
    peer = Peer(lambda conn: conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"))
    token = tmp_path / "token"
    token.write_text("tok", encoding="utf-8")
    log = tmp_path / "failures.json"
    payload = json.dumps({"hook_event_name": "pre_tool_call", "tool_name": "write_file",
                          "tool_input": {"content": "x" * (5 * 1024 * 1024)},
                          "session_id": "a2"})
    import os
    import subprocess
    import sys
    env = dict(os.environ)
    env.update({"WATCHDOG_URL": "http://127.0.0.1:%d" % peer.port,
                "WATCHDOG_TOKEN_FILE": str(token), "ANTILOOP_FAILURE_LOG": str(log)})
    r = subprocess.run([sys.executable, str(HOOK_PATH)], input=payload.encode(),
                       capture_output=True, timeout=90, env=env)
    peer.close()
    assert r.returncode == 0
    assert peer.hits == 0, "an oversize payload must not be partially processed"
    assert json.loads(log.read_text(encoding="utf-8")).get("oversize_payload") == 1
    assert log.stat().st_size < 4096, "the failure counter must stay bounded"


# ------------------------------------------------------------------ B1 / B2 overload
def test_b1_overload_returns_503_and_stays_bounded(tmp_path, monkeypatch):
    from antiloop_watchdog import api as api_mod
    from antiloop_watchdog.service import Service

    monkeypatch.setattr(api_mod.ApiServer, "max_workers", 2)
    svc = Service(config_path=str(tmp_path / "cfg" / "w.json"), base_dir=str(tmp_path),
                  overrides={"api": {"port": 0}}, token="t" * 40).start()
    codes = []
    lock = threading.Lock()

    def hit():
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/health" % svc.port)
            with urllib.request.urlopen(req, timeout=5) as r:
                code = r.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        except Exception:
            code = 0
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=hit) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    svc.stop()
    assert set(codes) <= {200, 503}, codes
    assert codes.count(503) + codes.count(200) == 12


# ------------------------------------------------------------------ C2
def _detector():
    import copy
    from antiloop_watchdog import config as cfgmod
    from antiloop_watchdog.repetition import RepetitionDetector
    base = copy.deepcopy(cfgmod.DEFAULTS["repetition"])
    return RepetitionDetector(lambda: {"repetition": base})


SENTENCE = "The deployment pipeline fails at the exact same stage every single time we run it. "


def test_c2_fence_does_not_blind_the_next_turn():
    d = _detector()
    d.feed("```python\ndef f():\n    return 1\n", final=True)
    assert d.feed(SENTENCE * 6, final=True), "an unclosed fence must not blind the next turn"


def test_c2_cross_turn_repetition_is_still_detected():
    d = _detector()
    d.feed(SENTENCE * 2, final=True)
    assert d.feed(SENTENCE * 3, final=True), "repetition spanning the turn boundary must still fire"


def test_c2_closed_fence_leaves_no_state():
    d = _detector()
    d.feed("```python\nx = 1\n```\n", final=True)
    assert d.in_fence is False


# ------------------------------------------------------------------ D1 all paths
def test_d1_interrupt_directive_cannot_block_in_warn(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    monkeypatch.setattr(hook, "_call", lambda *a, **k: {"directives": [
        {"id": "d1", "action": "interrupt", "message": "stop", "session_id": "s"}]})
    assert hook._interrupt_block("s", "warn") == "", "warn must never block, even with an interrupt queued"
    assert hook._interrupt_block("s", "automatic") != "", "automatic may still block"
