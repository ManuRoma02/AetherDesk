import pytest
from src.security import SecurityManager

def test_device_id_format(tmp_path):
    mgr = SecurityManager(config_dir=str(tmp_path))
    dev_id = mgr.get_or_create_device_id()
    clean_id = dev_id.replace(" ", "")
    assert len(clean_id) == 9
    assert clean_id.isdigit()
    # Verify persistence across instances
    mgr2 = SecurityManager(config_dir=str(tmp_path))
    assert mgr2.get_or_create_device_id() == dev_id

def test_password_hashing(tmp_path):
    mgr = SecurityManager(config_dir=str(tmp_path))
    mgr.set_password("MioProgetto2026!")
    assert mgr.verify_password("MioProgetto2026!") is True
    assert mgr.verify_password("PasswordErrata") is False

def test_packet_encryption(tmp_path):
    mgr = SecurityManager(config_dir=str(tmp_path))
    salt = b"random_salt_1234"
    key = mgr.derive_session_key("MioSegreto", salt)
    plaintext = b"Test packet payload for remote desktop"
    ciphertext = mgr.encrypt_packet(plaintext, key)
    assert ciphertext != plaintext
    decrypted = mgr.decrypt_packet(ciphertext, key)
    assert decrypted == plaintext

def test_rate_limiting(tmp_path):
    mgr = SecurityManager(config_dir=str(tmp_path))
    ip = "192.168.1.100"
    assert mgr.is_rate_limited(ip) is False
    for _ in range(5):
        mgr.record_failed_attempt(ip)
    assert mgr.is_rate_limited(ip) is True
    mgr.reset_attempts(ip)
    assert mgr.is_rate_limited(ip) is False
