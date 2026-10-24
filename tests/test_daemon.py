import pytest
from src.host_daemon import HostDaemon

from src.protocol import ProtocolMessage
from src.security import SecurityManager

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
    
    # 1. Unauthenticated challenge without token MUST fail (F-02)
    session_state = {}
    salt = b"test_salt_123456"
    resp_missing = daemon.handle_client_message("AUTH_REQUEST", {"salt": salt.hex()}, None, session_state)
    m_type, _, _ = ProtocolMessage.deserialize(resp_missing)
    assert m_type == "AUTH_FAILED"
    assert session_state.get("authenticated") is False

    # 2. Challenge with valid token succeeds (using distinct client SecurityManager)
    client_sec = SecurityManager(config_dir=str(tmp_path / "client_sec"))
    master = daemon.security.get_master_key()
    session_key = client_sec.derive_session_key(master, salt)
    token = client_sec.encrypt_packet(b"AETHER_AUTH_V1", session_key)
    resp_bytes = daemon.handle_client_message("AUTH_REQUEST", {"salt": salt.hex()}, token, session_state)
    m_type, _, _ = ProtocolMessage.deserialize(resp_bytes)
    assert m_type == "AUTH_OK"
    assert session_state.get("authenticated") is True
    assert "session_key" in session_state
    
    # 3. Valid encrypted mouse move
    key = session_state["session_key"]
    plaintext_input = b'{"type": "mouse_move", "x": 0.5, "y": 0.5}'
    encrypted = client_sec.encrypt_packet(plaintext_input, key)
    res = daemon.handle_client_message("INPUT", {}, encrypted, session_state)
    assert res is not None

def test_daemon_set_quality(tmp_path):
    daemon = HostDaemon(config_dir=str(tmp_path), headless=True)
    # Unauthenticated request rejected (F-01)
    unauth_res = daemon.handle_client_message("SET_QUALITY", {"quality": 85}, None, {})
    m_type, _, _ = ProtocolMessage.deserialize(unauth_res)
    assert m_type == "AUTH_FAILED"

    # Authenticated request succeeds
    res = daemon.handle_client_message("SET_QUALITY", {"quality": 85}, None, {"authenticated": True})
    assert res is not None
    assert daemon.capture.quality == 85
