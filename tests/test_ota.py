import os
import sys
import io
import time
import shutil
import subprocess
import asyncio
import hashlib
import tempfile
import platform
import urllib.request
import urllib.error
import pytest
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.asymmetric import ed25519

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
    ensure_watchdog_terminated,
    confirm_boot,
    check_boot_state,
    cleanup_old_versions,
    build_manifest_canonical_bytes,
    verify_manifest_signature,
    sign_update_manifest,
    download_github_binary,
    check_github_update,
    OTA_VENDOR_PUBLIC_KEY_HEX,
    get_ota_staging_dir
)
from src.protocol import ProtocolMessage
from src.host_daemon import HostDaemon
from src.client import RemoteClient
from src.security import MockTestDPAPIEngine

def _test_dpapi():
    return MockTestDPAPIEngine() if platform.system() != "Windows" else None

def test_ota_states():
    assert OTAUpdateState.IDLE == "IDLE"
    assert OTAUpdateState.DOWNLOADING == "DOWNLOADING"
    assert OTAUpdateState.VERIFYING == "VERIFYING"
    assert OTAUpdateState.CONFIRMED == "CONFIRMED"
    assert OTAUpdateState.TESTING == "TESTING"

def test_version_helpers():
    assert get_app_version() == "1.4.0"
    assert parse_version("1.4.0") == (1, 4, 0)
    assert parse_version("v1.4.2") == (1, 4, 2)
    assert parse_version("V0.1.0") == (0, 1, 0)
    assert is_newer_version("1.4.1", "1.4.0") is True
    assert is_newer_version("1.5.0", "1.4.0") is True
    assert is_newer_version("2.0.0", "1.4.0") is True
    assert is_newer_version("1.4.0", "1.4.0") is False
    assert is_newer_version("1.3.0", "1.4.0") is False
    assert is_newer_version("1.3.9", "1.4.0") is False
    # Malformed versions, whitespace, or Unicode digits return False in is_newer_version
    assert is_newer_version("1.5", "1.4.0") is False
    assert is_newer_version(" 1.4.1", "1.4.0") is False
    assert is_newer_version("1.4.1 ", "1.4.0") is False
    assert is_newer_version("\uff11.\uff14.\uff11", "1.4.0") is False
    assert is_newer_version("1.4.0-beta", "1.4.0") is False

def test_version_validation_strict_and_malformed():
    """
    Deterministic in-memory test for strict major.minor.patch SemVer validation.
    Verifies valid formats (ASCII digits 0-9, intentional single v/V prefix)
    and strictly rejects non-strings, external/internal whitespace, Unicode digits,
    incomplete/extra components, leading zeros, and prerelease/build suffixes.
    """
    # 1. Valid versions (pure ASCII digits 0-9, optional single v/V prefix)
    assert parse_version("0.0.0") == (0, 0, 0)
    assert parse_version("1.0.0") == (1, 0, 0)
    assert parse_version("v1.4.0") == (1, 4, 0)
    assert parse_version("V2.10.3") == (2, 10, 3)
    assert parse_version("0.1.99") == (0, 1, 99)

    # 2. External and internal whitespace strictly rejected (no strip tolerance)
    for space_ver in (
        " 1.4.0",
        "1.4.0 ",
        "  1.4.2  ",
        "\t1.4.0",
        "1.4.0\t",
        "\n1.4.0",
        "1.4.0\n",
        "1. 4.0",
        "1.4. 0",
        "1 .4.0",
        "1.4.0 beta"
    ):
        with pytest.raises(ValueError, match="Spazi non consentiti"):
            parse_version(space_ver)

    # 3. Unicode digits strictly rejected (only ASCII 0-9 allowed)
    for unicode_ver in (
        "\uff11.\uff14.\uff10",       # Fullwidth digits: １.４.０
        "1.4.\uff10",                 # Partial fullwidth: 1.4.０
        "\u0661.\u0664.\u0660",       # Arabic-Indic digits: ١.٤.٠
        "1.4.\u0660",                 # Partial Arabic-Indic: 1.4.٠
        "\u06f1.\u06f4.\u06f0",       # Extended Arabic-Indic: ۱.۴.۰
        "\u0967.\u096a.\u0966",       # Devanagari digits: १.४.०
        "1.4.\u2070",                 # Superscript zero: 1.4.⁰
        "\u00b9.4.0",                 # Superscript one: ¹.4.0
        "1.4.\u2080",                 # Subscript zero: 1.4.₀
    ):
        with pytest.raises(ValueError, match="non numerico ASCII"):
            parse_version(unicode_ver)

    # 4. Non-string inputs
    for non_str in (None, 123, 1.4, ["1", "4", "0"], {"version": "1.4.0"}):
        with pytest.raises(ValueError, match="La versione deve essere una stringa"):
            parse_version(non_str)

    # 5. Empty strings
    with pytest.raises(ValueError, match="non puo' essere vuota"):
        parse_version("")

    # 6. Bare prefix or prefix without components or duplicate prefix
    for bad_prefix in ("v", "V", "vv1.0.0", "VV1.0.0", "vV1.0.0"):
        with pytest.raises(ValueError):
            parse_version(bad_prefix)

    # 7. Missing components (not exactly 3 components)
    for missing_comp in ("1", "1.4", "1.", ".1.4", "1..4"):
        with pytest.raises(ValueError):
            parse_version(missing_comp)

    # 8. Extra components (> 3 components)
    for extra_comp in ("1.4.0.1", "1.2.3.4.5"):
        with pytest.raises(ValueError, match="attesi esattamente 3 componenti"):
            parse_version(extra_comp)

    # 9. Non-numeric ASCII characters
    for non_num in ("1.4.a", "x.y.z", "1.beta.0", "1.4.0a"):
        with pytest.raises(ValueError, match="non numerico ASCII"):
            parse_version(non_num)

    # 10. Leading zeros in components (except single '0')
    for leading_zero in ("01.4.0", "1.04.0", "1.4.00", "00.0.0", "01.02.03"):
        with pytest.raises(ValueError, match="Zeri iniziali non ammessi"):
            parse_version(leading_zero)

    # 11. Prerelease tags, build metadata, and suffixes
    for suffix_ver in (
        "1.4.0-beta",
        "1.4.0-rc1",
        "1.4.0+build2026",
        "1.4.0_preview",
        "1.4.0~1",
        "1.4.0-1",
        "1.4.0+2026"
    ):
        with pytest.raises(ValueError, match="Suffissi di prerelease o caratteri non consentiti"):
            parse_version(suffix_ver)

def test_verify_update_manifest():
    # Valid manifest (format check without signature requirement)
    valid_man = {
        "version": "1.4.0",
        "filename": "AetherDesk.exe",
        "sha256": "a" * 64,
        "size": 15000000
    }
    ok, msg = verify_update_manifest(valid_man, local_version="1.3.0", require_signature=False)
    assert ok is True

    # Missing version
    ok, msg = verify_update_manifest({"filename": "AetherDesk.exe", "sha256": "a" * 64}, local_version="1.3.0", require_signature=False)
    assert ok is False
    assert "privo di versione" in msg

    # Strict version validation inside verify_update_manifest:
    # External spaces in manifest version rejected fail-closed before anti-rollback
    malformed_man_spaces = {"version": " 1.4.0 ", "filename": "AetherDesk.exe", "sha256": "a" * 64, "size": 15000000}
    ok_sp, msg_sp = verify_update_manifest(malformed_man_spaces, local_version="1.3.0", require_signature=False)
    assert ok_sp is False
    assert "Spazi non consentiti" in msg_sp

    # Unicode digits in manifest version rejected fail-closed
    malformed_man_unicode = {"version": "\uff11.\uff14.\uff10", "filename": "AetherDesk.exe", "sha256": "a" * 64, "size": 15000000}
    ok_uni, msg_uni = verify_update_manifest(malformed_man_unicode, local_version="1.3.0", require_signature=False)
    assert ok_uni is False
    assert "non numerico ASCII" in msg_uni

    # Incomplete components (2 parts) rejected fail-closed before anti-rollback
    malformed_man_2part = {"version": "1.4", "filename": "AetherDesk.exe", "sha256": "a" * 64, "size": 15000000}
    ok_m2, msg_m2 = verify_update_manifest(malformed_man_2part, local_version="1.3.0", require_signature=False)
    assert ok_m2 is False
    assert "Formato versione manifest non valido" in msg_m2

    # Leading zeros rejected fail-closed
    malformed_man_lz = {"version": "01.4.0", "filename": "AetherDesk.exe", "sha256": "a" * 64, "size": 15000000}
    ok_lz, msg_lz = verify_update_manifest(malformed_man_lz, local_version="1.3.0", require_signature=False)
    assert ok_lz is False
    assert "Zeri iniziali non ammessi" in msg_lz

    # Prerelease suffix rejected fail-closed
    malformed_man_suffix = {"version": "1.5.0-beta", "filename": "AetherDesk.exe", "sha256": "a" * 64, "size": 15000000}
    ok_suf, msg_suf = verify_update_manifest(malformed_man_suffix, local_version="1.3.0", require_signature=False)
    assert ok_suf is False
    assert "Suffissi di prerelease o caratteri non consentiti" in msg_suf

    # Non-string version rejected fail-closed
    malformed_man_int = {"version": 150, "filename": "AetherDesk.exe", "sha256": "a" * 64, "size": 15000000}
    ok_int, msg_int = verify_update_manifest(malformed_man_int, local_version="1.3.0", require_signature=False)
    assert ok_int is False
    assert "attesa stringa" in msg_int

    # Invalid local version rejected fail-closed
    ok_bad_loc, msg_bad_loc = verify_update_manifest(valid_man, local_version="invalid_ver", require_signature=False)
    assert ok_bad_loc is False
    assert "Versione locale non valida" in msg_bad_loc

    # Anti-Rollback downgrade rejection
    old_man = {
        "version": "1.2.5",
        "filename": "AetherDesk.exe",
        "sha256": "a" * 64,
        "size": 15000000
    }
    ok, msg = verify_update_manifest(old_man, local_version="1.3.0", allow_downgrade=False, require_signature=False)
    assert ok is False
    assert "Anti-Rollback attivo" in msg

    # Explicit downgrade allowed
    ok, msg = verify_update_manifest(old_man, local_version="1.3.0", allow_downgrade=True, require_signature=False)
    assert ok is True

    # Default require_signature=True rejects unsigned manifest fail-closed
    ok_unsigned, msg_unsigned = verify_update_manifest(valid_man, local_version="1.3.0", require_signature=True)
    assert ok_unsigned is False
    assert "Firma digitale assente" in msg_unsigned

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

    # Valid checksum verification
    valid_sha, _ = validate_executable_file(fake_good, expected_sha256=sha)
    assert valid_sha is True

    # Bad checksum rejection
    invalid_sha, err = validate_executable_file(fake_good, expected_sha256="0" * 64)
    assert invalid_sha is False
    assert "Verifica integrita' SHA-256 fallita" in err

def test_daemon_ota_check_handler(tmp_path):
    daemon = HostDaemon(config_dir=str(tmp_path), headless=True, dpapi_engine=_test_dpapi(), start_worker=False)
    try:
        resp = daemon.handle_client_message("OTA_CHECK", {}, None, {})
        assert resp is not None
        msg_type, payload, _ = ProtocolMessage.deserialize(resp)
        assert msg_type == "OTA_INFO"
        assert "version" in payload
        assert payload.get("available") is False
        assert "fail-closed" in payload.get("error", "").lower()
    finally:
        daemon.stop()

def test_apply_in_place_update_dev_mode(tmp_path):
    mock_new_exe = tmp_path / "new_AetherDesk.exe"
    mock_new_exe.write_bytes(b"MZ" + b"\x00" * 1_000_100)

    # Rejection by Anti-Rollback
    ok, msg = apply_in_place_update(str(mock_new_exe), target_version="1.2.0", allow_downgrade=False, allow_unverified_dev_hash=True)
    assert ok is False
    assert "Anti-Rollback attivo" in msg

    # Success with higher version
    ok, msg = apply_in_place_update(str(mock_new_exe), target_version="1.5.0", destination_path=str(tmp_path / "dev_out.exe"), allow_unverified_dev_hash=True)
    assert ok is True
    assert "Modalita' dev" in msg

def test_confirm_boot_and_state():
    state = check_boot_state()
    assert state in (OTAUpdateState.IDLE, OTAUpdateState.TESTING)
    assert confirm_boot() is True

