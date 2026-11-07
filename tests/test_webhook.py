"""Push delivery against a real local HTTP receiver (the example receiver, not Hermes)."""
import importlib.util
import threading
import time
from http.server import HTTPServer
from pathlib import Path

from antiloop_watchdog.adapters import WebhookAdapter
from conftest import events_of
from synth import call

spec = importlib.util.spec_from_file_location("receiver", Path(__file__).resolve().parents[1] / "examples" / "webhook_receiver_example.py")
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


def test_webhook_push_round_trip_scoped_to_one_session(make_engine, monkeypatch):
    receiver.Receiver.received.clear()
    srv = HTTPServer(("127.0.0.1", 0), receiver.Receiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    monkeypatch.setenv("WD_TEST_HOOK_TOKEN", "hook-secret")
    try:
        ad = WebhookAdapter(f"http://127.0.0.1:{port}/redirect", f"http://127.0.0.1:{port}/interrupt", 2.0, "WD_TEST_HOOK_TOKEN")
        eng, clock, log = make_engine("automatic", adapter=ad)
        for _ in range(2):                                  # innocent bystander session
            call(eng, clock, "B", "terminal", {"command": "make"}, "error", "boom")
        for _ in range(3):
            call(eng, clock, "A", "terminal", {"command": "make"}, "error", "boom")
        for _ in range(100):
            if events_of(log, "intervention_result", "A"):
                break
            time.sleep(0.05)
        assert events_of(log, "intervention_result", "A")[0]["intervention_result"] == "pushed"
        assert len(receiver.Receiver.received) == 1
        got = receiver.Receiver.received[0]
        assert got["path"] == "/redirect" and got["auth"] == "Bearer hook-secret"
        assert got["body"]["session_id"] == "A" and got["body"]["action"] == "redirect"
        assert got["body"]["schema"] == "antiloop.directive.v1" and got["body"]["message"].startswith("You appear")
        assert eng.get_directives("A", "delivered", True)[0]["delivery"] == "pushed"
        assert eng.get_directives("B", None, True) == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_webhook_without_interrupt_url_cannot_interrupt(make_engine):
    ad = WebhookAdapter("http://127.0.0.1:9/redirect", "", 0.5)
    assert ad.supports_redirect and not ad.supports_interrupt
    eng, clock, log = make_engine("automatic", adapter=ad)
    assert eng.status()["adapter"]["supports_interrupt"] is False
