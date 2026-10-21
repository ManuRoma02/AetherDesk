import os
import json
import time
import secrets
import hashlib
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

class SecurityManager:
    """Security, credential management, E2EE AES-GCM crypto, and rate-limiting."""

    def __init__(self, config_dir: str = None):
        if config_dir:
            self.config_dir = Path(config_dir)
        else:
            prog_data = os.environ.get("ProgramData", "C:\\ProgramData")
            self.config_dir = Path(prog_data) / "RemoteDesktop"

        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.config_dir / "config.json"

        self._failed_attempts = {} # ip -> [timestamps]
        self._load_config()

    def _load_config(self):
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    self.config = json.load(f)
            except Exception:
                self.config = {}
        else:
            self.config = {}

    def _save_config(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)

    def get_or_create_device_id(self) -> str:
        """Returns the persistent 9-digit device ID (format: 'XXX XXX XXX')."""
        if "device_id" in self.config and self.config["device_id"]:
            return self.config["device_id"]

        # Generate a random 9-digit number between 100000000 and 999999999
        num = secrets.randbelow(900000000) + 100000000
        raw = str(num)
        formatted = f"{raw[:3]} {raw[3:6]} {raw[6:]}"
        self.config["device_id"] = formatted
        self._save_config()
        return formatted

    def set_password(self, password: str) -> None:
        """Hash and persist the unattended access password."""
        salt = secrets.token_bytes(16)
        pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
        self.config["pwd_salt"] = salt.hex()
        self.config["pwd_hash"] = pwd_hash.hex()
        self._save_config()

    def verify_password(self, password: str) -> bool:
        """Verify entered password against stored PBKDF2 hash."""
        salt_hex = self.config.get("pwd_salt")
        hash_hex = self.config.get("pwd_hash")
        if not salt_hex or not hash_hex:
            return False

        salt = bytes.fromhex(salt_hex)
        expected_hash = bytes.fromhex(hash_hex)
        computed_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
        return secrets.compare_digest(computed_hash, expected_hash)

    def derive_session_key(self, password: str, salt: bytes) -> bytes:
        """Derive 256-bit AES-GCM session key from password and salt."""
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 10_000)

    def encrypt_packet(self, data: bytes, key: bytes) -> bytes:
        """Encrypt payload using AES-256-GCM with a 12-byte random nonce."""
        aesgcm = AESGCM(key)
        nonce = secrets.token_bytes(12)
        ciphertext = aesgcm.encrypt(nonce, data, None)
        return nonce + ciphertext

    def decrypt_packet(self, encrypted: bytes, key: bytes) -> bytes:
        """Decrypt AES-256-GCM packet with prefixed 12-byte nonce."""
        if len(encrypted) < 12 + 16:
            raise ValueError("Encrypted data is too short")
        nonce = encrypted[:12]
        ciphertext = encrypted[12:]
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ciphertext, None)

    def is_rate_limited(self, client_ip: str, max_attempts: int = 5, lock_duration_sec: int = 60) -> bool:
        """Check if an IP has exceeded failed login attempts within window."""
        now = time.time()
        attempts = self._failed_attempts.get(client_ip, [])
        # Filter attempts within lock_duration_sec
        recent = [t for t in attempts if now - t < lock_duration_sec]
        self._failed_attempts[client_ip] = recent
        return len(recent) >= max_attempts

    def record_failed_attempt(self, client_ip: str) -> None:
        now = time.time()
        if client_ip not in self._failed_attempts:
            self._failed_attempts[client_ip] = []
        self._failed_attempts[client_ip].append(now)

    def reset_attempts(self, client_ip: str) -> None:
        if client_ip in self._failed_attempts:
            del self._failed_attempts[client_ip]