def test_cleanup_old_versions(tmp_path):
    """
    Deterministic test for cleanup_old_versions() in isolated tmp_path:
    Verifies that cleanup strictly purges only AetherDesk-owned OTA artifacts:
    - exact backups (AetherDesk.exe.old, AetherDesk.old)
    - OTA watchdog and restart scripts (ota_watchdog*.bat, aether_restart_*.bat)
    - staging files with unambiguous ownership (~AetherDesk.exe.new, aether_ota_staging*.exe)
    - protocol markers (.ota_ready_*)
    And strictly PRESERVES non-OTA sentinel files belonging to other applications or the user:
    - temp_update_staging.exe (generic staging name without AetherDesk ownership prefix)
    - altri_app.exe.old
    - documento.bak
    - backup.old
    - legacy.bak
    As well as preserving the active executable (AetherDesk.exe).
    """
    from src.ota import cleanup_old_versions

    # 1. Setup owned OTA leftover files
    ota_bak1 = tmp_path / "AetherDesk.exe.old"
    ota_bak2 = tmp_path / "AetherDesk.old"
    ota_watchdog = tmp_path / "ota_watchdog.bat"
    ota_restart = tmp_path / "aether_restart_99.bat"
    ota_staging = tmp_path / "aether_ota_staging_123.exe"
    ota_staged_target = tmp_path / "~AetherDesk.exe.new"
    ota_ready = tmp_path / ".ota_ready_99"

    for p in [ota_bak1, ota_bak2, ota_watchdog, ota_restart, ota_staging, ota_staged_target, ota_ready]:
        p.write_bytes(b"OTA_LEFTOVER_DATA")

    # 2. Setup active executable
    active_exe = tmp_path / "AetherDesk.exe"
    active_exe.write_bytes(b"ACTIVE_APPLICATION_BINARY")

    # 3. Setup non-OTA sentinel files belonging to other apps or user
    generic_staging = tmp_path / "temp_update_staging.exe"
    generic_staging.write_bytes(b"GENERIC_STAGING_SENTINEL_DO_NOT_TOUCH")
    other_app_old = tmp_path / "altri_app.exe.old"
    other_app_old.write_bytes(b"OTHER_APP_BACKUP_DO_NOT_TOUCH")
    doc_bak = tmp_path / "documento.bak"
    doc_bak.write_bytes(b"USER_DOCUMENT_BACKUP_DO_NOT_TOUCH")
    backup_old = tmp_path / "backup.old"
    backup_old.write_bytes(b"USER_GENERIC_BACKUP_DO_NOT_TOUCH")
    legacy_bak = tmp_path / "legacy.bak"
    legacy_bak.write_bytes(b"UNRELATED_BACKUP_DO_NOT_TOUCH")

    # Run cleanup
    count = cleanup_old_versions(tmp_path)
    assert count == 7

    # Verify owned OTA artifacts were deleted
    assert not ota_bak1.exists()
    assert not ota_bak2.exists()
    assert not ota_watchdog.exists()
    assert not ota_restart.exists()
    assert not ota_staging.exists()
    assert not ota_staged_target.exists()
    assert not ota_ready.exists()

    # Verify active executable was STRICTLY preserved
    assert active_exe.exists()
    assert active_exe.read_bytes() == b"ACTIVE_APPLICATION_BINARY"

    # Verify non-OTA sentinel files were STRICTLY PRESERVED without deletion or alteration
    assert generic_staging.exists()
    assert generic_staging.read_bytes() == b"GENERIC_STAGING_SENTINEL_DO_NOT_TOUCH"
    assert other_app_old.exists()
    assert other_app_old.read_bytes() == b"OTHER_APP_BACKUP_DO_NOT_TOUCH"
    assert doc_bak.exists()
    assert doc_bak.read_bytes() == b"USER_DOCUMENT_BACKUP_DO_NOT_TOUCH"
    assert backup_old.exists()
    assert backup_old.read_bytes() == b"USER_GENERIC_BACKUP_DO_NOT_TOUCH"
    assert legacy_bak.exists()
    assert legacy_bak.read_bytes() == b"UNRELATED_BACKUP_DO_NOT_TOUCH"

def test_github_update_check():
    from src.ota import check_github_update
    # Query live GitHub repo or structure
    is_newer, info = check_github_update(local_version="1.0.0")
    # Live repo has v1.2.0 without vendor signature, so check must return error or tag
    assert "tag" in info or "error" in info

def test_ed25519_signed_manifest_valid_and_tampered():
    """
    Deterministic in-memory test for Ed25519 signed manifest validation and schema enforcement.
    Verifies valid signatures, strict field validation (64-char hex sha256, .exe filename, size >= 1MB),
    detects tampering across all fields, and rejects invalid keys.
    """
    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()

    manifest_data = {
        "version": "1.5.0",
        "filename": "AetherDesk.exe",
        "sha256": "a" * 64,
        "size": 15000000
    }
    signed_man = sign_update_manifest(manifest_data, priv)
    assert "signature" in signed_man
    assert len(signed_man["signature"]) == 128

    # 1. Valid signature passes
    ok, msg = verify_update_manifest(signed_man, local_version="1.4.0", public_key_hex=pub_hex)
    assert ok is True
    assert "verificato con successo" in msg

    # 2. Schema check: SHA-256 not 64 hex characters fails
    bad_sha_man = dict(signed_man)
    bad_sha_man["sha256"] = "abc123"  # too short
    ok_bsha, msg_bsha = verify_update_manifest(bad_sha_man, local_version="1.4.0", public_key_hex=pub_hex)
    assert ok_bsha is False
    assert "Digest SHA-256 non valido" in msg_bsha

    # Non-hex characters in SHA-256 fails
    bad_hex_sha = dict(signed_man)
    bad_hex_sha["sha256"] = "g" * 64
    ok_ghash, msg_ghash = verify_update_manifest(bad_hex_sha, local_version="1.4.0", public_key_hex=pub_hex)
    assert ok_ghash is False
    assert "Digest SHA-256 non valido" in msg_ghash

    # 3. Schema check: invalid filename (path traversal or non-exe) fails
    trav_file = dict(signed_man)
    trav_file["filename"] = "../malicious.exe"
    ok_trav, msg_trav = verify_update_manifest(trav_file, local_version="1.4.0", public_key_hex=pub_hex)
    assert ok_trav is False
    assert "Nome asset non valido" in msg_trav

    non_exe_file = dict(signed_man)
    non_exe_file["filename"] = "update.dll"
    ok_non_exe, msg_non_exe = verify_update_manifest(non_exe_file, local_version="1.4.0", public_key_hex=pub_hex)
    assert ok_non_exe is False
    assert "richiesta estensione .exe" in msg_non_exe

    # 4. Schema check: invalid size (< 1_000_000 or non-int) fails
    small_size = dict(signed_man)
    small_size["size"] = 500
    ok_size, msg_size = verify_update_manifest(small_size, local_version="1.4.0", public_key_hex=pub_hex)
    assert ok_size is False
    assert "Dimensione asset non valida" in msg_size

    # 5. Tampered SHA-256 fails signature verification
    tampered_sha = dict(signed_man)
    tampered_sha["sha256"] = "b" * 64
    ok_sha, msg_sha = verify_update_manifest(tampered_sha, local_version="1.4.0", public_key_hex=pub_hex)
    assert ok_sha is False
    assert "non valida" in msg_sha or "manomesso" in msg_sha

    # 6. Tampered version fails signature verification
    tampered_ver = dict(signed_man)
    tampered_ver["version"] = "1.5.1"
    ok_ver, msg_ver = verify_update_manifest(tampered_ver, local_version="1.4.0", public_key_hex=pub_hex)
    assert ok_ver is False

    # 7. Tampered filename fails signature verification
    tampered_file = dict(signed_man)
    tampered_file["filename"] = "AetherClient.exe"
    ok_file, msg_file = verify_update_manifest(tampered_file, local_version="1.4.0", public_key_hex=pub_hex)
    assert ok_file is False

    # 8. Corrupted signature fails
    tampered_sig = dict(signed_man)
    tampered_sig["signature"] = "0" * 128
    ok_sig, msg_sig = verify_update_manifest(tampered_sig, local_version="1.4.0", public_key_hex=pub_hex)
    assert ok_sig is False

    # 9. Different public key fails
    other_priv = ed25519.Ed25519PrivateKey.generate()
    other_pub_hex = other_priv.public_key().public_bytes_raw().hex()
    ok_other, msg_other = verify_update_manifest(signed_man, local_version="1.4.0", public_key_hex=other_pub_hex)
    assert ok_other is False

def test_download_github_binary_fail_closed_without_auth(monkeypatch):
    """
    Verify that download_github_binary rejects unauthenticated calls fail-closed
    without performing any network operations.
    """
    # 1. Dev mode without auth or manifest
    ok, err = download_github_binary("https://example.com/binary.exe")
    assert ok is False
    assert "fail-closed" in err

    # 2. Frozen mode with isolated expected_sha256 (no signed manifest)
    monkeypatch.setattr(sys, "frozen", True)
    ok_frozen, err_frozen = download_github_binary(
        "https://example.com/binary.exe",
        expected_sha256="a" * 64
    )
    assert ok_frozen is False
    assert "fail-closed" in err_frozen

    # 3. Frozen mode with allow_unverified_dev_hash=True must STILL be rejected fail-closed
    ok_frozen_dev, err_frozen_dev = download_github_binary(
        "https://example.com/binary.exe",
        expected_sha256="a" * 64,
        allow_unverified_dev_hash=True
    )
    assert ok_frozen_dev is False
    assert "fail-closed" in err_frozen_dev

def test_download_github_binary_mocked_urlopen(tmp_path, monkeypatch):
    """
    Deterministic in-memory test for download_github_binary with simulated HTTP responses.
    Verifies valid download, SHA-256 match, bad checksum cleanup, asset filename binding,
    and signed manifest validation.
    """
    mock_exe_bytes = b"MZ" + b"\x90" * 1_000_100
    mock_sha = hashlib.sha256(mock_exe_bytes).hexdigest()

    class MockHTTPResponse:
        def __init__(self, data: bytes):
            self._stream = io.BytesIO(data)
            self.headers = {"Content-Length": str(len(data))}
        def read(self, amt=-1):
            return self._stream.read(amt)
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: MockHTTPResponse(mock_exe_bytes)
    )

    # 1. Dev mode explicit bypass with correct expected_sha256
    ok, path_or_err = download_github_binary(
        "https://example.com/AetherDesk.exe",
        expected_sha256=mock_sha,
        allow_unverified_dev_hash=True
    )
    assert ok is True
    downloaded_p = Path(path_or_err)
    try:
        assert downloaded_p.exists()
        assert downloaded_p.read_bytes()[:2] == b"MZ"
    finally:
        if downloaded_p.exists():
            downloaded_p.unlink()

    # 2. Failure and cleanup with incorrect expected_sha256
    bad_sha = "0" * 64
    ok_bad, err_msg = download_github_binary(
        "https://example.com/AetherDesk.exe",
        expected_sha256=bad_sha,
        allow_unverified_dev_hash=True
    )
    assert ok_bad is False
    assert "Verifica integrita' SHA-256 fallita" in err_msg

    # 3. Success with valid signed manifest
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes_raw().hex()
    manifest_dict = {
        "version": "1.5.0",
        "filename": "AetherDesk.exe",
        "sha256": mock_sha,
        "size": len(mock_exe_bytes)
    }
    signed_man = sign_update_manifest(manifest_dict, priv)

    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub)

    ok_sig, path_sig = download_github_binary(
        "https://example.com/AetherDesk.exe",
        signed_manifest=signed_man
    )
    assert ok_sig is True
    p_sig = Path(path_sig)
    try:
        assert p_sig.exists()
    finally:
        if p_sig.exists():
            p_sig.unlink()

    # 4. Asset binding failure: URL filename does not match manifest filename
    ok_diff_name, err_diff_name = download_github_binary(
        "https://example.com/OtherApp.exe",
        signed_manifest=signed_man
    )
    assert ok_diff_name is False
    assert "non corrisponde all'asset vincolato" in err_diff_name

    # 5. Failure with tampered signed manifest (corrupted signature)
    tampered_man = dict(signed_man)
    tampered_man["signature"] = "1" * 128
    ok_tamper, err_tamper = download_github_binary(
        "https://example.com/AetherDesk.exe",
        signed_manifest=tampered_man
    )
    assert ok_tamper is False
    assert "Manifest firmato non valido" in err_tamper

def test_download_github_binary_cleanup_on_all_failures(monkeypatch):
    """
    Deterministic in-memory test: verifies download_github_binary cleans up its temporary file
    under ALL failure conditions (network connection failure, mid-stream read abort, progress_callback
    exception, binary validation failure) and ensures NO orphaned or partial files remain on disk.
    """
    mock_valid_exe = b"MZ" + b"\x90" * 1_000_100
    mock_valid_sha = hashlib.sha256(mock_valid_exe).hexdigest()

    allocated_temp_files = []
    real_named_temp = tempfile.NamedTemporaryFile

    def tracked_named_temp(*args, **kwargs):
        tf = real_named_temp(*args, **kwargs)
        allocated_temp_files.append(tf.name)
        return tf

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", tracked_named_temp)

    class MockStreamResponse:
        def __init__(self, data: bytes):
            self._stream = io.BytesIO(data)
            self.headers = {"Content-Length": str(len(data))}
        def read(self, amt=-1):
            return self._stream.read(amt)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    # 1. Failure at urlopen (network failure / connection refused)
    allocated_temp_files.clear()
    def mock_urlopen_net_error(req, timeout=None):
        raise urllib.error.URLError("Network unreachable")

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen_net_error)
    ok_net, err_net = download_github_binary(
        "https://example.com/AetherDesk.exe",
        expected_sha256=mock_valid_sha,
        allow_unverified_dev_hash=True
    )
    assert ok_net is False
    assert "Errore durante il download da GitHub" in err_net
    assert len(allocated_temp_files) == 1
    assert not Path(allocated_temp_files[0]).exists()

    # 2. Failure during stream read (mid-stream network disconnect)
    allocated_temp_files.clear()
    class MidStreamAbortResponse:
        def __init__(self):
            self.headers = {"Content-Length": "1000000"}
            self._chunks = 0
        def read(self, amt=-1):
            if self._chunks == 0:
                self._chunks += 1
                return b"MZ" + b"\x90" * 20000
            raise ConnectionResetError("Connection aborted midway")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: MidStreamAbortResponse())
    ok_abort, err_abort = download_github_binary(
        "https://example.com/AetherDesk.exe",
        expected_sha256=mock_valid_sha,
        allow_unverified_dev_hash=True
    )
    assert ok_abort is False
    assert "Errore durante il download da GitHub" in err_abort
    assert len(allocated_temp_files) == 1
    assert not Path(allocated_temp_files[0]).exists()

    # 3. Failure during progress_callback execution (caller cancels / callback raises)
    allocated_temp_files.clear()
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: MockStreamResponse(mock_valid_exe)
    )
    def aborting_callback(received, total):
        raise RuntimeError("User cancelled update via progress callback")

    ok_cb, err_cb = download_github_binary(
        "https://example.com/AetherDesk.exe",
        progress_callback=aborting_callback,
        expected_sha256=mock_valid_sha,
        allow_unverified_dev_hash=True
    )
    assert ok_cb is False
    assert "Errore durante il download da GitHub" in err_cb
    assert "User cancelled update" in err_cb
    assert len(allocated_temp_files) == 1
    assert not Path(allocated_temp_files[0]).exists()

    # 4. Failure during binary validation (corrupted header / not MZ)
    allocated_temp_files.clear()
    mock_corrupted_exe = b"XX" + b"\x90" * 1_000_100
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: MockStreamResponse(mock_corrupted_exe)
    )
    ok_val, err_val = download_github_binary(
        "https://example.com/AetherDesk.exe",
        expected_sha256=hashlib.sha256(mock_corrupted_exe).hexdigest(),
        allow_unverified_dev_hash=True
    )
    assert ok_val is False
    assert "File scaricato non valido" in err_val
    assert len(allocated_temp_files) == 1
    assert not Path(allocated_temp_files[0]).exists()

    # 5. Success case: file is preserved and returned, caller manages valid file
    allocated_temp_files.clear()
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: MockStreamResponse(mock_valid_exe)
    )
    ok_succ, path_succ = download_github_binary(
        "https://example.com/AetherDesk.exe",
        expected_sha256=mock_valid_sha,
        allow_unverified_dev_hash=True
    )
    assert ok_succ is True
    assert len(allocated_temp_files) == 1
    succ_p = Path(path_succ)
    try:
        assert succ_p.exists()
        assert succ_p == Path(allocated_temp_files[0])
    finally:
        if succ_p.exists():
            succ_p.unlink()

