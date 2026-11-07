"""Requirement 13: service restart and configuration persistence."""
import json

from antiloop_watchdog.service import Service
from antiloop_watchdog.client import WatchdogClient


def boot(tmp_path):
    return Service(config_path=str(tmp_path / "config" / "w.json"), base_dir=str(tmp_path),
                   overrides=None if (tmp_path / "config" / "w.json").exists() else {"api": {"port": 0}}).start()


def test_config_session_modes_and_token_survive_restart(tmp_path):
    s1 = boot(tmp_path)
    wd = WatchdogClient(f"http://127.0.0.1:{s1.port}", s1.token)
    wd.send({"type": "state", "session_id": "keep-me"})
    wd.set_mode("keep-me", "warn")
    wd.patch_config({"repetition": {"threshold": 4}, "default_mode": "warn", "intervention": {"cooldown_s": 12}})
    token = s1.token
    s1.stop()
    assert not s1.pidfile.exists()

    s2 = boot(tmp_path)
    try:
        assert s2.token == token
        assert s2.cm.data["repetition"]["threshold"] == 4 and s2.cm.data["default_mode"] == "warn"
        assert s2.cm.data["intervention"]["cooldown_s"] == 12
        wd2 = WatchdogClient(f"http://127.0.0.1:{s2.port}", s2.token)
        wd2.send({"type": "state", "session_id": "keep-me"})
        wd2.send({"type": "state", "session_id": "fresh"})
        modes = {s["session_id"]: s["mode_override"] for s in wd2.sessions()}
        assert modes["keep-me"] == "warn" and modes["fresh"] is None
        assert wd2.config()["config"]["repetition"]["threshold"] == 4
    finally:
        s2.stop()


def test_config_file_is_valid_json_and_written_atomically(tmp_path):
    s = boot(tmp_path)
    WatchdogClient(f"http://127.0.0.1:{s.port}", s.token).patch_config({"speed": {"warning_tps": 72}})
    s.stop()
    data = json.loads((tmp_path / "config" / "w.json").read_text())
    assert data["speed"]["warning_tps"] == 72
    assert not list((tmp_path / "config").glob("*.tmp"))


def test_unknown_keys_in_file_warn_but_do_not_prevent_start(tmp_path):
    cfg = tmp_path / "config" / "w.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"api": {"port": 0}, "mystery": {"x": 1}, "speed": {"warning_tps": 71}}))
    s = Service(config_path=str(cfg), base_dir=str(tmp_path)).start()
    try:
        assert any("mystery" in w for w in s.cm.warnings)
        assert s.cm.data["speed"]["warning_tps"] == 71
    finally:
        s.stop()


def test_invalid_config_file_fails_fast(tmp_path):
    import pytest
    from antiloop_watchdog.schema import ConfigError
    cfg = tmp_path / "w.json"
    cfg.write_text(json.dumps({"repetition": {"threshold": 0}}))
    with pytest.raises(ConfigError):
        Service(config_path=str(cfg), base_dir=str(tmp_path), token="x" * 20)
    cfg.write_text("{broken")
    with pytest.raises(ConfigError):
        Service(config_path=str(cfg), base_dir=str(tmp_path), token="x" * 20)
