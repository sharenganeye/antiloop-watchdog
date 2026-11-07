"""Runs the real entry point as a separate process."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.mark.skipif(os.name == "nt", reason="graceful SIGTERM semantics are POSIX-only")
def test_real_process_start_query_and_graceful_stop(tmp_path):
    port = free_port()
    cfg = tmp_path / "config" / "w.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"api": {"port": port}, "default_mode": "warn"}))
    proc = subprocess.Popen([sys.executable, "-m", "antiloop_watchdog", "--config", str(cfg),
                             "--base-dir", str(tmp_path)], cwd=str(ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                with urllib.request.urlopen(url + "/health", timeout=1) as r:
                    assert json.loads(r.read())["status"] == "ok"
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("service did not come up: " + proc.stderr.read())
        token = (tmp_path / "state" / "token").read_text().strip()
        req = urllib.request.Request(url + "/status", headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=2) as r:
            st = json.loads(r.read())
        assert st["default_mode"] == "warn" and st["adapter"]["type"] == "queue"
        pid = int((tmp_path / "state" / "watchdog.pid").read_text())
        assert pid == proc.pid
        assert oct((tmp_path / "state" / "token").stat().st_mode & 0o777) == "0o600"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("service ignored SIGTERM")
    assert proc.returncode == 0
    assert not (tmp_path / "state" / "watchdog.pid").exists()          # removed on graceful stop
    log = (tmp_path / "logs" / "watchdog.jsonl").read_text()
    assert '"service_started"' in log and '"service_stopped"' in log


def test_check_config_and_version_flags(tmp_path):
    cfg = tmp_path / "w.json"
    cfg.write_text(json.dumps({"repetition": {"threshold": 0}}))
    r = subprocess.run([sys.executable, "-m", "antiloop_watchdog", "--config", str(cfg), "--base-dir", str(tmp_path),
                        "--check-config"], cwd=str(ROOT), capture_output=True, text=True)
    assert r.returncode == 2 and "configuration error" in r.stderr
    r = subprocess.run([sys.executable, "-m", "antiloop_watchdog", "--version"], cwd=str(ROOT), capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "1.0.0"