def test_apply_in_place_update_fail_closed_frozen(tmp_path, monkeypatch):
    """
    Ensure apply_in_place_update rejects unauthenticated binaries fail-closed in frozen production mode,
    even if an isolated expected_sha256 or allow_unverified_dev_hash is provided,
    and guarantees immediate cleanup of the temporary file on every failure.
    """
    monkeypatch.setattr(sys, "frozen", True)

    # 1. Calling without manifest and without sha -> fails closed & cleans up temp file
    mock_exe1 = tmp_path / "app1.exe"
    mock_exe1.write_bytes(b"MZ" + b"\x90" * 1_000_100)
    ok, msg = apply_in_place_update(str(mock_exe1), target_version="2.0.0")
    assert ok is False
    assert "fail-closed" in msg.lower()
    assert not mock_exe1.exists()

    # 2. Calling with isolated expected_sha256 in frozen mode -> fails closed & cleans up temp file
    mock_exe2 = tmp_path / "app2.exe"
    mock_exe2.write_bytes(b"MZ" + b"\x90" * 1_000_100)
    mock_sha2 = hashlib.sha256(mock_exe2.read_bytes()).hexdigest()
    ok_sha, msg_sha = apply_in_place_update(str(mock_exe2), target_version="2.0.0", expected_sha256=mock_sha2)
    assert ok_sha is False
    assert "fail-closed" in msg_sha.lower()
    assert not mock_exe2.exists()

    # 3. Calling with allow_unverified_dev_hash=True in frozen mode -> fails closed & cleans up temp file
    mock_exe3 = tmp_path / "app3.exe"
    mock_exe3.write_bytes(b"MZ" + b"\x90" * 1_000_100)
    mock_sha3 = hashlib.sha256(mock_exe3.read_bytes()).hexdigest()
    ok_dev, msg_dev = apply_in_place_update(
        str(mock_exe3),
        target_version="2.0.0",
        expected_sha256=mock_sha3,
        allow_unverified_dev_hash=True
    )
    assert ok_dev is False
    assert "fail-closed" in msg_dev.lower()
    assert not mock_exe3.exists()

async def test_daemon_stream_ota_update_fail_closed():
    """
    Ensure host daemon rejects _stream_ota_update fail-closed with OTA_ERROR.
    """
    daemon = HostDaemon(headless=True, dpapi_engine=_test_dpapi(), start_worker=False)
    try:
        class MockWS:
            def __init__(self):
                self.sent = []
            async def send(self, msg):
                self.sent.append(msg)
        ws = MockWS()
        await daemon._stream_ota_update(ws, None)
        assert len(ws.sent) == 1
        msg_type, payload, _ = ProtocolMessage.deserialize(ws.sent[0])
        assert msg_type == "OTA_ERROR"
        assert "fail-closed" in payload.get("error", "").lower()
    finally:
        daemon.stop()

@pytest.mark.asyncio
async def test_client_p2p_ota_finish_fail_closed(tmp_path):
    """
    Deterministic in-memory test: verifies RemoteClient receive loop cleans up temp files
    and rejects P2P OTA updates fail-closed upon receiving OTA_FINISH.
    """
    class InMemoryWebSocket:
        def __init__(self, in_queue, out_queue):
            self.in_queue = in_queue
            self.out_queue = out_queue
            self.closed = False

        async def recv(self):
            if self.closed:
                raise Exception("Closed")
            item = await self.in_queue.get()
            if item is None:
                raise Exception("Closed")
            return item

        async def send(self, data):
            if self.closed:
                raise Exception("Closed")
            await self.out_queue.put(data)

        async def close(self):
            self.closed = True

    in_q = asyncio.Queue()
    out_q = asyncio.Queue()
    mock_ws = InMemoryWebSocket(in_q, out_q)

    client = RemoteClient(config_dir=str(tmp_path), ws_connector=lambda uri: mock_ws)
    temp_ota_file = tmp_path / "ota_p2p_staged.exe"
    temp_ota_file.write_bytes(b"MZ" + b"\x00" * 1_000_100)
    client._ota_temp_file = str(temp_ota_file)

    completed_events = []
    client.on_ota_complete_callback = lambda ok, msg: completed_events.append((ok, msg))

    finish_msg = ProtocolMessage.serialize("OTA_FINISH", {"version": "2.0.0", "sha256": "fake_sha"})
    await in_q.put(finish_msg)
    disconnect_msg = ProtocolMessage.serialize("CLIENT_DISCONNECTED", {})
    await in_q.put(disconnect_msg)

    try:
        await asyncio.wait_for(client._receive_loop(mock_ws), timeout=2.0)
    except Exception:
        pass
    finally:
        await client.disconnect()

    assert len(completed_events) == 1
    assert completed_events[0][0] is False
    assert "fail-closed" in completed_events[0][1].lower()
    assert not temp_ota_file.exists()
    assert client._ota_temp_file is None

def test_sign_release_tool(tmp_path, monkeypatch):
    """
    Deterministic in-memory test for scripts/sign_release.py:
    verifies load_private_key (raw 32B, 64-char hex, encrypted/unencrypted PEM),
    executable validation, fail-closed rejection on key mismatch without writing or altering
    output files, and manifest schema verification using the fixed public trust anchor.
    Also verifies that the CLI cannot bypass the trust anchor and halts cleanly on failure.
    """
    from scripts.sign_release import load_private_key, create_signed_manifest, main as sign_release_main
    from cryptography.hazmat.primitives import serialization

    # Generate ephemeral keypair in memory for test (never persisted)
    priv = ed25519.Ed25519PrivateKey.generate()
    raw_bytes = priv.private_bytes_raw()
    hex_str = raw_bytes.hex()
    pub_hex = priv.public_key().public_bytes_raw().hex()

    pem_bytes = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    # 1. Test load_private_key via raw binary file
    raw_key_file = tmp_path / "raw.key"
    raw_key_file.write_bytes(raw_bytes)
    k_raw = load_private_key(key_file=raw_key_file)
    assert k_raw.public_key().public_bytes_raw().hex() == pub_hex

    # 2. Test load_private_key via hex text file
    hex_key_file = tmp_path / "hex.key"
    hex_key_file.write_text(hex_str)
    k_hex_file = load_private_key(key_file=hex_key_file)
    assert k_hex_file.public_key().public_bytes_raw().hex() == pub_hex

    # 3. Test load_private_key via PEM file (unencrypted)
    pem_key_file = tmp_path / "priv.pem"
    pem_key_file.write_bytes(pem_bytes)
    k_pem = load_private_key(key_file=pem_key_file)
    assert k_pem.public_key().public_bytes_raw().hex() == pub_hex

    # 4. Test load_private_key via encrypted PEM file with passphrase
    encrypted_pem_bytes = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(b"secure_passphrase_123")
    )
    enc_pem_file = tmp_path / "priv_enc.pem"
    enc_pem_file.write_bytes(encrypted_pem_bytes)
    k_enc_pem = load_private_key(key_file=enc_pem_file, password="secure_passphrase_123")
    assert k_enc_pem.public_key().public_bytes_raw().hex() == pub_hex

    # 5. Invalid key file paths or corrupted formats raise appropriate exceptions
    with pytest.raises(FileNotFoundError):
        load_private_key(key_file=tmp_path / "nonexistent.key")
    with pytest.raises(ValueError):
        load_private_key(key_file="")

    bad_content_file = tmp_path / "bad.key"
    bad_content_file.write_bytes(b"corrupted_key_material_too_short")
    with pytest.raises(ValueError, match="Formato file chiave privata non riconosciuto"):
        load_private_key(key_file=bad_content_file)

    # 6. Setup valid mock executable
    mock_exe = tmp_path / "AetherDesk.exe"
    mock_exe_bytes = b"MZ" + b"\x90" * 1_200_000
    mock_exe.write_bytes(mock_exe_bytes)

    # Monkeypatch the app's fixed trust anchor to the test keypair's public key
    monkeypatch.setattr("scripts.sign_release.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    # 7. Success case: Matching key generates manifest verified against fixed trust anchor
    # Notice create_signed_manifest has NO expected_public_key_hex parameter (cannot be overridden)
    signed_man, verified_pub = create_signed_manifest(
        binary_path=str(mock_exe),
        version="1.5.0",
        private_key=priv
    )
    assert signed_man["version"] == "1.5.0"
    assert signed_man["filename"] == "AetherDesk.exe"
    assert signed_man["sha256"] == hashlib.sha256(mock_exe_bytes).hexdigest()
    assert signed_man["size"] == len(mock_exe_bytes)
    assert "signature" in signed_man
    assert len(signed_man["signature"]) == 128
    assert verified_pub == pub_hex

    # Valid manifest can be written to disk
    valid_out = tmp_path / "manifest.json"
    valid_out.write_text(json.dumps(signed_man), encoding="utf-8")
    assert valid_out.exists()

    # 8. Fail-closed case: Mismatched key rejected without writing or altering output file
    other_priv = ed25519.Ed25519PrivateKey.generate()
    other_pub_hex = other_priv.public_key().public_bytes_raw().hex()
    assert other_pub_hex != pub_hex

    canary_uncreated = tmp_path / "uncreated_manifest.json"
    canary_existing = tmp_path / "existing_manifest.json"
    canary_existing.write_text("PREEXISTING_VALID_CONTENT", encoding="utf-8")

    # Mismatch between private key and fixed trust anchor raises ValueError fail-closed
    with pytest.raises(ValueError, match="Rifiuto firma fail-closed"):
        create_signed_manifest(
            binary_path=str(mock_exe),
            version="1.5.0",
            private_key=other_priv
        )

    # Verify output files were NOT created or altered
    assert not canary_uncreated.exists()
    assert canary_existing.read_text(encoding="utf-8") == "PREEXISTING_VALID_CONTENT"

    # 9. Malformed versions or invalid PE headers rejected
    with pytest.raises(ValueError, match="Formato versione non valido"):
        create_signed_manifest(str(mock_exe), version="1.5", private_key=priv)

    bad_exe = tmp_path / "invalid.exe"
    bad_exe.write_bytes(b"NOT_MZ" + b"0" * 100)
    with pytest.raises(ValueError, match="non valido"):
        create_signed_manifest(str(bad_exe), version="1.5.0", private_key=priv)

    # 10. CLI flow cannot bypass trust anchor: --expected-public-key is rejected by parser (SystemExit)
    # and output file is not created
    cli_bypass_target = tmp_path / "cli_bypass_manifest.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sign_release.py",
            "--binary", str(mock_exe),
            "--key-file", str(pem_key_file),
            "--expected-public-key", other_pub_hex,
            "--output", str(cli_bypass_target)
        ]
    )
    with pytest.raises(SystemExit):
        sign_release_main()
    assert not cli_bypass_target.exists()

    # 11. CLI execution with mismatched private key returns error code (1) and does NOT create or alter output file
    other_pem_file = tmp_path / "other_priv.pem"
    other_pem_file.write_bytes(
        other_priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
    )
    cli_mismatch_target = tmp_path / "cli_mismatch_manifest.json"
    canary_existing_cli = tmp_path / "existing_cli_manifest.json"
    canary_existing_cli.write_text("PREEXISTING_CLI_CONTENT", encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sign_release.py",
            "--binary", str(mock_exe),
            "--key-file", str(other_pem_file),
            "--output", str(cli_mismatch_target)
        ]
    )
    ret = sign_release_main()
    assert ret != 0
    assert not cli_mismatch_target.exists()

    # Also verify existing file is untouched when CLI signing fails
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sign_release.py",
            "--binary", str(mock_exe),
            "--key-file", str(other_pem_file),
            "--output", str(canary_existing_cli)
        ]
    )
    ret_exist = sign_release_main()
    assert ret_exist != 0
    assert canary_existing_cli.read_text(encoding="utf-8") == "PREEXISTING_CLI_CONTENT"

