import pytest
from src.host_daemon import HostDaemon

def test_daemon_sleep_prevention(tmp_path):
    daemon = HostDaemon(config_dir=str(tmp_path), headless=True)
    res = daemon.prevent_sleep(True)
    assert res is True
    daemon.prevent_sleep(False)

def test_daemon_status(tmp_path):
    daemon = HostDaemon(config_dir=str(tmp_path), headless=True)
    status = daemon.get_status()
    assert "device_id" in status
    assert "running" in status
    assert "connected_clients" in status

def test_daemon_auth_and_input_handling(tmp_path):
    daemon = HostDaemon(config_dir=str(tmp_path), headless=True)
    daemon.security.set_password("Secret123")
    
    # Test valid authentication challenge
    session_state = {}
    salt = b"test_salt_123456"
    resp_bytes = daemon.handle_client_message("AUTH_REQUEST", {"salt": salt.hex()}, None, session_state)
    assert resp_bytes is not None
    assert "session_key" in session_state
    
    # Test valid encrypted mouse move
    key = session_state["session_key"]
    plaintext_input = b'{"type": "mouse_move", "x": 0.5, "y": 0.5}'
    encrypted = daemon.security.encrypt_packet(plaintext_input, key)
    res = daemon.handle_client_message("INPUT", {}, encrypted, session_state)
    assert res is not None
