import pytest
from src.client import ClientState

def test_client_id_validation():
    assert ClientState.validate_id("123 456 789") is True
    assert ClientState.validate_id("123456789") is True
    assert ClientState.validate_id("12 34 56 789") is True
    assert ClientState.validate_id("abc") is False
    assert ClientState.validate_id("12345678") is False # 8 digits
    assert ClientState.validate_id("1234567890") is False # 10 digits

def test_client_id_cleaning():
    assert ClientState.clean_id(" 482 910 234 ") == "482910234"

def test_device_id_combobox_extraction():
    sample = "315 747 031 (Ufficio)"
    extracted = sample.split("(")[0].strip()
    assert ClientState.validate_id(extracted) is True
    assert ClientState.clean_id(extracted) == "315747031"

def test_client_connection_handshake_state():
    state = ClientState()
    assert state.is_connected is False
    state.set_connecting(True)
    assert state.is_connecting is True
    state.set_connected(True, b"test_session_key")
    assert state.is_connected is True
    assert state.session_key == b"test_session_key"
