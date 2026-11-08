"""P1 sixth follow-up: strict chunked trailer parsing (the last named A1 correction)."""
import socket
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "integration" / "hermes_hook.py"
BODY = b'{"ok": true}'          # 12 bytes == 0x0c


def load_hook(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("p1f_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "BREAKER_FILE", tmp_path / ".down")
    monkeypatch.setattr(mod, "BREAK_DIR", tmp_path / "state")
    monkeypatch.setattr(mod, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok", encoding="utf-8")
    return mod


class Peer:
    def __init__(self, raw):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, args=(raw,), daemon=True).start()

    def _serve(self, raw):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                conn.recv(65536)
                conn.sendall(raw)
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


def run(hook, monkeypatch, raw):
    peer = Peer(raw)
    monkeypatch.setattr(hook, "BASE_URL", "http://127.0.0.1:%d" % peer.port)
    hook.BREAKER_FILE.unlink(missing_ok=True)
    out = hook._call("POST", "/events", {})
    breaker = hook._breaker_open()
    peer.close()
    return out, breaker


HEAD = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nc\r\n" + BODY + b"\r\n"


# ------------------------------------------------------------------ accepted
def test_trailer_empty_is_accepted(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    out, breaker = run(hook, monkeypatch, HEAD + b"0\r\n\r\n")
    assert out == {"ok": True}
    assert breaker is False


def test_trailer_with_valid_fields_is_accepted(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    out, breaker = run(hook, monkeypatch, HEAD + b"0\r\nX-Checksum: abc123\r\nX-Note: ok\r\n\r\n")
    assert out == {"ok": True}
    assert breaker is False


def test_two_data_chunks_with_empty_trailer_are_accepted(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    raw = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
           b"6\r\n{\"ok\":\r\n6\r\n true}\r\n0\r\n\r\n")
    out, breaker = run(hook, monkeypatch, raw)
    assert out == {"ok": True}
    assert breaker is False


# ------------------------------------------------------------------ rejected
def test_trailer_line_without_a_colon_is_rejected(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    out, breaker = run(hook, monkeypatch, HEAD + b"0\r\nnot-a-header\r\n\r\n")
    assert out is None
    assert breaker is True


def test_unexpected_bytes_after_the_terminator_are_rejected(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    out, breaker = run(hook, monkeypatch, HEAD + b"0\r\n\r\nJUNK")
    assert out is None
    assert breaker is True


def test_trailer_without_its_final_crlf_is_rejected(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    out, breaker = run(hook, monkeypatch, HEAD + b"0\r\nX-Sum: 1\r\n")
    assert out is None
    assert breaker is True


def test_trailer_field_name_with_a_space_is_rejected(tmp_path, monkeypatch):
    """A colon is not enough: the field name must be an RFC 7230 token."""
    hook = load_hook(tmp_path, monkeypatch)
    out, breaker = run(hook, monkeypatch, HEAD + b"0\r\nBad Header: value\r\n\r\n")
    assert out is None, "a field name containing a space is not a valid HTTP token"
    assert breaker is True


def test_trailer_field_name_with_a_non_ascii_byte_is_rejected(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    out, breaker = run(hook, monkeypatch, HEAD + b"0\r\nX-N\xc3\xa4me: value\r\n\r\n")
    assert out is None
    assert breaker is True


def test_trailer_field_name_with_a_control_byte_is_rejected(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    out, breaker = run(hook, monkeypatch, HEAD + b"0\r\nX-\x07bad: value\r\n\r\n")
    assert out is None
    assert breaker is True


def test_all_token_characters_are_accepted_in_a_field_name(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    name = b"X-A!#$%&'*+-.^_|~`Z"
    out, breaker = run(hook, monkeypatch, HEAD + b"0\r\n" + name + b": v\r\n\r\n")
    assert out == {"ok": True}
    assert breaker is False
