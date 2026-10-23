import os
import json
import time
import secrets
import hashlib
import struct
import platform
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

def _dpapi_protect(data: bytes) -> bytes:
    """Protect binary data at rest using Windows DPAPI (CryptProtectData)."""
    if platform.system() != "Windows":
        return data
    try:
        import ctypes
        import ctypes.wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))
            ]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        buf = ctypes.create_string_buffer(data)
        blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        # 0x01 = CRYPTPROTECT_UI_FORBIDDEN
        if not crypt32.CryptProtectData(ctypes.byref(blob_in), "aether_secret", None, None, None, 0x01, ctypes.byref(blob_out)):
            return data
        res = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        kernel32.LocalFree(blob_out.pbData)
        return res
    except Exception:
        return data

def _dpapi_unprotect(data: bytes) -> bytes:
    """Unprotect binary data using Windows DPAPI (CryptUnprotectData)."""
    if platform.system() != "Windows":
        return data
    try:
        import ctypes
        import ctypes.wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))
            ]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        buf = ctypes.create_string_buffer(data)
        blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0x01, ctypes.byref(blob_out)):
            return data
        res = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        kernel32.LocalFree(blob_out.pbData)
        return res
    except Exception:
        return data

class SecurityManager:
    """Security, credential management, E2EE AES-GCM crypto with anti-replay, and rate-limiting."""

    def __init__(self, config_dir: str = None):
        if config_dir:
            self.config_dir = Path(config_dir)
        else:
            prog_data = os.environ.get("ProgramData", "C:\\ProgramData")
            self.config_dir = Path(prog_data) / "RemoteDesktop"

        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.config_dir / "config.json"

        self._failed_attempts = {} # ip -> [timestamps]
        self._send_seq = 0
        self._seen_seqs = set()
        self._max_recv_seq = -1
        self._load_config()

    def reset_replay_window(self) -> None:
        """Reset sequence counters and replay protection for a fresh session."""
        self._send_seq = 0
        self._seen_seqs.clear()
        self._max_recv_seq = -1

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

    def set_password(self, password: str, iterations: int = 600_000) -> None:
        """Derive master key and persist credentials for unattended access."""
        salt = secrets.token_bytes(16)
        master_key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        pwd_hash = hashlib.sha256(master_key).hexdigest()
        self.config["pwd_salt"] = salt.hex()
        self.config["iterations"] = iterations
        self.config["master_key"] = master_key.hex()
        self.config["pwd_hash"] = pwd_hash
        self.config["password_display"] = password

        # OS-level secret protection via Windows DPAPI
        dpapi_blob = _dpapi_protect(master_key)
        if dpapi_blob != master_key:
            self.config["master_key_dpapi"] = dpapi_blob.hex()

        self._save_config()

    def get_password_display(self) -> str:
        """Returns the visible/configured password, provisioning default '123456' if none exists."""
        pwd = self.config.get("password_display", "")
        if not pwd:
            if not self.config.get("master_key"):
                self.set_password("123456")
                return "123456"
            return self.config.get("password_display", "123456")
        return pwd

    def get_master_key(self) -> bytes | None:
        """Retrieve stored master key, decrypting DPAPI container if present."""
        if "master_key_dpapi" in self.config and self.config["master_key_dpapi"]:
            raw_dpapi = bytes.fromhex(self.config["master_key_dpapi"])
            unprotected = _dpapi_unprotect(raw_dpapi)
            if unprotected:
                return unprotected
        if "master_key" in self.config and self.config["master_key"]:
            return bytes.fromhex(self.config["master_key"])
        return None

    def derive_master_key(self, password: str, salt: bytes, iterations: int = 600_000) -> bytes:
        """Derive 256-bit master key from user password and salt using OWASP-compliant PBKDF2."""
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)

    def verify_password(self, password: str) -> bool:
        """Verify entered password against stored master key hash."""
        salt_hex = self.config.get("pwd_salt")
        if not salt_hex:
            return False

        salt = bytes.fromhex(salt_hex)
        iterations = self.config.get("iterations", 600_000)
        computed_master = self.derive_master_key(password, salt, iterations=iterations)

        if "pwd_hash" in self.config and self.config["pwd_hash"]:
            computed_hash = hashlib.sha256(computed_master).hexdigest()
            return secrets.compare_digest(computed_hash, self.config["pwd_hash"])

        stored_master = self.get_master_key()
        if stored_master:
            return secrets.compare_digest(computed_master, stored_master)
        return False

    def derive_session_key(self, master_or_password: str | bytes, session_salt: bytes) -> bytes:
        """Derive 256-bit AES-GCM session key from master key/password and session salt."""
        if isinstance(master_or_password, str):
            key_data = master_or_password.encode("utf-8")
        else:
            key_data = master_or_password
        return hashlib.pbkdf2_hmac("sha256", key_data, session_salt, 10_000)

    def encrypt_packet(self, data: bytes, key: bytes, seq: int = None, aad: bytes = None) -> bytes:
        """
        Encrypt payload using AES-256-GCM with anti-replay framing (RFC 5116).
        Packet format: MAGIC(4B: b'AEP1') | SEQ(8B: uint64) | NONCE(12B) | CIPHERTEXT+TAG
        Associated Data cryptographically binds MAGIC + SEQ + optional extra aad.
        """
        aesgcm = AESGCM(key)
        if seq is None:
            self._send_seq = (self._send_seq + 1) & 0xFFFFFFFFFFFFFFFF
            seq = self._send_seq

        header = b"AEP1" + struct.pack(">Q", seq)
        nonce = secrets.token_bytes(12)
        associated_data = header + (aad if aad else b"")
        ciphertext = aesgcm.encrypt(nonce, data, associated_data)
        return header + nonce + ciphertext

    def decrypt_packet(self, encrypted: bytes, key: bytes, aad: bytes = None, check_replay: bool = True) -> bytes:
        """
        Decrypt AES-256-GCM packet.
        Validates anti-replay sequence window and AEAD associated data.
        Falls back to legacy format (12B nonce + ciphertext) for backwards compatibility.
        """
        # Modern AEP1 packet: 4B magic + 8B seq + 12B nonce + min 16B tag
        if encrypted.startswith(b"AEP1") and len(encrypted) >= 24 + 16:
            header = encrypted[:12]
            seq = struct.unpack(">Q", header[4:12])[0]
            nonce = encrypted[12:24]
            ciphertext = encrypted[24:]
            associated_data = header + (aad if aad else b"")
            aesgcm = AESGCM(key)
            decrypted = aesgcm.decrypt(nonce, ciphertext, associated_data)

            if check_replay:
                if seq in self._seen_seqs or (self._max_recv_seq > 1000 and seq < self._max_recv_seq - 1000):
                    raise ValueError(f"Replay attack detected: duplicate or expired sequence {seq}")
                self._seen_seqs.add(seq)
                if seq > self._max_recv_seq:
                    self._max_recv_seq = seq
                if len(self._seen_seqs) > 2000:
                    cutoff = self._max_recv_seq - 1000
                    self._seen_seqs = {s for s in self._seen_seqs if s >= cutoff}
            return decrypted

        # Legacy format fallback (12-byte nonce + ciphertext)
        if len(encrypted) < 12 + 16:
            raise ValueError("Encrypted data is too short")
        nonce = encrypted[:12]
        ciphertext = encrypted[12:]
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ciphertext, None)

    def is_rate_limited(self, client_ip: str, max_attempts: int = 5, lock_duration_sec: int = 60) -> bool:
        """Check if an IP has exceeded failed login attempts within window and prune expired entries."""
        now = time.time()
        # Prune expired IPs to avoid unbounded memory growth
        stale_ips = [ip for ip, times in self._failed_attempts.items() if not times or now - times[-1] >= lock_duration_sec]
        for ip in stale_ips:
            del self._failed_attempts[ip]

        attempts = self._failed_attempts.get(client_ip, [])
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