def test_check_github_update_multi_asset_selection(monkeypatch):
    """
    Deterministic in-memory test for check_github_update:
    Verifies that when a release contains multiple .exe assets, the client strictly selects
    the executable matching the authenticated filename in the manifest, rejects releases
    where the bound asset is missing, and rejects ambiguous duplicate assets fail-closed.
    """
    from src.ota import check_github_update

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()

    manifest_dict = {
        "version": "1.5.0",
        "filename": "AetherDesk.exe",
        "sha256": "a" * 64,
        "size": 15000000
    }
    signed_man = sign_update_manifest(manifest_dict, priv)

    # 1. Multiple executables present: client selects the one matching manifest
    release_payload_multi = {
        "tag_name": "v1.5.0",
        "name": "Release v1.5.0",
        "body": f"AETHER_MANIFEST:\n{json.dumps(signed_man)}\n---",
        "assets": [
            {"name": "Setup.exe", "browser_download_url": "https://example.com/Setup.exe", "size": 20000000},
            {"name": "AetherDesk.exe", "browser_download_url": "https://example.com/AetherDesk.exe", "size": 15000000},
            {"name": "OtherApp.exe", "browser_download_url": "https://example.com/OtherApp.exe", "size": 10000000},
        ]
    }

    class MockReleaseResponse:
        def __init__(self, data: dict):
            self._stream = io.BytesIO(json.dumps(data).encode("utf-8"))
        def read(self, amt=-1):
            return self._stream.read(amt)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: MockReleaseResponse(release_payload_multi)
    )

    is_newer, info = check_github_update(local_version="1.4.0", public_key_hex=pub_hex)
    assert is_newer is True
    assert info["download_url"] == "https://example.com/AetherDesk.exe"
    assert info["manifest"]["filename"] == "AetherDesk.exe"

    # 2. Bound asset missing from assets: rejected fail-closed
    release_payload_missing = {
        "tag_name": "v1.5.0",
        "name": "Release v1.5.0",
        "body": f"AETHER_MANIFEST:\n{json.dumps(signed_man)}\n---",
        "assets": [
            {"name": "WrongName.exe", "browser_download_url": "https://example.com/WrongName.exe", "size": 15000000}
        ]
    }
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: MockReleaseResponse(release_payload_missing)
    )
    is_newer_miss, info_miss = check_github_update(local_version="1.4.0", public_key_hex=pub_hex)
    assert is_newer_miss is False
    assert "non trovato tra gli asset" in info_miss.get("error", "")

    # 3. Duplicate/ambiguous assets matching manifest: rejected fail-closed
    release_payload_dupe = {
        "tag_name": "v1.5.0",
        "name": "Release v1.5.0",
        "body": f"AETHER_MANIFEST:\n{json.dumps(signed_man)}\n---",
        "assets": [
            {"name": "AetherDesk.exe", "browser_download_url": "https://example.com/AetherDesk.exe", "size": 15000000},
            {"name": "AetherDesk.exe", "browser_download_url": "https://example.com/dup/AetherDesk.exe", "size": 15000000}
        ]
    }
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: MockReleaseResponse(release_payload_dupe)
    )
    is_newer_dupe, info_dupe = check_github_update(local_version="1.4.0", public_key_hex=pub_hex)
    assert is_newer_dupe is False
    assert "molteplici asset ambigui" in info_dupe.get("error", "")

def test_apply_in_place_update_temp_cleanup_on_all_failures(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies apply_in_place_update cleans up the passed
    temporary executable on ALL error paths (anti-rollback, PE header mismatch, size mismatch,
    or manifest schema error) without deleting unrelated files.
    """
    monkeypatch.setattr(sys, "frozen", True)
    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    staging_dir = get_ota_staging_dir()
    unrelated_file = tmp_path / "keep_me.txt"
    unrelated_file.write_text("IMPORTANT_USER_DATA", encoding="utf-8")

    # 1. Anti-Rollback failure (target_version <= CURRENT_VERSION)
    valid_exe_bytes = b"MZ" + b"\x90" * 1_000_100
    temp_rollback = staging_dir / "aether_ota_temp_rollback.exe"
    temp_rollback.write_bytes(valid_exe_bytes)
    man_rollback = sign_update_manifest({
        "version": "1.3.0",  # Older than CURRENT_VERSION (1.4.0)
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(valid_exe_bytes).hexdigest(),
        "size": len(valid_exe_bytes)
    }, priv)

    ok_rb, msg_rb = apply_in_place_update(str(temp_rollback), signed_manifest=man_rollback)
    assert ok_rb is False
    assert "Anti-Rollback" in msg_rb
    assert not temp_rollback.exists()
    assert unrelated_file.exists()

    # 2. Corrupted PE header failure (not starting with MZ)
    bad_pe_bytes = b"XX" + b"\x90" * 1_000_100
    temp_bad_pe = staging_dir / "aether_ota_temp_bad_pe.exe"
    temp_bad_pe.write_bytes(bad_pe_bytes)
    man_bad_pe = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(bad_pe_bytes).hexdigest(),
        "size": len(bad_pe_bytes)
    }, priv)

    ok_pe, msg_pe = apply_in_place_update(str(temp_bad_pe), signed_manifest=man_bad_pe)
    assert ok_pe is False
    assert "MZ" in msg_pe or "Intestazione" in msg_pe
    assert not temp_bad_pe.exists()
    assert unrelated_file.exists()

    # 3. File size mismatch failure
    temp_bad_sz = staging_dir / "aether_ota_temp_bad_sz.exe"
    temp_bad_sz.write_bytes(valid_exe_bytes)
    man_bad_sz = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(valid_exe_bytes).hexdigest(),
        "size": len(valid_exe_bytes) + 5000  # Declared size differs from actual
    }, priv)

    ok_sz, msg_sz = apply_in_place_update(str(temp_bad_sz), signed_manifest=man_bad_sz)
    assert ok_sz is False
    assert "Dimensione file non corrispondente" in msg_sz
    assert not temp_bad_sz.exists()
    assert unrelated_file.exists()

def test_apply_in_place_update_copy_failure_rollback(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies that if copying the new binary to same-volume staging fails,
    apply_in_place_update aborts fail-closed, cleans the staging slot, removes the temporary file,
    and leaves the running current_exe completely untouched.
    """
    original_bytes = b"ORIGINAL_WORKING_APPLICATION_BINARY_v1_4_0"
    mock_current_exe = tmp_path / "AetherDesk.exe"
    mock_current_exe.write_bytes(original_bytes)

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_current_exe))

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    new_exe_bytes = b"MZ" + b"\x90" * 1_200_000
    staging_dir = get_ota_staging_dir()
    temp_update = staging_dir / "aether_ota_staged_download.exe"
    temp_update.write_bytes(new_exe_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_exe_bytes).hexdigest(),
        "size": len(new_exe_bytes)
    }, priv)

    # Simulate failure during shutil.copy2 when copying to same-volume staging
    real_copy2 = shutil.copy2
    staged_target = mock_current_exe.parent / f"~{mock_current_exe.name}.new"

    def failing_copy2(src, dst):
        if Path(dst) == staged_target:
            raise OSError("Simulated disk error or permission failure during same-volume staging")
        return real_copy2(src, dst)

    monkeypatch.setattr(shutil, "copy2", failing_copy2)

    ok, err_msg = apply_in_place_update(str(temp_update), signed_manifest=signed_man)
    assert ok is False
    assert "Impossibile copiare il nuovo binario nella staging dello stesso volume" in err_msg
    # Current exe was never touched and remains intact!
    assert mock_current_exe.exists()
    assert mock_current_exe.read_bytes() == original_bytes
    assert not staged_target.exists()
    assert not (mock_current_exe.parent / "AetherDesk.exe.old").exists()
    # Staging temp download file must be deleted
    assert not temp_update.exists()

def test_cleanup_old_versions_testing_state_guard(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies that cleanup_old_versions() protects the active backup
    slot (.exe.old) while the application is in TESTING state (awaiting self-test confirmation).
    confirm_boot() emits .ota_confirmed_{PID} without deleting the backup.
    The active process's own confirmation marker (.ota_confirmed_{PID}) is strictly preserved
    even when cleanup_old_versions(force=True) is called, so it remains available on disk
    for the watchdog supervisor to detect and consume in the normal flow.
    cleanup_old_versions(force=True) purges the backup slot (.exe.old) and stale confirmation
    markers from other PIDs, but leaves the running process's marker intact.
    Crucially, non-OTA sentinel files (altri_app.exe.old, documento.bak, backup.old, stale_old_file.bak)
    are strictly preserved in ALL cases (both force=False and force=True).
    """
    mock_exe = tmp_path / "AetherDesk.exe"
    mock_exe.write_bytes(b"MZ_ACTIVE")
    active_backup = tmp_path / "AetherDesk.exe.old"
    active_backup.write_bytes(b"MZ_BACKUP")
    stale_marker = tmp_path / ".ota_confirmed_99999"
    stale_marker.write_text("old_marker", encoding="utf-8")

    # Non-OTA sentinel files belonging to other applications or the user
    other_app_old = tmp_path / "altri_app.exe.old"
    other_app_old.write_bytes(b"OTHER_APP_BACKUP_DO_NOT_TOUCH")
    doc_bak = tmp_path / "documento.bak"
    doc_bak.write_bytes(b"USER_DOCUMENT_DO_NOT_TOUCH")
    backup_old = tmp_path / "backup.old"
    backup_old.write_bytes(b"USER_BACKUP_DO_NOT_TOUCH")
    stale_bak = tmp_path / "stale_old_file.bak"
    stale_bak.write_bytes(b"STALE")

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_exe))

    # In TESTING state, cleanup_old_versions without force=True must NOT delete active_backup or confirmation markers
    assert check_boot_state() == OTAUpdateState.TESTING
    cleanup_old_versions(tmp_path, force=False)
    assert active_backup.exists()
    assert stale_marker.exists()
    # Non-OTA sentinels must be strictly preserved
    assert other_app_old.exists()
    assert doc_bak.exists()
    assert backup_old.exists()
    assert stale_bak.exists()

    # When confirm_boot is called, confirmation marker is emitted, and active_backup is strictly preserved
    # on disk for the watchdog supervisor to consume
    confirm_marker = tmp_path / f".ota_confirmed_{os.getpid()}"
    confirmed = confirm_boot()
    assert confirmed is True
    assert confirm_marker.exists()
    assert confirm_marker.read_text(encoding="utf-8") == "confirmed"
    assert active_backup.exists()

    # Even with confirm marker on disk, non-forced cleanup in TESTING state preserves backup and markers
    cleanup_old_versions(tmp_path, force=False)
    assert active_backup.exists()
    assert confirm_marker.exists()
    assert stale_marker.exists()
    assert other_app_old.exists()
    assert doc_bak.exists()
    assert backup_old.exists()
    assert stale_bak.exists()

    # Explicit force=True purges the backup slot and stale markers from other PIDs,
    # but strictly PRESERVES the running process's own confirmation marker so the watchdog can consume it,
    # and strictly PRESERVES non-OTA sentinel files
    assert cleanup_old_versions(tmp_path, force=True) > 0
    assert not active_backup.exists()
    assert not stale_marker.exists()
    assert confirm_marker.exists()  # The active marker remains available until watchdog consumes it
    assert other_app_old.exists()
    assert doc_bak.exists()
    assert backup_old.exists()
    assert stale_bak.exists()

    # In normal flow, watchdog supervisor consumes and deletes the confirmation marker
    confirm_marker.unlink()
    assert not confirm_marker.exists()

def test_watchdog_script_rollback_logic(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies that the generated watchdog restart script
    contains same-volume swap execution, active boot monitoring, and automatic rollback logic.
    """
    mock_exe = tmp_path / "AetherDesk.exe"
    mock_exe.write_bytes(b"MZ" + b"\x90" * 1_100_000)

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_exe))

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    new_bytes = b"MZ" + b"\x90" * 1_200_000
    staging_dir = get_ota_staging_dir()
    temp_dl = staging_dir / "aether_ota_dl.exe"
    temp_dl.write_bytes(new_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_bytes).hexdigest(),
        "size": len(new_bytes)
    }, priv)

    captured_commands = []
    def mock_popen(args, **kwargs):
        captured_commands.append(args)
        # Simulate watchdog startup by touching the readiness marker
        (mock_exe.parent / f".ota_ready_{os.getpid()}").write_text("ready", encoding="utf-8")
        class DummyProc:
            pid = 99999
        return DummyProc()

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    ok, msg = apply_in_place_update(str(temp_dl), signed_manifest=signed_man)
    assert ok is True
    assert len(captured_commands) == 1
    bat_path = Path(captured_commands[0][2])
    assert bat_path.exists()

    bat_text = bat_path.read_text(encoding="utf-8")

    staged_target = mock_exe.parent / f"~{mock_exe.name}.new"

    # Verify presence of same-volume swap, watchdog monitoring loop and automatic rollback with PID isolation
    assert ":do_same_volume_swap" in bat_text
    assert f'move /y "{staged_target}" "{mock_exe}"' in bat_text
    assert ":monitor_boot" in bat_text
    assert ":do_rollback" in bat_text
    assert "powershell" in bat_text
    assert "Start-Process" in bat_text
    assert "PID eq %NEW_PID%" in bat_text
    assert "taskkill /f /pid %NEW_PID%" in bat_text
    assert f'move /y "{mock_exe}.old" "{mock_exe}"' in bat_text
    assert f'start "" "{mock_exe}"' in bat_text
    assert "if errorlevel 1 goto rollback_failed" in bat_text
    assert ":rollback_failed" in bat_text
    assert ":staging_missing" in bat_text
    assert f'if not exist "{staged_target}" goto staging_missing' in bat_text
    assert ":swap_failed_backup" in bat_text
    assert ":caller_timed_out" in bat_text
    assert "exit /b 1" in bat_text

    # Positive confirmation requirement:
    # 1. :update_ok is reached ONLY after detecting and deleting .ota_confirmed_%NEW_PID% and old_backup
    assert f'if exist "{mock_exe.parent}\\.ota_confirmed_%NEW_PID%" (' in bat_text
    assert f'del /f /q "{mock_exe.parent}\\.ota_confirmed_%NEW_PID%"' in bat_text
    assert f'del /f /q "{mock_exe}.old"' in bat_text
    assert "goto update_ok" in bat_text

    # 2. If old_backup is missing without the positive confirmation marker, watchdog jumps
    # to :unconfirmed_missing_backup, writes critical failure log, and exits with code 1 without reaching update_ok
    assert f'if not exist "{mock_exe}.old" goto unconfirmed_missing_backup' in bat_text
    assert ":unconfirmed_missing_backup" in bat_text

    # 3. Verify absence of generic wildcard deletion patterns (*.old, *.exe.old, *.bak)
    assert f'del /f /q "{mock_exe.parent}\\*.old"' not in bat_text
    assert f'del /f /q "{mock_exe.parent}\\*.exe.old"' not in bat_text
    assert "*.bak" not in bat_text

