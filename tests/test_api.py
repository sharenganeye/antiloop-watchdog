"""HTTP API, authentication, validation, logging, restart persistence (req 13), client."""
import http.client
import json
import os

import pytest

from antiloop_watchdog.client import WatchdogClient, WatchdogHTTPError, WatchdogUnavailable
from antiloop_watchdog.schema import ConfigError
from antiloop_watchdog.service import Service
from synth import paragraph

TOKEN = "t" * 40


def start(tmp_path, overrides=None, token=TOKEN):
    ov = {"api": {"port": 0}}
    for k, v in (overrides or {}).items():
        ov[k] = {**ov.get(k, {}), **v} if isinstance(v, dict) else v
    return Service(config_path=str(tmp_path / "cfg" / "w.json"), base_dir=str(tmp_path), overrides=ov, token=token).start()


@pytest.fixture
def svc(tmp_path):
    s = start(tmp_path)
    yield s
    s.stop()


class Http:
    def __init__(self, svc, token=TOKEN):
        self.svc, self.token = svc, token

    def __call__(self, method, path, body=None, token="default", headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.svc.port, timeout=5)
        h = dict(headers or {})
        tok = self.token if token == "default" else token
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        txt = r.read().decode()
        conn.close()
        try:
            return r.status, json.loads(txt)
        except ValueError:
            return r.status, txt


@pytest.fixture
def http_(svc):
    return Http(svc)


def test_health_is_public_everything_else_needs_a_token(svc, http_):
    assert http_("GET", "/health", token=None)[0] == 200
    for path in ("/status", "/metrics", "/sessions", "/events", "/config"):
        assert http_("GET", path, token=None)[0] == 401
        assert http_("GET", path, token="wrong-token-wrong-token")[0] == 401
        assert http_("GET", path)[0] == 200
    assert http_("POST", "/events", {"type": "state"}, token=None)[0] == 401
    st, body = http_("GET", "/health", token=None)
    assert body["status"] == "ok" and "sessions" not in body        # no information leak


def test_alternative_token_header(svc, http_):
    assert http_("GET", "/status", token=None, headers={"X-Watchdog-Token": TOKEN})[0] == 200


def test_binds_to_loopback_and_refuses_remote_config(tmp_path, svc):
    assert svc.server.server_address[0] == "127.0.0.1"
    with pytest.raises(ConfigError):
        Service(config_path=str(tmp_path / "c2.json"), base_dir=str(tmp_path),
                overrides={"api": {"host": "0.0.0.0", "port": 0}}, token=TOKEN)


def test_foreign_host_header_is_rejected(svc, http_):
    st, body = http_("GET", "/health", token=None, headers={"Host": "evil.example.com"})
    assert st == 403 and body["error"]["code"] == "forbidden_host"
    assert http_("GET", "/health", token=None, headers={"Host": f"localhost:{svc.port}"})[0] == 200


def test_unknown_routes_and_methods(http_):
    assert http_("GET", "/nope")[0] == 404
    assert http_("DELETE", "/sessions/x")[0] == 405
    assert http_("POST", "/status", {})[0] == 405
    assert http_("GET", "/sessions/x/mode")[0] == 405


def test_input_validation(http_):
    assert http_("POST", "/events", {"type": "bogus"})[0] == 400
    assert http_("POST", "/events", {"type": "text_delta", "session_id": "bad id!"})[0] == 400
    assert http_("POST", "/events", {"type": "generation", "session_id": "s", "tokens": -1, "duration_s": 1})[0] == 400
    assert http_("POST", "/events", {"type": "generation", "session_id": "s", "tokens": 5, "duration_s": "x"})[0] == 400
    assert http_("POST", "/events", {"type": "tool_call", "session_id": "s"})[0] == 400
    assert http_("POST", "/events", {"type": "state", "session_id": "slot-1"})[0] == 400
    assert http_("POST", "/events", ["not", "an", "object"])[0] == 400
    assert http_("POST", "/events", {"events": []})[0] == 400
    assert http_("POST", "/events", raw=b"{not json", headers={"Content-Type": "application/json"})[0] == 400
    assert http_("POST", "/events", raw=b"{}", headers={"Content-Type": "text/plain"})[0] == 415
    st, body = http_("POST", "/events", {"type": "bogus"})
    assert body["error"]["code"] == "invalid_event_type"
    http_("POST", "/events", {"type": "state", "session_id": "s1"})
    assert http_("POST", "/sessions/s1/mode", {"mode": "turbo"})[0] == 400
    assert http_("POST", "/sessions/s1/mode", {})[0] == 400
    assert http_("POST", "/sessions/bad%20id/mode", {"mode": "warn"})[0] == 400
    assert http_("POST", "/sessions/nope/reset", {})[0] == 404
    assert http_("POST", "/sessions/s1/intervene", {"action": "restart-llama"})[0] == 400
    assert http_("POST", "/sessions/s1/intervene", {"action": "redirect", "extra": 1})[0] == 400
    assert http_("GET", "/events?since_id=abc")[0] == 400
    assert http_("GET", "/events?limit=0")[0] == 400
    assert http_("GET", "/sessions/s1/directives?status=weird")[0] == 400


