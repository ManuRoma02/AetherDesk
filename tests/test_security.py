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

def test_anti_replay_protection(tmp_path):
    mgr_sender = SecurityManager(config_dir=str(tmp_path / "sender"))
    mgr_receiver = SecurityManager(config_dir=str(tmp_path / "receiver"))
    key = mgr_sender.derive_session_key("SecretKey2026", b"session_salt_1234")

    # Send 3 distinct packets in sequence
    p1 = mgr_sender.encrypt_packet(b"Packet 1: Mouse Move", key)
    p2 = mgr_sender.encrypt_packet(b"Packet 2: Left Click", key)
    p3 = mgr_sender.encrypt_packet(b"Packet 3: Key Press", key)

    assert mgr_receiver.decrypt_packet(p1, key) == b"Packet 1: Mouse Move"
    assert mgr_receiver.decrypt_packet(p2, key) == b"Packet 2: Left Click"
    assert mgr_receiver.decrypt_packet(p3, key) == b"Packet 3: Key Press"

    # Replay attack: attempt to re-inject p2
    with pytest.raises(ValueError, match="Replay attack detected"):
        mgr_receiver.decrypt_packet(p2, key)

    # Replay attack: attempt to re-inject p1
    with pytest.raises(ValueError, match="Replay attack detected"):
        mgr_receiver.decrypt_packet(p1, key)

def test_dpapi_and_high_iteration_password(tmp_path):
    mgr = SecurityManager(config_dir=str(tmp_path))
    mgr.set_password("SecurePassword2026!", iterations=600_000)

    # Check persistence and verification
    assert mgr.verify_password("SecurePassword2026!") is True
    assert mgr.verify_password("WrongPassword") is False
    assert mgr.get_master_key() is not None

    # Load fresh manager instance from disk
    mgr2 = SecurityManager(config_dir=str(tmp_path))
    assert mgr2.verify_password("SecurePassword2026!") is True
    assert mgr2.get_master_key() == mgr.get_master_key()

def test_rate_limiting(tmp_path):
    mgr = SecurityManager(config_dir=str(tmp_path))
    ip = "192.168.1.100"
    assert mgr.is_rate_limited(ip) is False
    for _ in range(5):
        mgr.record_failed_attempt(ip)
    assert mgr.is_rate_limited(ip) is True
    mgr.reset_attempts(ip)
    assert mgr.is_rate_limited(ip) is False