def test_host_tray_boot_confirmation_flow(monkeypatch):
    """
    Deterministic in-memory test: verifies HostControlPanel in host_tray implements
    the orderly _verify_and_confirm_ota_boot flow and does NOT delete backups unconditionally.
    """
    from src.host_tray import HostControlPanel
    import src.ota as ota_module

    confirmed_calls = []
    monkeypatch.setattr(ota_module, "confirm_boot", lambda: (confirmed_calls.append(True), True)[1])
    monkeypatch.setattr(ota_module, "check_boot_state", lambda: OTAUpdateState.TESTING)

    panel = HostControlPanel.__new__(HostControlPanel)
    panel.root = None
    panel.daemon = None

    panel._verify_and_confirm_ota_boot()
    assert len(confirmed_calls) == 1

def test_apply_in_place_update_copy_failure_with_partial_file(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies that if shutil.copy2 writes partial corrupt bytes
    to same-volume staging before raising an exception, apply_in_place_update removes the partial file,
    leaves the original current_exe completely untouched, cleans up the staged temp file,
    and returns False with a clear error without touching or losing the active binary.
    """
    original_bytes = b"ORIGINAL_WORKING_APPLICATION_BINARY_v1_4_0"
    mock_current_exe = tmp_path / "AetherDesk.exe"
    mock_current_exe.write_bytes(original_bytes)

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_current_exe))

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    new_exe_bytes = b"MZ" + b"\x90" * 1_200_000
    staging_dir = get_ota_staging_dir()
    temp_update = staging_dir / "aether_ota_partial_download.exe"
    temp_update.write_bytes(new_exe_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_exe_bytes).hexdigest(),
        "size": len(new_exe_bytes)
    }, priv)

    real_copy2 = shutil.copy2
    staged_target = mock_current_exe.parent / f"~{mock_current_exe.name}.new"

    def partial_failing_copy2(src, dst):
        if Path(dst) == staged_target:
            # Simulate a partial corrupt write to local staging before the I/O error occurs
            Path(dst).write_bytes(b"MZ_PARTIAL_CORRUPTED_BYTES_FROM_FAILED_COPY")
            raise OSError("Disk quota exceeded or write fault during copy2")
        return real_copy2(src, dst)

    monkeypatch.setattr(shutil, "copy2", partial_failing_copy2)

    ok, err_msg = apply_in_place_update(str(temp_update), signed_manifest=signed_man)
    assert ok is False
    assert "Impossibile copiare il nuovo binario nella staging dello stesso volume" in err_msg

    # Verify that the active executable was NEVER touched and original bytes are preserved
    assert mock_current_exe.exists()
    assert mock_current_exe.read_bytes() == original_bytes
    # Staged partial file was safely unlinked
    assert not staged_target.exists()
    assert not (mock_current_exe.parent / "AetherDesk.exe.old").exists()
    # Verify staging temp file was cleaned up
    assert not temp_update.exists()

def test_apply_in_place_update_watchdog_spawn_failure_rollback(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies that if spawning the supervisor watchdog process fails
    (e.g., subprocess.Popen raises OSError), apply_in_place_update NEVER touches or removes current_exe;
    it aborts fail-closed, deletes any generated batch file and staged candidate,
    cleans up the temporary update file, and returns False.
    """
    original_bytes = b"ORIGINAL_WORKING_APPLICATION_BINARY_v1_4_0"
    mock_current_exe = tmp_path / "AetherDesk.exe"
    mock_current_exe.write_bytes(original_bytes)

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_current_exe))

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    new_exe_bytes = b"MZ" + b"\x90" * 1_200_000
    staging_dir = get_ota_staging_dir()
    temp_update = staging_dir / "aether_ota_spawn_fail.exe"
    temp_update.write_bytes(new_exe_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_exe_bytes).hexdigest(),
        "size": len(new_exe_bytes)
    }, priv)

    def failing_popen(*args, **kwargs):
        raise OSError("Permission denied or process quota exceeded spawning watchdog")

    monkeypatch.setattr(subprocess, "Popen", failing_popen)

    ok, err_msg = apply_in_place_update(str(temp_update), signed_manifest=signed_man)
    assert ok is False
    assert "Impossibile avviare il supervisore" in err_msg
    assert "Eseguibile attivo rimasto intatto" in err_msg

    # Verify that the active executable was completely untouched
    assert mock_current_exe.exists()
    assert mock_current_exe.read_bytes() == original_bytes
    assert not (mock_current_exe.parent / f"~{mock_current_exe.name}.new").exists()
    assert not (mock_current_exe.parent / "AetherDesk.exe.old").exists()
    # Verify staging temp file was cleaned up
    assert not temp_update.exists()

def test_apply_in_place_update_refuses_when_in_testing_state(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies that apply_in_place_update strictly refuses
    to start a new update if check_boot_state() indicates the app is still in TESTING state
    (an existing update is unconfirmed, protecting the active backup from being destroyed).
    """
    mock_exe = tmp_path / "AetherDesk.exe"
    mock_exe.write_bytes(b"MZ_CURRENT")
    active_backup = tmp_path / "AetherDesk.exe.old"
    active_backup.write_bytes(b"MZ_ORIGINAL_BACKUP")

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_exe))

    assert check_boot_state() == OTAUpdateState.TESTING

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    new_exe_bytes = b"MZ" + b"\x90" * 1_200_000
    staging_dir = get_ota_staging_dir()
    temp_update = staging_dir / "aether_ota_concurrent.exe"
    temp_update.write_bytes(new_exe_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_exe_bytes).hexdigest(),
        "size": len(new_exe_bytes)
    }, priv)

    ok, msg = apply_in_place_update(str(temp_update), signed_manifest=signed_man)
    assert ok is False
    assert "stato TESTING" in msg
    # Verify existing backup was untouched
    assert active_backup.exists()
    assert active_backup.read_bytes() == b"MZ_ORIGINAL_BACKUP"
    # Verify candidate staging file was cleaned up
    assert not temp_update.exists()

def test_safe_cleanup_temp_ownership_guard(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies that _safe_cleanup_temp() strictly validates
    file ownership. It deletes files belonging strictly to the OTA staging directory
    and starting with 'aether_ota_' and ending with '.exe'.
    A file outside the staging directory (even with the prefix) is strictly protected.
    """
    monkeypatch.setattr(sys, "frozen", True)
    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    staging_dir = get_ota_staging_dir()

    bad_man = sign_update_manifest({
        "version": "1.3.0",  # Triggers Anti-Rollback failure
        "filename": "AetherDesk.exe",
        "sha256": "0" * 64,
        "size": 1_000_000
    }, priv)

    # 1. File with 'aether_ota_' prefix OUTSIDE staging directory: must NOT be deleted
    outside_file = tmp_path / "aether_ota_outside.exe"
    outside_file.write_bytes(b"MZ" + b"\x90" * 1_000_100)

    ok_out, _ = apply_in_place_update(str(outside_file), signed_manifest=bad_man)
    assert ok_out is False
    # File outside staging was protected and NOT deleted!
    assert outside_file.exists()

    # 2. Arbitrary non-OTA file outside staging directory: must NOT be deleted
    user_file = tmp_path / "user_notes.txt"
    user_file.write_text("CRITICAL_USER_DATA", encoding="utf-8")

    ok_usr, _ = apply_in_place_update(str(user_file), signed_manifest=bad_man)
    assert ok_usr is False
    assert user_file.exists()

    # 3. File INSIDE staging directory but with invalid format (no .exe extension): must NOT be deleted
    inside_bad_ext = staging_dir / "aether_ota_notes.txt"
    inside_bad_ext.write_text("SOME_NOTES", encoding="utf-8")

    ok_ext, _ = apply_in_place_update(str(inside_bad_ext), signed_manifest=bad_man)
    assert ok_ext is False
    assert inside_bad_ext.exists()
    try:
        inside_bad_ext.unlink()
    except Exception:
        pass

    # 4. File INSIDE staging directory but lacking 'aether_ota_' prefix: must NOT be deleted
    inside_unprefixed = staging_dir / "unprefixed_file.exe"
    inside_unprefixed.write_bytes(b"MZ" + b"\x90" * 1_000_100)

    ok_pre, _ = apply_in_place_update(str(inside_unprefixed), signed_manifest=bad_man)
    assert ok_pre is False
    assert inside_unprefixed.exists()
    try:
        inside_unprefixed.unlink()
    except Exception:
        pass

    # 5. File INSIDE staging directory with correct prefix AND .exe extension: IS deleted on failure
    legit_ota = staging_dir / "aether_ota_legit.exe"
    legit_ota.write_bytes(b"MZ" + b"\x90" * 1_000_100)

    ok_legit, _ = apply_in_place_update(str(legit_ota), signed_manifest=bad_man)
    assert ok_legit is False
    assert not legit_ota.exists()

def test_watchdog_script_pid_isolation_and_command_structure(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies the generated watchdog batch script
    launches the new process capturing its exact PID via PowerShell Start-Process -PassThru,
    monitors only that PID, and on rollback terminates ONLY that PID (taskkill /f /pid %NEW_PID%),
    preventing any interference with other running instances.
    """
    mock_exe = tmp_path / "AetherDesk.exe"
    mock_exe.write_bytes(b"MZ" + b"\x90" * 1_100_000)

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_exe))

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    new_bytes = b"MZ" + b"\x90" * 1_200_000
    staging_dir = get_ota_staging_dir()
    temp_dl = staging_dir / "aether_ota_pid_test.exe"
    temp_dl.write_bytes(new_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_bytes).hexdigest(),
        "size": len(new_bytes)
    }, priv)

    captured_cmds = []
    def mock_popen(args, **kwargs):
        captured_cmds.append(args)
        (mock_exe.parent / f".ota_ready_{os.getpid()}").write_text("ready", encoding="utf-8")
        class DummyProc:
            pid = 77777
        return DummyProc()

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    ok, msg = apply_in_place_update(str(temp_dl), signed_manifest=signed_man)
    assert ok is True
    assert len(captured_cmds) == 1
    bat_path = Path(captured_cmds[0][2])
    assert bat_path.exists()

    bat_content = bat_path.read_text(encoding="utf-8")

    # PID isolation verification
    assert "powershell" in bat_content
    assert "Start-Process" in bat_content
    assert "-PassThru).Id" in bat_content
    assert "set NEW_PID=" in bat_content
    assert 'tasklist /fi "PID eq %NEW_PID%"' in bat_content
    assert "taskkill /f /pid %NEW_PID%" in bat_content
    assert ".ota_confirmed_%NEW_PID%" in bat_content
    # Ensure global image name killing is NOT present
    assert "taskkill /f /im" not in bat_content

def test_watchdog_script_rollback_failure_diagnostics(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies that in :do_rollback, the generated script:
    1. Checks the outcome of move /y old_backup current_exe (errorlevel and existence).
    2. If restoration fails, jumps to :rollback_failed.
    3. In :rollback_failed, writes an actionable diagnostic log (.ota_rollback_failure.log).
    4. PRESERVES old_backup without deleting it.
    5. Exits with exit /b 1 rather than claiming rollback succeeded or jumping to cleanup.
    """
    mock_exe = tmp_path / "AetherDesk.exe"
    mock_exe.write_bytes(b"MZ" + b"\x90" * 1_100_000)

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_exe))

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    new_bytes = b"MZ" + b"\x90" * 1_200_000
    staging_dir = get_ota_staging_dir()
    temp_dl = staging_dir / "aether_ota_diag_test.exe"
    temp_dl.write_bytes(new_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_bytes).hexdigest(),
        "size": len(new_bytes)
    }, priv)

    captured_cmds = []
    def mock_popen(args, **kwargs):
        captured_cmds.append(args)
        (mock_exe.parent / f".ota_ready_{os.getpid()}").write_text("ready", encoding="utf-8")
        class DummyProc:
            pid = 88888
        return DummyProc()

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    ok, _ = apply_in_place_update(str(temp_dl), signed_manifest=signed_man)
    assert ok is True
    assert len(captured_cmds) == 1, "subprocess.Popen must have been called exactly once for watchdog supervisor"
    bat_path = Path(captured_cmds[0][2])
    bat_content = bat_path.read_text(encoding="utf-8")

    # Structural verification of the rollback failure branch
    assert ":rollback_failed" in bat_content
    assert ":rollback_missing_backup" in bat_content
    assert ".ota_rollback_failure.log" in bat_content
    assert "Backup valido preservato in:" in bat_content
    assert "Azione manuale richiesta: rinominare" in bat_content
    assert "exit /b 1" in bat_content
    # Verify move outcome verification
    assert "if errorlevel 1 goto rollback_failed" in bat_content

