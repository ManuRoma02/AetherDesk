import pytest
import os
from pathlib import Path
from src.client import ClientSettings, ClientState, RemoteClient
from src.host_daemon import HostDaemon

def test_client_settings_save_and_load(tmp_path):
    settings = ClientSettings(config_dir=str(tmp_path))
    assert settings.load() == ("", "", False)

    settings.save("482 910 234", "MyPassword123", remember=True)
    dev_id, pwd, remember = settings.load()
    assert dev_id == "482 910 234"
    assert pwd == "MyPassword123"
    assert remember is True

    # Test forgetting
    settings.save("", "", remember=False)
    dev_id, pwd, remember = settings.load()
    assert dev_id == ""
    assert pwd == ""
    assert remember is False

def test_host_privacy_mode(tmp_path):
    daemon = HostDaemon(config_dir=str(tmp_path), headless=True)
    res_on = daemon.set_privacy_mode(True)
    assert isinstance(res_on, bool)
    res_off = daemon.set_privacy_mode(False)
    assert isinstance(res_off, bool)

def test_client_state_reconnection():
    state = ClientState()
    assert state.is_reconnecting is False
    state.set_reconnecting(True)
    assert state.is_reconnecting is True
    state.set_reconnecting(False)
    assert state.is_reconnecting is False