def test_oversized_body_is_rejected(tmp_path):
    s = start(tmp_path, {"api": {"max_body_bytes": 2048}})
    try:
        st, body = Http(s)("POST", "/events", raw=b"x" * 5000, headers={"Content-Type": "application/json"})
        assert st == 413 and body["error"]["code"] == "payload_too_large"
    finally:
        s.stop()


def test_end_to_end_loop_flow_over_http(svc, http_):
    st, r = http_("POST", "/events", {"type": "state", "session_id": "hermes-1"})
    assert st == 200 and r["mode"] == "observe"
    st, r = http_("POST", "/sessions/hermes-1/mode", {"mode": "warn"})
    assert st == 200 and r["mode"] == "warn"
    for _ in range(3):
        st, r = http_("POST", "/events", {"type": "tool_call", "session_id": "hermes-1", "tool": "terminal",
                                           "args": {"command": "make"}, "status": "error", "result_text": "boom"})
    assert r["detections"] and r["detections"][0]["detection_type"] == "tool_repeat_failed"
    assert r["pending_directives"] == 1
    st, d = http_("GET", "/sessions/hermes-1/directives")
    did = d["directives"][0]["id"]
    assert d["directives"][0]["message"].startswith("You appear to be stuck in a repetitive loop")
    st, a = http_("POST", f"/sessions/hermes-1/directives/{did}/ack", {"result": "applied"})
    assert st == 200 and a["directive"]["status"] == "acked"
    st, ev = http_("GET", "/events?session_id=hermes-1&event_type=detection")
    assert st == 200 and ev["events"] and ev["next_since_id"] == ev["events"][-1]["id"]
    st, ev2 = http_("GET", f"/events?since_id={ev['next_since_id']}&session_id=hermes-1&event_type=detection")
    assert ev2["events"] == []
    assert http_("POST", "/sessions/hermes-1/reset", {})[0] == 200
    st, s = http_("GET", "/sessions/hermes-1")
    assert st == 200 and s["directives"][0]["status"] == "acked"


def test_batch_ingest_reports_rejections(http_):
    st, r = http_("POST", "/events", {"events": [
        {"type": "state", "session_id": "a"}, {"type": "nope"}, {"type": "state", "session_id": "b"}]})
    assert st == 200 and r["accepted"] == 2 and r["rejected"][0]["index"] == 1


def test_status_metrics_sessions_shapes(http_):
    http_("POST", "/events", {"type": "generation", "session_id": "m1", "tokens": 200, "duration_s": 4.0})
    st, s = http_("GET", "/status")
    for k in ("service", "version", "uptime_s", "default_mode", "adapter", "sessions", "counters", "resource"):
        assert k in s
    assert s["adapter"]["verified_against_hermes"] is False
    st, m = http_("GET", "/metrics")
    assert m["sessions"][0]["session_id"] == "m1" and "rss_mb" in m["resource"]
    st, txt = http_("GET", "/metrics?format=prometheus")
    assert st == 200 and "antiloop_sessions" in txt
    st, ss = http_("GET", "/sessions")
    assert ss["sessions"][0]["speed"]["accepted_blocks"] == 1


def test_config_get_and_post(http_, svc):
    st, c = http_("GET", "/config")
    assert c["config"]["repetition"]["threshold"] == 3 and "repetition.threshold" in c["mutable_keys"]
    assert "api.port" not in c["mutable_keys"]
    st, c = http_("POST", "/config", {"repetition": {"threshold": 4}, "default_mode": "warn"})
    assert st == 200 and c["config"]["repetition"]["threshold"] == 4 and svc.cm.data["default_mode"] == "warn"
    assert http_("POST", "/config", {"nonsense": 1})[0] == 400
    assert http_("POST", "/config", {"repetition": {"threshold": 1}})[0] == 400
    assert http_("POST", "/config", {"repetition": {"threshold": True}})[0] == 400
    assert http_("POST", "/config", {"speed": {"warning_tps": 90, "critical_tps": 80}})[0] == 400
    assert http_("POST", "/config", {"repetition": {"ignore_patterns": ["("]}})[0] == 400
    st, e = http_("POST", "/config", {"api": {"host": "0.0.0.0"}})
    assert st == 400 and e["error"]["code"] == "config_key_immutable"
    assert http_("POST", "/config", {"adapter": {"redirect_url": "http://127.0.0.1:1/x"}})[0] == 400
    assert http_("POST", "/config", {})[0] == 400
    assert svc.cm.data["speed"]["warning_tps"] == 70.0                    # rejected patches change nothing