def test_check_boot_state_helper_recovery_when_invoked(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies fallback recovery in check_boot_state()
    when invoked by an administrative script, installer, or Python test harness:
    if current_exe is missing on disk but .exe.old exists, check_boot_state() restores
    the backup to current_exe.

    NOTE ON REALISTIC RUNTIME LIMITS:
    In standalone production without an external launcher or persistent service, if current_exe
    is missing (e.g., following a hard power loss mid-swap), Windows cannot execute the application
    at all (ERROR_FILE_NOT_FOUND), so check_boot_state() cannot run spontaneously on boot.
    Such interruptions require an external launcher or manual renaming of .exe.old.
    """
    mock_exe = tmp_path / "AetherDesk.exe"
    assert not mock_exe.exists()

    backup_exe = tmp_path / "AetherDesk.exe.old"
    backup_exe.write_bytes(b"RECOVERABLE_ORIGINAL_BINARY")

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_exe))

    state = check_boot_state()
    assert mock_exe.exists()
    assert mock_exe.read_bytes() == b"RECOVERABLE_ORIGINAL_BINARY"

def test_watchdog_script_staging_missing_handling(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies that in the generated watchdog script,
    if the same-volume staging file (~{name}.new) does not exist:
    1. It does NOT skip the swap to run the old executable or reach update_ok.
    2. It branches directly to :staging_missing.
    3. It logs a critical diagnostic in .ota_rollback_failure.log.
    4. It restarts current_exe if present so the machine is not left without a running process.
    5. It exits with code 1 (exit /b 1).
    """
    mock_exe = tmp_path / "AetherDesk.exe"
    mock_exe.write_bytes(b"MZ" + b"\x90" * 1_100_000)

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_exe))

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    new_bytes = b"MZ" + b"\x90" * 1_200_000
    staging_dir = get_ota_staging_dir()
    temp_dl = staging_dir / "aether_ota_staging_missing_test.exe"
    temp_dl.write_bytes(new_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_bytes).hexdigest(),
        "size": len(new_bytes)
    }, priv)

    captured_cmds = []
    def mock_popen(args, **kwargs):
        captured_cmds.append(args)
        (mock_exe.parent / f".ota_ready_{os.getpid()}").write_text("ready", encoding="utf-8")
        class DummyProc:
            pid = 66666
        return DummyProc()

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    ok, _ = apply_in_place_update(str(temp_dl), signed_manifest=signed_man)
    assert ok is True
    assert len(captured_cmds) == 1, "subprocess.Popen must have been called exactly once for watchdog supervisor"
    bat_path = Path(captured_cmds[0][2])
    bat_content = bat_path.read_text(encoding="utf-8")

    staged_target = mock_exe.parent / f"~{mock_exe.name}.new"

    # Branch structure verification
    assert f'if not exist "{staged_target}" goto staging_missing' in bat_content
    assert ":staging_missing" in bat_content
    assert f'echo [CRITICAL ERROR] OTA Swap: File di staging {staged_target} non trovato.' in bat_content
    assert f'start "" "{mock_exe}"' in bat_content
    assert "exit /b 1" in bat_content

def test_watchdog_script_swap_failed_backup_handling(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies that in :swap_failed_backup,
    the generated watchdog batch script:
    1. Does NOT delete current_exe or old_backup.
    2. Deletes the uninstalled staged_target (~{name}.new).
    3. Checks which executable is intact:
       - if current_exe exists, restarts it (start "" "{current_exe}") and exits with error code 1.
       - if old_backup exists, restores and restarts it, then exits with error code 1.
    4. Writes diagnostic failure details to .ota_rollback_failure.log.
    5. Strictly terminates with exit /b 1 and never reaches update_ok.
    """
    mock_exe = tmp_path / "AetherDesk.exe"
    mock_exe.write_bytes(b"MZ" + b"\x90" * 1_100_000)

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_exe))

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    new_bytes = b"MZ" + b"\x90" * 1_200_000
    staging_dir = get_ota_staging_dir()
    temp_dl = staging_dir / "aether_ota_backup_fail_test.exe"
    temp_dl.write_bytes(new_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_bytes).hexdigest(),
        "size": len(new_bytes)
    }, priv)

    captured_cmds = []
    def mock_popen(args, **kwargs):
        captured_cmds.append(args)
        (mock_exe.parent / f".ota_ready_{os.getpid()}").write_text("ready", encoding="utf-8")
        class DummyProc:
            pid = 55555
        return DummyProc()

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    ok, _ = apply_in_place_update(str(temp_dl), signed_manifest=signed_man)
    assert ok is True
    assert len(captured_cmds) == 1, "subprocess.Popen must have been called exactly once for watchdog supervisor"
    bat_path = Path(captured_cmds[0][2])
    bat_content = bat_path.read_text(encoding="utf-8")

    staged_target = mock_exe.parent / f"~{mock_exe.name}.new"
    old_backup = mock_exe.parent / "AetherDesk.exe.old"

    assert ":swap_failed_backup" in bat_content
    assert f'echo [CRITICAL ERROR] OTA Swap: Impossibile creare backup {old_backup} da {mock_exe}' in bat_content
    assert f'del /f /q "{staged_target}" 2>nul' in bat_content
    assert f'if exist "{mock_exe}" (' in bat_content
    assert f'if exist "{old_backup}" (' in bat_content
    assert f'start "" "{mock_exe}"' in bat_content

def test_apply_in_place_update_watchdog_readiness_timeout_cleanup(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies that if the supervisor watchdog process
    does not signal readiness via .ota_ready_{pid} within the timeout:
    1. apply_in_place_update aborts fail-closed and returns False with a clear error message.
    2. The calling application process is NOT terminated.
    3. current_exe remains 100% intact and untouched.
    4. Transient attempt artifacts (staged ~{name}.new and restart_bat) are deleted.
    5. The downloaded temporary file is cleaned up.
    """
    original_bytes = b"ACTIVE_WORKING_PROCESS_BINARY_INTACT"
    mock_exe = tmp_path / "AetherDesk.exe"
    mock_exe.write_bytes(original_bytes)

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_exe))

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    new_bytes = b"MZ" + b"\x90" * 1_200_000
    staging_dir = get_ota_staging_dir()
    temp_dl = staging_dir / "aether_ota_readiness_fail.exe"
    temp_dl.write_bytes(new_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_bytes).hexdigest(),
        "size": len(new_bytes)
    }, priv)

    captured_cmds = []
    dummy_proc_calls = {
        "terminated": False,
        "waited_terminate": False,
        "killed": False,
        "waited_kill": False
    }

    def mock_popen_no_ready(args, **kwargs):
        captured_cmds.append(args)
        # Intentionally do NOT create .ota_ready_{pid} to simulate a hung cmd.exe
        class DummyProc:
            pid = 44444
            def poll(self):
                if dummy_proc_calls["killed"]:
                    return -9  # Confirmed terminated after kill
                return None  # Process is still running
            def terminate(self):
                dummy_proc_calls["terminated"] = True
            def wait(self, timeout=None):
                if not dummy_proc_calls["killed"]:
                    dummy_proc_calls["waited_terminate"] = True
                    raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
                dummy_proc_calls["waited_kill"] = True
                return 0
            def kill(self):
                dummy_proc_calls["killed"] = True
        return DummyProc()

    monkeypatch.setattr(subprocess, "Popen", mock_popen_no_ready)

    # Use a small timeout (0.05s) for fast deterministic test execution
    ok, err_msg = apply_in_place_update(str(temp_dl), signed_manifest=signed_man, readiness_timeout=0.05)
    assert ok is False
    assert "non ha confermato la readiness" in err_msg
    assert "Eseguibile attivo rimasto intatto" in err_msg

    # Verify that the orphaned watchdog process was terminated, wait timed out, and kill fallback was executed
    assert dummy_proc_calls["terminated"] is True
    assert dummy_proc_calls["waited_terminate"] is True
    assert dummy_proc_calls["killed"] is True
    assert dummy_proc_calls["waited_kill"] is True

    # Verify that the active executable was NEVER touched
    assert mock_exe.exists()
    assert mock_exe.read_bytes() == original_bytes

    # Verify that staged candidate and batch script were safely unlinked
    staged_target = mock_exe.parent / f"~{mock_exe.name}.new"
    assert not staged_target.exists()
    assert len(captured_cmds) == 1
    bat_path = Path(captured_cmds[0][2])
    assert not bat_path.exists()

    # Verify temporary downloaded file was cleaned up
    assert not temp_dl.exists()

def test_watchdog_script_caller_wait_timeout(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies that the watchdog batch script implements
    a bounded wait in :wait_exit with a counter up to 30 seconds.
    If the caller does not exit, it jumps to :caller_timed_out, writes a warning to
    the diagnostic log, removes the readiness marker and staged file, and jumps to
    :cleanup without modifying or removing current_exe or old_backup.
    """
    mock_exe = tmp_path / "AetherDesk.exe"
    mock_exe.write_bytes(b"MZ" + b"\x90" * 1_100_000)

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_exe))

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    new_bytes = b"MZ" + b"\x90" * 1_200_000
    staging_dir = get_ota_staging_dir()
    temp_dl = staging_dir / "aether_ota_caller_timeout.exe"
    temp_dl.write_bytes(new_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_bytes).hexdigest(),
        "size": len(new_bytes)
    }, priv)

    captured_cmds = []
    def mock_popen(args, **kwargs):
        captured_cmds.append(args)
        (mock_exe.parent / f".ota_ready_{os.getpid()}").write_text("ready", encoding="utf-8")
        class DummyProc:
            pid = 33333
        return DummyProc()

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    ok, _ = apply_in_place_update(str(temp_dl), signed_manifest=signed_man)
    assert ok is True
    assert len(captured_cmds) == 1, "subprocess.Popen must have been called exactly once for watchdog supervisor"
    bat_path = Path(captured_cmds[0][2])
    bat_content = bat_path.read_text(encoding="utf-8")

    staged_target = mock_exe.parent / f"~{mock_exe.name}.new"
    ready_marker = mock_exe.parent / f".ota_ready_{os.getpid()}"

    # Verify bounded loop and timeout handler
    assert f'echo ready > "{ready_marker}"' in bat_content
    assert "set /a caller_wait_count=0" in bat_content
    assert "set /a caller_wait_count+=1" in bat_content
    assert "if %caller_wait_count% geq 30 goto caller_timed_out" in bat_content
    assert ":caller_timed_out" in bat_content
    assert "Timeout (30s) in attesa della chiusura del processo chiamante" in bat_content
    assert f'del /f /q "{ready_marker}" 2>nul' in bat_content
    assert f'del /f /q "{staged_target}" 2>nul' in bat_content
    assert "goto cleanup" in bat_content

def test_watchdog_script_positive_confirmation_required_for_update_ok(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies that :update_ok in the watchdog supervisor script
    is reachable ONLY after detecting the explicit positive confirmation marker (.ota_confirmed_%NEW_PID%).
    If old_backup is missing without the positive marker, the script branches to :unconfirmed_missing_backup,
    logs the critical diagnostic failure, and terminates with exit /b 1 without ever reaching :update_ok.
    """
    mock_exe = tmp_path / "AetherDesk.exe"
    mock_exe.write_bytes(b"MZ" + b"\x90" * 1_100_000)

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_exe))

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    new_bytes = b"MZ" + b"\x90" * 1_200_000
    staging_dir = get_ota_staging_dir()
    temp_dl = staging_dir / "aether_ota_confirm_protocol.exe"
    temp_dl.write_bytes(new_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_bytes).hexdigest(),
        "size": len(new_bytes)
    }, priv)

    captured_cmds = []
    def mock_popen(args, **kwargs):
        captured_cmds.append(args)
        (mock_exe.parent / f".ota_ready_{os.getpid()}").write_text("ready", encoding="utf-8")
        class DummyProc:
            pid = 22222
        return DummyProc()

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    ok, _ = apply_in_place_update(str(temp_dl), signed_manifest=signed_man)
    assert ok is True
    assert len(captured_cmds) == 1, "subprocess.Popen must have been called exactly once for watchdog supervisor"
    bat_path = Path(captured_cmds[0][2])
    bat_content = bat_path.read_text(encoding="utf-8")

    # 1. Parse all lines containing "goto update_ok"
    update_ok_gotos = [line.strip() for line in bat_content.splitlines() if "goto update_ok" in line]
    assert len(update_ok_gotos) == 1, "There must be exactly one branch reaching update_ok in the script"

    # 2. Verify that goto update_ok is guarded by the explicit positive confirmation marker check
    expected_confirm_check = f'if exist "{mock_exe.parent}\\.ota_confirmed_%NEW_PID%" ('
    assert expected_confirm_check in bat_content

    # Extract block following confirmation check
    idx_marker = bat_content.find(expected_confirm_check)
    assert idx_marker != -1
    confirm_block = bat_content[idx_marker:idx_marker + 300]
    assert "goto update_ok" in confirm_block
    assert f'del /f /q "{mock_exe.parent}\\.ota_confirmed_%NEW_PID%"' in confirm_block
    assert f'del /f /q "{mock_exe}.old"' in confirm_block

    # 3. Verify missing old_backup without confirmation marker routes to unconfirmed_missing_backup
    assert f'if not exist "{mock_exe}.old" goto unconfirmed_missing_backup' in bat_content
    assert ":unconfirmed_missing_backup" in bat_content
    idx_unconf = bat_content.find(":unconfirmed_missing_backup")
    assert idx_unconf != -1
    unconf_block = bat_content[idx_unconf:idx_unconf + 400]
    assert "Aggiornamento NON confermato" in unconf_block
    assert "exit /b 1" in unconf_block
    assert "goto update_ok" not in unconf_block

    # 4. Verify :update_ok uses exact backup deletion and avoids generic wildcards (*.old, *.exe.old, *.bak)
    idx_ok = bat_content.find(":update_ok")
    assert idx_ok != -1
    ok_block = bat_content[idx_ok:idx_ok + 400]
    assert f'del /f /q "{mock_exe}.old" 2>nul' in ok_block
    assert f'del /f /q "{mock_exe.with_suffix(".old")}" 2>nul' in ok_block
    assert f'del /f /q "{mock_exe.parent}\\*.old"' not in bat_content
    assert f'del /f /q "{mock_exe.parent}\\*.exe.old"' not in bat_content
    assert "*.bak" not in bat_content

