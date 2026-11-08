"""P1 regression tests — written before the fix, must fail against v1.0.1.

One test per authorised finding: A1, A2, A11, B1, B2, C1, C2, D1.
"""
import importlib.util
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "integration" / "hermes_hook.py"
PLUGIN_PATH = ROOT / "integration" / "hermes_plugin" / "antiloop_stream" / "__init__.py"


def load_hook(tmp_path, monkeypatch):
    """Load the hook module with its state files redirected into tmp_path."""
    spec = importlib.util.spec_from_file_location("p1_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "BREAKER_FILE", tmp_path / ".watchdog_down")
    monkeypatch.setattr(mod, "BREAK_DIR", tmp_path / "state")
    monkeypatch.setattr(mod, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("test-token", encoding="utf-8")
    return mod


def load_plugin():
    spec = importlib.util.spec_from_file_location("p1_plugin", PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class StubServer:
    """Scripted HTTP peer on an ephemeral port."""

    def __init__(self, behaviour, delay=0.0):
        self.behaviour = behaviour
        self.delay = delay
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        try:
            conn.recv(8192)
            time.sleep(self.delay)
            self.behaviour(conn)
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


# --------------------------------------------------------------------------- A1
def test_a1_incomplete_response_counts_as_transport_failure(tmp_path, monkeypatch):
    """Headers arrive, the body never does: that is a failure, not a healthy {}."""
    hook = load_hook(tmp_path, monkeypatch)

    def headers_then_stall(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 200\r\n\r\n")
        time.sleep(3)

    stub = StubServer(headers_then_stall)
    monkeypatch.setattr(hook, "BASE_URL", "http://127.0.0.1:%d" % stub.port)
    hook.BREAKER_FILE.unlink(missing_ok=True)

    result = hook._call("POST", "/events", {"type": "tool_call", "session_id": "a1"})
    stub.close()

    assert result is None, "an incomplete response must not be reported as a healthy reply"
    assert hook._breaker_open() is True, "an incomplete response must count toward the breaker"


# --------------------------------------------------------------------------- A2
def test_a2_oversize_stdin_is_recorded_not_silently_dropped(tmp_path):
    """A payload over the read cap must leave a visible failure record."""
    big = "x" * (5 * 1024 * 1024)      # above the 4 MiB STDIN_LIMIT
    payload = json.dumps({"hook_event_name": "pre_tool_call", "tool_name": "write_file",
                          "tool_input": {"content": big}, "session_id": "a2-big"})
    env = {"WATCHDOG_URL": "http://127.0.0.1:9", "WATCHDOG_TOKEN_FILE": str(tmp_path / "token"),
           "ANTILOOP_FAILURE_LOG": str(tmp_path / "failures.json"), "PATH": (Path(sys.executable).parent.as_posix() + ";" + "/usr/bin:/bin")}
    (tmp_path / "token").write_text("t", encoding="utf-8")
    r = subprocess.run([sys.executable, str(HOOK_PATH)], input=payload.encode(),
                       capture_output=True, timeout=60, env=env)
    assert r.returncode == 0, "the hook must still exit 0"
    assert r.stdout.strip() == b"", "nothing but JSON may reach stdout"
    log = tmp_path / "failures.json"
    assert log.exists(), "an oversize payload must be recorded, not silently dropped"
    body = json.loads(log.read_text(encoding="utf-8"))
    assert body.get("oversize_payload", 0) >= 1


# --------------------------------------------------------------------------- A11
def test_a11_client_raises_typed_error_on_empty_200(tmp_path, monkeypatch):
    from antiloop_watchdog import client as client_mod

    def empty_200(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

    stub = StubServer(empty_200)
    c = client_mod.WatchdogClient(base_url="http://127.0.0.1:%d" % stub.port, token="t")
    try:
        with pytest.raises(client_mod.WatchdogError):
            c.sessions()
    finally:
        stub.close()


def test_a11_client_raises_typed_error_on_malformed_body(tmp_path):
    from antiloop_watchdog import client as client_mod

    def garbage(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n{oops")

    stub = StubServer(garbage)
    c = client_mod.WatchdogClient(base_url="http://127.0.0.1:%d" % stub.port, token="t")
    try:
        with pytest.raises(client_mod.WatchdogError):
            c.sessions()
    finally:
        stub.close()


# --------------------------------------------------------------------------- B1
def test_b1_server_queue_and_timeouts_are_configured():
    from antiloop_watchdog.api import ApiServer

    assert getattr(ApiServer, "request_queue_size", 5) >= 64, "accept queue must survive a hook burst"
    assert getattr(ApiServer, "handler_timeout", 0) > 0, "a stalled client must not pin a thread forever"
    assert getattr(ApiServer, "max_workers", 0) > 0, "handler concurrency must be bounded"


# --------------------------------------------------------------------------- C1
def test_c1_capitalised_loop_is_truncated():
    plugin = load_plugin()
    unit = "The Same Sentence Is Repeating Again And Again Without Change "
    text = unit * 6
    det = plugin.analyse(text)
    assert det, "a capitalised loop should be detected"
    trimmed = plugin.truncate(text, det["unit"])
    assert trimmed, "a detected capitalised loop must still be truncated"
    assert len(trimmed) < len(text)
    assert "The Same Sentence" in trimmed or "the same sentence" in trimmed.lower()


# --------------------------------------------------------------------------- C2
def test_c2_unclosed_fence_does_not_blind_the_next_turn():
    import copy
    from antiloop_watchdog import config as cfgmod
    from antiloop_watchdog.repetition import RepetitionDetector

    base = copy.deepcopy(cfgmod.DEFAULTS["repetition"])
    det = RepetitionDetector(lambda: {"repetition": base})
    det.feed("```python\ndef f():\n    return 1\n", final=True)      # fence never closed
    loop = "the loop repeats the same words again and again without any change at all "
    assert det.feed(loop * 8, final=True), "the next turn must still detect a loop"


# --------------------------------------------------------------------------- D1
def test_d1_warn_mode_never_blocks(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    resp = {"mode": "warn",
            "detections": [{"detection_type": "tool_repeat_failed", "confidence": 0.95,
                            "repetitions": 4, "message": "loop"}]}
    msg = hook._block_from_detection(resp, "d1-session", "terminal", {"command": "x"})
    assert msg == "", "warn mode is advisory: it must never refuse a tool call"