def test_automatic_mode_request_for_anonymous_session_is_refused(http_):
    http_("POST", "/events", {"type": "state"})
    st, e = http_("POST", "/sessions/__unattributed__/mode", {"mode": "automatic"})
    assert st == 409 and e["error"]["code"] == "automatic_not_allowed"


def test_structured_json_log_and_human_log(svc, http_):
    for _ in range(3):
        http_("POST", "/events", {"type": "tool_call", "session_id": "log1", "tool": "terminal",
                                   "args": {"command": "echo hunter2-secret"}, "status": "error",
                                   "result_text": "hunter2-secret failed"})
    svc.event_log.close()
    jl = [json.loads(l) for l in (svc.log_dir / "watchdog.jsonl").read_text().splitlines()]
    det = [e for e in jl if e["event_type"] == "detection"][0]
    for k in ("ts", "session_id", "event_type", "tps", "detection_type", "confidence", "sequence_fingerprint",
              "tool_fingerprint", "repetitions", "intervention_action", "intervention_result", "wasted_tokens_est"):
        assert k in det
    assert det["session_id"] == "log1" and det["tool_fingerprint"]
    human = (svc.log_dir / "watchdog.log").read_text()
    assert "detection=tool_repeat_failed" in human and "session=log1" in human
    assert "hunter2-secret" not in human and "hunter2-secret" not in json.dumps(jl)


def test_token_file_is_created_with_private_permissions(tmp_path):
    s = Service(config_path=str(tmp_path / "c.json"), base_dir=str(tmp_path), overrides={"api": {"port": 0}})
    tok = s.token_path.read_text().strip()
    assert len(tok) >= 32 and tok == s.token
    if os.name != "nt":
        assert oct(s.token_path.stat().st_mode & 0o777) == "0o600"
    s.event_log.close()


def test_client_roundtrip_and_unavailable(svc):
    wd = WatchdogClient(f"http://127.0.0.1:{svc.port}", TOKEN)
    assert wd.health()["status"] == "ok"
    wd.set_mode("c1", "warn")
    for _ in range(3):
        wd.tool_call("c1", "terminal", {"command": "make"}, status="error", result_text="boom")
    ds = wd.directives("c1")
    assert len(ds) == 1 and wd.ack("c1", ds[0]["id"])["status"] == "acked"
    assert wd.session("c1")["mode"] == "warn" and wd.events(session_id="c1")["events"]
    with pytest.raises(WatchdogHTTPError) as ei:
        wd.set_mode("c1", "bogus")
    assert ei.value.status == 400
    with pytest.raises(WatchdogHTTPError):
        WatchdogClient(f"http://127.0.0.1:{svc.port}", "wrong" * 5).status()
    dead = WatchdogClient("http://127.0.0.1:1", TOKEN, timeout=0.5)
    with pytest.raises(WatchdogUnavailable):
        dead.health()


def test_text_loop_over_http_with_session_in_automatic_mode_uses_pull_queue(svc, http_):
    http_("POST", "/events", {"type": "state", "session_id": "txt"})
    http_("POST", "/sessions/txt/mode", {"mode": "automatic"})
    text = " ".join([paragraph(7)] * 6)
    for i in range(0, len(text), 120):
        http_("POST", "/events", {"type": "text_delta", "session_id": "txt", "text": text[i:i + 120], "kind": "reasoning"})
    http_("POST", "/events", {"type": "text_delta", "session_id": "txt", "text": "", "final": True})
    st, d = http_("GET", "/sessions/txt/directives?peek=1")
    assert d["directives"] and d["directives"][0]["action"] == "redirect"


def test_manual_intervention_endpoint(svc, http_):
    http_("POST", "/events", {"type": "state", "session_id": "mi"})
    assert http_("POST", "/sessions/mi/intervene", {"action": "redirect"})[0] == 409      # observe
    http_("POST", "/sessions/mi/mode", {"mode": "warn"})
    st, r = http_("POST", "/sessions/mi/intervene", {"action": "redirect", "message": "Use another approach."})
    assert st == 200 and r["directive"]["message"] == "Use another approach."
