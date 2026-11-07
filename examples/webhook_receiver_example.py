#!/usr/bin/env python3
"""Minimal loopback receiver for the watchdog's *webhook* adapter (push model).

This is a REFERENCE for what Hermes-side code must implement; it only prints directives. A real
receiver would inject `message` into the matching Hermes session (redirect) or stop that session's
current turn (interrupt) using whatever session-control interface Hermes really offers.

    python examples/webhook_receiver_example.py 8795
    # config/watchdog.config.json -> "adapter": {"type": "webhook",
    #     "redirect_url": "http://127.0.0.1:8795/redirect", "interrupt_url": "http://127.0.0.1:8795/interrupt"}
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Receiver(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        Receiver.received.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        print(f"{self.path}: session={body['session_id']} action={body['action']} directive={body['directive_id']}")
        # >>> apply to the Hermes session named body["session_id"] ONLY <<<
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8795
    HTTPServer(("127.0.0.1", port), Receiver).serve_forever()