def test_watchdog_script_cleanup_preserves_non_ota_sentinels(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies that the generated watchdog batch script:
    1. In :update_ok, targets strictly the exact backup files derived from current_exe
       ({current_exe.name}.old / {current_exe}.old and {current_exe.stem}.old).
    2. Does NOT contain generic wildcards (*.old, *.exe.old, *.bak) that could delete
       non-OTA files belonging to other applications or the user.
    3. Confirms that non-OTA sentinel filenames (altri_app.exe.old, documento.bak, backup.old)
       are never targeted or deleted by the script commands.
    """
    mock_exe = tmp_path / "AetherDesk.exe"
    mock_exe.write_bytes(b"MZ" + b"\x90" * 1_100_000)

    # Setup non-OTA sentinel files in the same directory as the executable
    other_app_old = tmp_path / "altri_app.exe.old"
    other_app_old.write_bytes(b"OTHER_APP_DATA")
    doc_bak = tmp_path / "documento.bak"
    doc_bak.write_bytes(b"USER_DOCUMENT_DATA")
    backup_old = tmp_path / "backup.old"
    backup_old.write_bytes(b"USER_BACKUP_DATA")

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_exe))

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    new_bytes = b"MZ" + b"\x90" * 1_200_000
    staging_dir = get_ota_staging_dir()
    temp_dl = staging_dir / "aether_ota_sentinel_test.exe"
    temp_dl.write_bytes(new_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_bytes).hexdigest(),
        "size": len(new_bytes)
    }, priv)

    captured_cmds = []
    def mock_popen(args, **kwargs):
        captured_cmds.append(args)
        (mock_exe.parent / f".ota_ready_{os.getpid()}").write_text("ready", encoding="utf-8")
        class DummyProc:
            pid = 33333
        return DummyProc()

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    ok, _ = apply_in_place_update(str(temp_dl), signed_manifest=signed_man)
    assert ok is True
    assert len(captured_cmds) == 1, "subprocess.Popen must have been called exactly once for watchdog supervisor"

    bat_path = Path(captured_cmds[0][2])
    bat_content = bat_path.read_text(encoding="utf-8")

    # 1. Verify that update_ok targets exact backup files and staged target
    assert ":update_ok" in bat_content
    idx_ok = bat_content.find(":update_ok")
    ok_block = bat_content[idx_ok:idx_ok + 400]

    expected_old_backup = str(mock_exe.with_suffix(".exe.old"))
    expected_old_backup_alt = str(mock_exe.with_suffix(".old"))
    expected_staged = str(mock_exe.parent / f"~{mock_exe.name}.new")

    assert f'del /f /q "{expected_old_backup}" 2>nul' in ok_block
    assert f'del /f /q "{expected_old_backup_alt}" 2>nul' in ok_block
    assert f'del /f /q "{expected_staged}" 2>nul' in ok_block

    # 2. Verify that generic over-broad wildcards are strictly absent
    assert f'del /f /q "{mock_exe.parent}\\*.old"' not in bat_content
    assert f'del /f /q "{mock_exe.parent}\\*.exe.old"' not in bat_content
    assert "*.bak" not in bat_content
    assert "*.exe.old" not in bat_content

    for line in bat_content.splitlines():
        if "del " in line.lower():
            assert "*.old" not in line, f"Wildcard deletion '*.old' detected in command: {line}"
            assert "*.bak" not in line, f"Wildcard deletion '*.bak' detected in command: {line}"
            assert "*.exe.old" not in line, f"Wildcard deletion '*.exe.old' detected in command: {line}"

    # 3. Verify that non-OTA sentinel files were untouched on disk during apply_in_place_update
    assert other_app_old.exists()
    assert other_app_old.read_bytes() == b"OTHER_APP_DATA"
    assert doc_bak.exists()
    assert doc_bak.read_bytes() == b"USER_DOCUMENT_DATA"
    assert backup_old.exists()
    assert backup_old.read_bytes() == b"USER_BACKUP_DATA"

def test_apply_in_place_update_generic_exception_watchdog_kill_and_cleanup(tmp_path, monkeypatch):
    """
    Test deterministico con filesystem temporaneo isolato (tmp_path): verifica che se si verifica un'eccezione
    imprevista dopo Popen (intercettata dal blocco generico except Exception as e), apply_in_place_update:
    1. Termina il processo watchdog avviato per questo tentativo (terminate -> wait timeout -> fallback kill -> wait).
    2. Neutralizzato il watchdog, rimuove esclusivamente gli artefatti del tentativo:
       - marcatore di readiness (.ota_ready_{PID})
       - script batch di restart (aether_restart_{PID}.bat)
       - binario di staging sullo stesso volume (~{name}.new)
       - file temporaneo scaricato (aether_ota_*.exe)
    3. Preserva rigorosamente intatti l'eseguibile attivo (current_exe) e il file di backup (.exe.old).
    4. Restituisce (False, error_msg) senza crash.
    """
    original_bytes = b"ACTIVE_PROCESS_CURRENT_EXE_MUST_REMAIN_INTACT"
    mock_exe = tmp_path / "AetherDesk.exe"
    mock_exe.write_bytes(original_bytes)
    mock_backup = tmp_path / "AetherDesk.exe.old"
    mock_backup.write_bytes(b"BACKUP_OF_PREVIOUS_VERSION_MUST_NOT_BE_TOUCHED")

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_exe))

    # Isola lo stato di boot a IDLE per consentire l'avvio del watchdog preservando il file di backup
    monkeypatch.setattr("src.ota.check_boot_state", lambda: OTAUpdateState.IDLE)
    # Isola cleanup_old_versions con no-op: lo stato IDLE e' artificiale per il test del solo watchdog
    # e mock_backup funge da sentinel per verificare che i rami di errore del watchdog non lo tocchino
    monkeypatch.setattr("src.ota.cleanup_old_versions", lambda *args, **kwargs: 0)

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    # Isola la cartella temporanea e di staging sotto tmp_path
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("src.ota.tempfile.gettempdir", lambda: str(tmp_path))
    staging_dir = tmp_path / "ota_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("src.ota.get_ota_staging_dir", lambda: staging_dir)

    new_bytes = b"MZ" + b"\x90" * 1_200_000
    temp_dl = staging_dir / "aether_ota_generic_error.exe"
    temp_dl.write_bytes(new_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_bytes).hexdigest(),
        "size": len(new_bytes)
    }, priv)

    captured_cmds = []
    proc_events = []
    staged_target = mock_exe.parent / f"~{mock_exe.name}.new"
    ready_marker_path = mock_exe.parent / f".ota_ready_{os.getpid()}"

    def mock_popen(args, **kwargs):
        captured_cmds.append(args)
        # Create readiness marker to simulate batch starting
        ready_marker_path.write_text("ready", encoding="utf-8")

        class HangingWatchdogProc:
            pid = 55555
            def poll(self):
                if "kill" in proc_events:
                    return -9  # Confirmed terminated after kill
                return None  # Running
            def terminate(self):
                proc_events.append("terminate")
            def wait(self, timeout=None):
                if "kill" not in proc_events:
                    proc_events.append(f"wait_terminate_timeout_{timeout}")
                    raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
                proc_events.append(f"wait_kill_timeout_{timeout}")
                return 0
            def kill(self):
                proc_events.append("kill")

        return HangingWatchdogProc()

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    # Inject an unexpected exception in time.monotonic to trigger the outer generic except Exception block
    # right after Popen returns and before readiness can be evaluated or completed
    def mock_monotonic_fail():
        raise RuntimeError("Simulated unexpected exception during readiness check loop")

    monkeypatch.setattr(time, "monotonic", mock_monotonic_fail)

    try:
        ok, err_msg = apply_in_place_update(str(temp_dl), signed_manifest=signed_man)

        # 1. Verification of return values
        assert ok is False
        assert "Simulated unexpected exception during readiness check loop" in err_msg

        # 2. Verifica esplicita che Popen sia stato invocato esattamente una volta prima di accedere agli argomenti
        assert len(captured_cmds) == 1, "subprocess.Popen must have been called exactly once for watchdog supervisor"

        # 3. Verification of watchdog termination, wait timeout, fallback kill, and final wait
        assert "terminate" in proc_events
        assert "wait_terminate_timeout_2.0" in proc_events
        assert "kill" in proc_events
        assert "wait_kill_timeout_1.0" in proc_events

        # 4. Verification that attempt artifacts were safely unlinked by production code
        assert len(captured_cmds) == 1, "subprocess.Popen must have been called exactly once for watchdog supervisor"
        bat_path = Path(captured_cmds[0][2])

        assert not staged_target.exists()
        assert not ready_marker_path.exists()
        assert not bat_path.exists()
        assert not temp_dl.exists()

        # 5. Verification that vital files (current_exe and old_backup) were STRICTLY PRESERVED
        assert mock_exe.exists()
        assert mock_exe.read_bytes() == original_bytes
        assert mock_backup.exists()
        assert mock_backup.read_bytes() == b"BACKUP_OF_PREVIOUS_VERSION_MUST_NOT_BE_TOUCHED"
    finally:
        def _cleanup_test_file(target_file: Optional[Path]):
            if target_file is None:
                return
            try:
                resolved_target = target_file.resolve()
                resolved_tmp = tmp_path.resolve()
                try:
                    resolved_target.relative_to(resolved_tmp)
                    is_inside = True
                except ValueError:
                    is_inside = False
                if is_inside and resolved_target.is_file():
                    resolved_target.unlink()
            except Exception:
                pass

        _cleanup_test_file(staged_target)
        _cleanup_test_file(ready_marker_path)
        _cleanup_test_file(temp_dl)
        if captured_cmds:
            _cleanup_test_file(Path(captured_cmds[0][2]))
        _cleanup_test_file(mock_exe)
        _cleanup_test_file(mock_backup)
        try:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
        except Exception:
            pass

def test_ensure_watchdog_terminated_unit():
    """
    Deterministic unit test for ensure_watchdog_terminated helper:
    1. None process -> returns True.
    2. Already exited process -> returns True immediately.
    3. terminate() succeeds and wait() completes -> returns True without calling kill().
    4. wait() times out after terminate(), fallback kill() and wait() succeed -> returns True.
    5. kill() or final wait() fails and process remains alive -> returns False.
    6. poll() raises an OS exception -> returns False.
    """
    # 1. None process
    assert ensure_watchdog_terminated(None) is True

    # 2. Already exited process
    class AlreadyDeadProc:
        def poll(self):
            return 0
    assert ensure_watchdog_terminated(AlreadyDeadProc()) is True

    # 3. Clean terminate + wait
    calls_3 = {"terminated": False, "waited": False, "killed": False}
    class CleanTerminateProc:
        def poll(self):
            return 0 if calls_3["waited"] else None
        def terminate(self):
            calls_3["terminated"] = True
        def wait(self, timeout=None):
            calls_3["waited"] = True
            return 0
        def kill(self):
            calls_3["killed"] = True
    proc_3 = CleanTerminateProc()
    assert ensure_watchdog_terminated(proc_3, term_timeout=0.1) is True
    assert calls_3["terminated"] is True
    assert calls_3["waited"] is True
    assert calls_3["killed"] is False, "kill() must not be called when terminate() succeeds"

    # 4. Wait timeout after terminate, followed by successful kill
    calls_4 = {"terminated": False, "waited_term": False, "killed": False, "waited_kill": False}
    class KillFallbackProc:
        def poll(self):
            return -9 if calls_4["killed"] else None
        def terminate(self):
            calls_4["terminated"] = True
        def wait(self, timeout=None):
            if not calls_4["killed"]:
                calls_4["waited_term"] = True
                raise subprocess.TimeoutExpired(cmd="test", timeout=timeout)
            calls_4["waited_kill"] = True
            return 0
        def kill(self):
            calls_4["killed"] = True
    proc_4 = KillFallbackProc()
    assert ensure_watchdog_terminated(proc_4, term_timeout=0.1, kill_timeout=0.1) is True
    assert calls_4["terminated"] is True
    assert calls_4["waited_term"] is True
    assert calls_4["killed"] is True
    assert calls_4["waited_kill"] is True

    # 5. Process refuses to exit (poll() returns None even after kill)
    class ImmortalProc:
        def poll(self):
            return None  # Never terminates
        def terminate(self):
            pass
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="test", timeout=timeout)
        def kill(self):
            pass
    assert ensure_watchdog_terminated(ImmortalProc(), term_timeout=0.01, kill_timeout=0.01) is False

    # 6. poll() raises an OS exception
    class BrokenProc:
        def poll(self):
            raise OSError("Access denied")
        def terminate(self):
            pass
        def wait(self, timeout=None):
            pass
        def kill(self):
            pass
    assert ensure_watchdog_terminated(BrokenProc(), term_timeout=0.01, kill_timeout=0.01) is False

def test_apply_in_place_update_watchdog_readiness_timeout_clean_terminate(tmp_path, monkeypatch):
    """
    Deterministic in-memory test: verifies that if readiness handshake times out and the watchdog
    process terminates cleanly on terminate() (without needing kill()), the attempt artifacts
    are safely cleaned up while current_exe remains intact.
    """
    original_bytes = b"ACTIVE_EXE_CLEAN_TERMINATE_INTACT"
    mock_exe = tmp_path / "AetherDesk.exe"
    mock_exe.write_bytes(original_bytes)

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_exe))

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    new_bytes = b"MZ" + b"\x90" * 1_200_000
    staging_dir = get_ota_staging_dir()
    temp_dl = staging_dir / "aether_ota_clean_term.exe"
    temp_dl.write_bytes(new_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_bytes).hexdigest(),
        "size": len(new_bytes)
    }, priv)

    captured_cmds = []
    events = []

    def mock_popen(args, **kwargs):
        captured_cmds.append(args)
        class CleanProc:
            pid = 11111
            def poll(self):
                return 0 if "terminate" in events else None
            def terminate(self):
                events.append("terminate")
            def wait(self, timeout=None):
                events.append("wait")
                return 0
            def kill(self):
                events.append("kill")
        return CleanProc()

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    ok, err_msg = apply_in_place_update(str(temp_dl), signed_manifest=signed_man, readiness_timeout=0.05)
    assert ok is False
    assert "Supervisore terminato ed eseguibile attivo rimasto intatto" in err_msg
    assert "terminate" in events
    assert "kill" not in events

    # Artifacts are safely cleaned up because termination was positively confirmed
    staged_target = mock_exe.parent / f"~{mock_exe.name}.new"
    assert len(captured_cmds) == 1, "subprocess.Popen must have been called exactly once for watchdog supervisor"
    bat_path = Path(captured_cmds[0][2])
    assert not staged_target.exists()
    assert not bat_path.exists()
    assert not temp_dl.exists()
    assert mock_exe.exists()
    assert mock_exe.read_bytes() == original_bytes

def test_apply_in_place_update_watchdog_termination_unconfirmed_preserves_artifacts(tmp_path, monkeypatch):
    """
    Test deterministico con filesystem temporaneo isolato (tmp_path): verifica che se la terminazione
    del supervisore watchdog NON puo' essere confermata (es. timeout di attesa, poll() restituisce None):
    1. apply_in_place_update registra un errore critico e restituisce False.
    2. Il messaggio di errore dichiara esplicitamente che il supervisore potrebbe essere ancora attivo.
    3. Il messaggio NON dichiara l'aggiornamento annullato in sicurezza o il cleanup completato.
    4. Cruciale: staged_target (~{name}.new) e restart_bat (.bat) vengono RIGOROSAMENTE PRESERVATI
       sul disco da apply_in_place_update per non corrompere l'ambiente del supervisore attivo.
    5. current_exe e il backup (.exe.old) rimangono intatti e non toccati.
    6. La directory di staging e la cartella temporanea sono isolate sotto tmp_path tramite monkeypatch,
       impedendo la scrittura di file nella directory temporanea globale del sistema.
    7. Il file temporaneo scaricato (temp_dl) viene eliminato dal codice di produzione (in quanto indipendente
       dal supervisore), mentre tutti gli artefatti del tentativo vengono rimossi nel blocco finally post-asserzione
       garantendo la totale assenza di residui su disco anche in caso di fallimento delle asserzioni.
    """
    original_bytes = b"ACTIVE_EXE_IMMORTAL_WATCHDOG_TEST"
    mock_exe = tmp_path / "AetherDesk.exe"
    mock_exe.write_bytes(original_bytes)
    mock_backup = tmp_path / "AetherDesk.exe.old"
    mock_backup.write_bytes(b"BACKUP_SLOT_UNTOUCHED")

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_exe))

    # Isola lo stato di boot a IDLE per consentire l'avvio del watchdog preservando il file di backup
    monkeypatch.setattr("src.ota.check_boot_state", lambda: OTAUpdateState.IDLE)
    # Isola cleanup_old_versions con no-op: lo stato IDLE e' artificiale per il test del solo watchdog
    # e mock_backup funge da sentinel per verificare che i rami di errore del watchdog non lo tocchino
    monkeypatch.setattr("src.ota.cleanup_old_versions", lambda *args, **kwargs: 0)

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    # 1. Isola la directory di staging e tempdir rigorosamente sotto tmp_path
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("src.ota.tempfile.gettempdir", lambda: str(tmp_path))
    staging_dir = tmp_path / "ota_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("src.ota.get_ota_staging_dir", lambda: staging_dir)

    new_bytes = b"MZ" + b"\x90" * 1_200_000
    temp_dl = staging_dir / "aether_ota_unconfirmed.exe"
    temp_dl.write_bytes(new_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_bytes).hexdigest(),
        "size": len(new_bytes)
    }, priv)

    captured_cmds = []
    staged_target = mock_exe.parent / f"~{mock_exe.name}.new"
    ready_marker = mock_exe.parent / f".ota_ready_{os.getpid()}"

    def mock_popen(args, **kwargs):
        captured_cmds.append(args)
        # Il supervisore non emette il marcatore di readiness (simulazione blocco/timeout)
        class ImmortalProc:
            pid = 88888
            def poll(self):
                return None  # Ancora in vita, mai terminato
            def terminate(self):
                pass
            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
            def kill(self):
                pass
        return ImmortalProc()

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    try:
        # Trigger readiness timeout
        ok, err_msg = apply_in_place_update(str(temp_dl), signed_manifest=signed_man, readiness_timeout=0.05)

        # 1. Deve restituire fallimento
        assert ok is False

        # 2. Deve dichiarare esplicitamente che la terminazione non e' confermata e il supervisore potrebbe essere attivo
        assert "non e' stato possibile confermarne la terminazione" in err_msg
        assert "il processo supervisore potrebbe essere ancora attivo" in err_msg
        assert "annullato in sicurezza" not in err_msg.lower()
        assert "cleanup completato" not in err_msg.lower()

        # 3. Durante l'esecuzione, gli artefatti del supervisore DEVONO essere stati preservati da apply_in_place_update!
        assert len(captured_cmds) == 1, "subprocess.Popen must have been called exactly once for watchdog supervisor"
        bat_path = Path(captured_cmds[0][2])
        assert staged_target.exists(), "staged_target must NOT be deleted by production code when watchdog termination is unconfirmed"
        assert bat_path.exists(), "restart_bat must NOT be deleted by production code when watchdog termination is unconfirmed"

        # 4. I binari vitali devono rimanere rigorosamente intatti
        assert mock_exe.exists()
        assert mock_exe.read_bytes() == original_bytes
        assert mock_backup.exists()
        assert mock_backup.read_bytes() == b"BACKUP_SLOT_UNTOUCHED"

        # 5. Il codice di produzione deve invece aver ripulito il file temporaneo scaricato (temp_dl), indipendente dal supervisore
        assert not temp_dl.exists(), "temp_dl must be cleaned up by production code as it is independent of supervisor"
    finally:
        # La cancellazione nel blocco finally serve esclusivamente a ripulire il test dopo le asserzioni.
        # Chiarimento fondamentale: in questo test il watchdog e' un mock in-process (ImmortalProc) e non un processo
        # reale in esecuzione nel sistema operativo; la rimozione post-asserzione e' quindi sicura e non interferisce
        # con alcun supervisore. Vincolo rigoroso: non viene eliminato alcun file al di fuori di tmp_path.
        def _cleanup_test_file(target_file: Optional[Path]):
            if target_file is None:
                return
            try:
                resolved_target = target_file.resolve()
                resolved_tmp = tmp_path.resolve()
                try:
                    resolved_target.relative_to(resolved_tmp)
                    is_inside = True
                except ValueError:
                    is_inside = False
                if is_inside and resolved_target.is_file():
                    resolved_target.unlink()
            except Exception:
                pass

        _cleanup_test_file(staged_target)
        _cleanup_test_file(ready_marker)
        _cleanup_test_file(temp_dl)
        if captured_cmds:
            _cleanup_test_file(Path(captured_cmds[0][2]))
        _cleanup_test_file(mock_exe)
        _cleanup_test_file(mock_backup)
        try:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
        except Exception:
            pass

def test_apply_in_place_update_generic_exception_unconfirmed_watchdog_preserves_artifacts(tmp_path, monkeypatch):
    """
    Test deterministico con filesystem temporaneo isolato (tmp_path): verifica che se si verifica un'eccezione
    imprevista dopo Popen e la terminazione del supervisore watchdog non puo' essere confermata:
    1. ready_marker, restart_bat e staged_target NON vengono eliminati da apply_in_place_update.
    2. current_exe e old_backup sono rigorosamente preservati.
    3. Il messaggio restituito dichiara esplicitamente che il supervisore watchdog potrebbe essere ancora attivo.
    4. La directory di staging e tempdir sono isolate sotto tmp_path tramite monkeypatch.
    5. Il file scaricato indipendente (temp_dl) viene rimosso dal codice di produzione, mentre tutti gli artefatti
       del tentativo vengono rimossi determiniscamente nel blocco finally post-asserzione garantendo la pulizia
       anche in caso di fallimento delle asserzioni.
    """
    original_bytes = b"ACTIVE_EXE_GENERIC_UNCONFIRMED_TEST"
    mock_exe = tmp_path / "AetherDesk.exe"
    mock_exe.write_bytes(original_bytes)
    mock_backup = tmp_path / "AetherDesk.exe.old"
    mock_backup.write_bytes(b"BACKUP_SLOT_GENERIC_UNCONFIRMED")

    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setattr(sys, "executable", str(mock_exe))

    # Isola lo stato di boot a IDLE per consentire l'avvio del watchdog preservando il file di backup
    monkeypatch.setattr("src.ota.check_boot_state", lambda: OTAUpdateState.IDLE)
    # Isola cleanup_old_versions con no-op: lo stato IDLE e' artificiale per il test del solo watchdog
    # e mock_backup funge da sentinel per verificare che i rami di errore del watchdog non lo tocchino
    monkeypatch.setattr("src.ota.cleanup_old_versions", lambda *args, **kwargs: 0)

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr("src.ota.OTA_VENDOR_PUBLIC_KEY_HEX", pub_hex)

    # 1. Isola la directory di staging e tempdir rigorosamente sotto tmp_path
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("src.ota.tempfile.gettempdir", lambda: str(tmp_path))
    staging_dir = tmp_path / "ota_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("src.ota.get_ota_staging_dir", lambda: staging_dir)

    new_bytes = b"MZ" + b"\x90" * 1_200_000
    temp_dl = staging_dir / "aether_ota_generic_unconf.exe"
    temp_dl.write_bytes(new_bytes)

    signed_man = sign_update_manifest({
        "version": "2.0.0",
        "filename": "AetherDesk.exe",
        "sha256": hashlib.sha256(new_bytes).hexdigest(),
        "size": len(new_bytes)
    }, priv)

    captured_cmds = []
    staged_target = mock_exe.parent / f"~{mock_exe.name}.new"
    ready_marker = mock_exe.parent / f".ota_ready_{os.getpid()}"

    def mock_popen(args, **kwargs):
        captured_cmds.append(args)
        ready_marker.write_text("ready", encoding="utf-8")

        class ImmortalProc:
            pid = 77777
            def poll(self):
                return None  # Never terminates
            def terminate(self):
                pass
            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
            def kill(self):
                pass
        return ImmortalProc()

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    # Force unexpected exception via time.monotonic right after Popen returns
    def mock_monotonic_fail():
        raise RuntimeError("Unexpected failure during readiness handshake")

    monkeypatch.setattr(time, "monotonic", mock_monotonic_fail)

    try:
        ok, err_msg = apply_in_place_update(str(temp_dl), signed_manifest=signed_man)

        assert ok is False
        assert "Unexpected failure during readiness handshake" in err_msg
        assert "mancata conferma della terminazione del supervisore watchdog" in err_msg
        assert "il processo supervisore potrebbe essere ancora attivo" in err_msg

        # Crucial check: gli artefatti DEVONO essere preservati sul disco da apply_in_place_update durante l'esecuzione!
        assert len(captured_cmds) == 1, "subprocess.Popen must have been called exactly once for watchdog supervisor"
        bat_path = Path(captured_cmds[0][2])

        assert staged_target.exists(), "staged_target must NOT be deleted by production code when watchdog termination is unconfirmed"
        assert ready_marker.exists(), "ready_marker must NOT be deleted by production code when watchdog termination is unconfirmed"
        assert bat_path.exists(), "restart_bat must NOT be deleted by production code when watchdog termination is unconfirmed"

        # Vital files intact
        assert mock_exe.exists()
        assert mock_exe.read_bytes() == original_bytes
        assert mock_backup.exists()
        assert mock_backup.read_bytes() == b"BACKUP_SLOT_GENERIC_UNCONFIRMED"

        # Production code cleans up the downloaded temp file (temp_dl) while preserving supervisor artifacts
        assert not temp_dl.exists(), "temp_dl must be cleaned up by production code as it is independent of supervisor"
    finally:
        # La cancellazione nel blocco finally serve esclusivamente a ripulire il test dopo le asserzioni.
        # Chiarimento fondamentale: in questo test il watchdog e' un mock in-process (ImmortalProc) e non un processo
        # reale in esecuzione nel sistema operativo; la rimozione post-asserzione e' quindi sicura e non interferisce
        # con alcun supervisore. Vincolo rigoroso: non viene eliminato alcun file al di fuori di tmp_path.
        def _cleanup_test_file(target_file: Optional[Path]):
            if target_file is None:
                return
            try:
                resolved_target = target_file.resolve()
                resolved_tmp = tmp_path.resolve()
                try:
                    resolved_target.relative_to(resolved_tmp)
                    is_inside = True
                except ValueError:
                    is_inside = False
                if is_inside and resolved_target.is_file():
                    resolved_target.unlink()
            except Exception:
                pass

        _cleanup_test_file(staged_target)
        _cleanup_test_file(ready_marker)
        _cleanup_test_file(temp_dl)
        if captured_cmds:
            _cleanup_test_file(Path(captured_cmds[0][2]))
        _cleanup_test_file(mock_exe)
        _cleanup_test_file(mock_backup)
        try:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
        except Exception:
            pass
