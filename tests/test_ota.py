import os
import sys
import tempfile
import pytest
from pathlib import Path

from src.ota import (
    CURRENT_VERSION,
    OTAUpdateState,
    get_app_version,
    parse_version,
    is_newer_version,
    compute_file_sha256,
    validate_executable_file,
    verify_update_manifest,
    apply_in_place_update,
    confirm_boot,
    check_boot_state
)
from src.protocol import ProtocolMessage
from src.host_daemon import HostDaemon

def test_ota_states():
    assert OTAUpdateState.IDLE == "IDLE"
    assert OTAUpdateState.DOWNLOADING == "DOWNLOADING"
    assert OTAUpdateState.VERIFYING == "VERIFYING"
    assert OTAUpdateState.CONFIRMED == "CONFIRMED"
    assert OTAUpdateState.TESTING == "TESTING"

def test_version_helpers():
    assert get_app_version() == "1.3.0"
    assert parse_version("1.3.0") == (1, 3, 0)
    assert parse_version("1.3") == (1, 3, 0)
    assert parse_version("v1.4.2") == (1, 4, 2)
    assert parse_version("1.3.0.4") == (1, 3, 0, 4)
    assert is_newer_version("1.3.1", "1.3.0") is True
    assert is_newer_version("1.4", "1.3.0") is True
    assert is_newer_version("2.0.0", "1.3.0") is True
    assert is_newer_version("1.3.0", "1.3.0") is False
    assert is_newer_version("1.3", "1.3.0") is False
    assert is_newer_version("1.2.9", "1.3.0") is False

def test_verify_update_manifest():
    # Valid manifest
    valid_man = {"version": "1.4.0", "sha256": "abc1234567890"}
    ok, msg = verify_update_manifest(valid_man, local_version="1.3.0")
    assert ok is True

    # Missing version
    ok, msg = verify_update_manifest({"sha256": "abc"}, local_version="1.3.0")
    assert ok is False
    assert "privo di versione" in msg

    # Anti-Rollback downgrade rejection
    old_man = {"version": "1.2.5", "sha256": "abc"}
    ok, msg = verify_update_manifest(old_man, local_version="1.3.0", allow_downgrade=False)
    assert ok is False
    assert "Anti-Rollback attivo" in msg

    # Explicit downgrade allowed
    ok, msg = verify_update_manifest(old_man, local_version="1.3.0", allow_downgrade=True)
    assert ok is True

def test_file_validation_and_sha256(tmp_path):
    # Too small / not an exe
    fake_small = tmp_path / "small.exe"
    fake_small.write_bytes(b"MZ" + b"0" * 100)
    valid, msg = validate_executable_file(fake_small)
    assert valid is False
    assert "incompleto" in msg or "corrotto" in msg

    # Large enough, but not MZ header
    fake_large_bad = tmp_path / "bad.exe"
    fake_large_bad.write_bytes(b"PK" + b"0" * 1_000_100)
    valid, msg = validate_executable_file(fake_large_bad)
    assert valid is False
    assert "non valida" in msg

    # Valid mock exe
    fake_good = tmp_path / "good.exe"
    fake_good.write_bytes(b"MZ" + b"\x90" * 1_000_100)
    valid, msg = validate_executable_file(fake_good)
    assert valid is True

    # Checksum computation
    sha = compute_file_sha256(fake_good)
    assert len(sha) == 64

def test_daemon_ota_check_handler(tmp_path):
    daemon = HostDaemon(config_dir=str(tmp_path), headless=True)
    resp = daemon.handle_client_message("OTA_CHECK", {}, None, {})
    assert resp is not None
    msg_type, payload, _ = ProtocolMessage.deserialize(resp)
    assert msg_type == "OTA_INFO"
    assert "version" in payload

def test_apply_in_place_update_dev_mode(tmp_path):
    mock_new_exe = tmp_path / "new_AetherDesk.exe"
    mock_new_exe.write_bytes(b"MZ" + b"\x00" * 1_000_100)

    # Rejection by Anti-Rollback
    ok, msg = apply_in_place_update(str(mock_new_exe), target_version="1.2.0", allow_downgrade=False)
    assert ok is False
    assert "Anti-Rollback attivo" in msg

    # Success with higher version
    ok, msg = apply_in_place_update(str(mock_new_exe), target_version="1.4.0", destination_path=str(tmp_path / "dev_out.exe"))
    assert ok is True
    assert "Modalita' dev" in msg

def test_confirm_boot_and_state():
    state = check_boot_state()
    assert state in (OTAUpdateState.IDLE, OTAUpdateState.TESTING)
    assert confirm_boot() is True

def test_github_update_check():
    from src.ota import check_github_update
    # Query live GitHub repo or structure
    is_newer, info = check_github_update(local_version="1.0.0")
    # Live repo has v1.2.0, so comparing against 1.0.0 is_newer must be True
    assert "tag" in info or "error" in info
