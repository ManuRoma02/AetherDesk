import os
import sys
import json
import time
import secrets
import hashlib
import struct
import platform
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.exceptions import InvalidSignature

import logging
from typing import Optional

logger = logging.getLogger("SecurityManager")

def is_test_environment() -> bool:
    """Check if Python process is running within pytest or test discovery."""
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST") or "PYTEST_VERSION" in os.environ:
        return True
    if any("pytest" in arg.lower() for arg in sys.argv):
        return True
    return False

class IDPAPIEngine:
    """Interface for credential at-rest protection engines."""
    def protect(self, data: bytes) -> Optional[bytes]:
        raise NotImplementedError
    def unprotect(self, data: bytes) -> Optional[bytes]:
        raise NotImplementedError


class WindowsDPAPIEngine(IDPAPIEngine):
    """Production Windows DPAPI engine using CryptProtectData and CryptUnprotectData."""

    def protect(self, data: bytes) -> Optional[bytes]:
        if platform.system() != "Windows":
            return None
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
                return None
            res = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            kernel32.LocalFree(blob_out.pbData)
            return res
        except Exception as e:
            logger.error(f"Windows DPAPI CryptProtectData failed: {e}")
            return None

    def unprotect(self, data: bytes) -> Optional[bytes]:
        if platform.system() != "Windows":
            return None
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
                return None
            res = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            kernel32.LocalFree(blob_out.pbData)
            return res
        except Exception as e:
            logger.error(f"Windows DPAPI CryptUnprotectData failed: {e}")
            return None


class MockTestDPAPIEngine(IDPAPIEngine):
    """
    Explicit test-only mock engine for automated unit tests on non-Windows environments.
    Prefixes with explicit test tag 'MOCK_TEST_DPAPI:' and AES-GCM encryption with
    an in-memory ephemeral key.
    """
    def __init__(self, test_key: bytes = None):
        self._key = test_key or b"TEST_MOCK_DPAPI_KEY_32_BYTES_123"

    def protect(self, data: bytes) -> Optional[bytes]:
        aes = AESGCM(self._key)
        nonce = secrets.token_bytes(12)
        ct = aes.encrypt(nonce, data, None)
        return b"MOCK_TEST_DPAPI:" + nonce + ct

    def unprotect(self, data: bytes) -> Optional[bytes]:
        if not data.startswith(b"MOCK_TEST_DPAPI:"):
            return None
        try:
            payload = data[len(b"MOCK_TEST_DPAPI:"):]
            nonce = payload[:12]
            ct = payload[12:]
            aes = AESGCM(self._key)
            return aes.decrypt(nonce, ct, None)
        except Exception:
            return None


def _dpapi_protect(data: bytes, engine: IDPAPIEngine = None) -> Optional[bytes]:
    """Protect binary data using specified engine (defaults to Windows DPAPI). Returns None on failure or unsupported OS."""
    eng = engine
    if eng is None and platform.system() == "Windows":
        try:
            import ctypes
            if hasattr(ctypes, "windll"):
                eng = WindowsDPAPIEngine()
        except Exception:
            eng = None
    if eng is None:
        return None
    return eng.protect(data)


def _dpapi_unprotect(data: bytes, engine: IDPAPIEngine = None) -> Optional[bytes]:
    """Unprotect binary data using specified engine (defaults to Windows DPAPI). Returns None on failure or unsupported OS."""
    eng = engine
    if eng is None and platform.system() == "Windows":
        try:
            import ctypes
            if hasattr(ctypes, "windll"):
                eng = WindowsDPAPIEngine()
        except Exception:
            eng = None
    if eng is None:
        return None
    return eng.unprotect(data)


class SecurityManager:
    """Security, credential management, E2EE AES-GCM crypto with anti-replay, and rate-limiting."""

    def __init__(self, config_dir: str = None, dpapi_engine: IDPAPIEngine = None):
        if dpapi_engine is not None:
            self.dpapi_engine = dpapi_engine
        elif platform.system() == "Windows":
            try:
                import ctypes
                self.dpapi_engine = WindowsDPAPIEngine() if hasattr(ctypes, "windll") else None
            except Exception:
                self.dpapi_engine = None
        else:
            self.dpapi_engine = None
        if config_dir:
            self.config_dir = Path(config_dir)
        elif is_test_environment():
            import tempfile
            self._test_temp_dir = tempfile.TemporaryDirectory(prefix="aether_test_sec_")
            self.config_dir = Path(self._test_temp_dir.name)
        else:
            prog_data = Path(os.environ.get("ProgramData", "C:\\ProgramData"))
            legacy = prog_data / "RemoteDesktop"
            primary = prog_data / "AetherDesk"
            self.config_dir = legacy if legacy.exists() and not primary.exists() else primary

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
        self._migrate_credentials()

    def _migrate_credentials(self) -> bool:
        """
        Safely migrate legacy plaintext credentials to DPAPI-protected storage.
        Eliminates plaintext storage on disk while preserving existing user credentials.
        Never erases the legacy plaintext credential if DPAPI protection fails!
        """
        if self.dpapi_engine is None:
            return False

        migrated = False

        # 1. Migrate legacy plaintext password_display
        legacy_pwd = self.config.get("password_display")
        if legacy_pwd and isinstance(legacy_pwd, str):
            prot = self.dpapi_engine.protect(legacy_pwd.encode("utf-8"))
            if prot is not None:
                self.config["password_display_dpapi"] = prot.hex()
                self.config.pop("password_display", None)
                migrated = True
            else:
                logger.error(
                    "DPAPI protection of legacy password failed during migration. "
                    "Preserving original password_display to prevent credential loss."
                )

        # 2. Migrate legacy plaintext master_key
        legacy_mk = self.config.get("master_key")
        if legacy_mk and isinstance(legacy_mk, str):
            try:
                mk_bytes = bytes.fromhex(legacy_mk)
                prot = self.dpapi_engine.protect(mk_bytes)
                if prot is not None:
                    self.config["master_key_dpapi"] = prot.hex()
                    self.config.pop("master_key", None)
                    migrated = True
                else:
                    logger.error(
                        "DPAPI protection of legacy master_key failed during migration. "
                        "Preserving original master_key to prevent credential loss."
                    )
            except Exception:
                pass

        if migrated:
            self._save_config()
        return migrated

    @staticmethod
    def _generate_secure_password(length: int = 10) -> str:
        """Generate a cryptographically secure, readable password without weak defaults."""
        alphabet = "23456789abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ"
        return "".join(secrets.choice(alphabet) for _ in range(length))

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

    def get_or_create_identity_key(self) -> ed25519.Ed25519PrivateKey:
        """Get or generate persistent Ed25519 device identity keypair, protected by DPAPI at rest."""
        if hasattr(self, "_identity_key_cache") and self._identity_key_cache is not None:
            return self._identity_key_cache

        # 1. Try to load existing key
        raw_priv = None
        if "identity_key_dpapi" in self.config and self.config["identity_key_dpapi"] and self.dpapi_engine:
            try:
                blob = bytes.fromhex(self.config["identity_key_dpapi"])
                raw_priv = self.dpapi_engine.unprotect(blob)
            except Exception as e:
                logger.warning(f"Failed to unprotect identity_key_dpapi: {e}")

        if raw_priv is None and "identity_key_raw" in self.config and self.config["identity_key_raw"]:
            try:
                raw_priv = bytes.fromhex(self.config["identity_key_raw"])
            except Exception:
                pass

        if raw_priv and len(raw_priv) == 32:
            key = ed25519.Ed25519PrivateKey.from_private_bytes(raw_priv)
            self._identity_key_cache = key
            return key

        # 2. Generate fresh Ed25519 keypair
        key = ed25519.Ed25519PrivateKey.generate()
        priv_bytes = key.private_bytes_raw()
        pub_bytes = key.public_key().public_bytes_raw()

        if self.dpapi_engine:
            prot = self.dpapi_engine.protect(priv_bytes)
            if prot is not None:
                self.config["identity_key_dpapi"] = prot.hex()
                self.config.pop("identity_key_raw", None)
            else:
                self.config["identity_key_raw"] = priv_bytes.hex()
        else:
            self.config["identity_key_raw"] = priv_bytes.hex()

        self.config["identity_pubkey"] = pub_bytes.hex()
        self._save_config()
        self._identity_key_cache = key
        return key

    def get_identity_public_key(self) -> bytes:
        """Return 32-byte raw Ed25519 public key."""
        key = self.get_or_create_identity_key()
        return key.public_key().public_bytes_raw()

    def get_identity_public_key_hex(self) -> str:
        """Return hex-encoded Ed25519 public key."""
        return self.get_identity_public_key().hex()

    def sign_data(self, data: bytes) -> bytes:
        """Sign data using the persistent Ed25519 private key."""
        key = self.get_or_create_identity_key()
        return key.sign(data)

    @staticmethod
    def verify_data_signature(public_key_bytes: bytes, data: bytes, signature: bytes) -> bool:
        """Verify Ed25519 signature against data and raw public key bytes."""
        try:
            pub = ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)
            pub.verify(signature, data)
            return True
        except (InvalidSignature, Exception):
            return False

    @staticmethod
    def create_ephemeral_ecdh() -> tuple[x25519.X25519PrivateKey, bytes]:
        """Generate ephemeral X25519 keypair for Forward-Secret key exchange."""
        priv = x25519.X25519PrivateKey.generate()
        pub_bytes = priv.public_key().public_bytes_raw()
        return priv, pub_bytes

    @staticmethod
    def derive_ecdh_shared_key(
        private_key: x25519.X25519PrivateKey,
        peer_public_bytes: bytes,
        salt: bytes,
        context: bytes = b"AetherDesk-v2-Session"
    ) -> bytes:
        """Derive 32-byte symmetric master key using X25519 ECDH + HKDF-SHA256."""
        peer_pub = x25519.X25519PublicKey.from_public_bytes(peer_public_bytes)
        shared_secret = private_key.exchange(peer_pub)
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=context,
        )
        return hkdf.derive(shared_secret)

    @staticmethod
    def compute_auth_proof(
        session_key: bytes,
        tag: bytes,
        password_hash: str,
        salt: bytes,
        client_eph: bytes,
        host_eph: bytes
    ) -> bytes:
        """Compute transcript-bound HMAC mutual authentication proof."""
        h = hmac.HMAC(session_key, hashes.SHA256())
        h.update(tag)
        h.update(password_hash.encode("utf-8"))
        h.update(salt)
        h.update(client_eph)
        h.update(host_eph)
        return h.finalize()

    @staticmethod
    def compute_host_fingerprint(pubkey_hex: str) -> str:
        """
        Compute human-verifiable full 32-byte SHA-256 fingerprint for an Ed25519 public key.
        Outputs all 32 bytes (64 hex characters) formatted with colons (XX:XX:...:XX)
        to prevent truncated fingerprint collisions and broker impersonation.
        """
        try:
            raw = bytes.fromhex(pubkey_hex.strip())
            digest = hashlib.sha256(raw).digest()
            return ":".join(f"{b:02X}" for b in digest)
        except Exception:
            return "INVALID_KEY"

    def check_host_pin_status(self, device_id: str, host_pubkey_hex: str) -> tuple[str, str]:
        """
        Check if host public key is verified against persistent known_hosts.
        Returns:
            ("PINNED_OK", fingerprint) if key matches pinned key.
            ("UNPINNED", fingerprint) if host is not yet pinned (requires user fingerprint confirmation).
            ("KEY_CHANGED", fingerprint) if host key differs from pinned key (MITM attack / broker tampering).
        """
        clean_id = device_id.replace(" ", "").strip()
        pub_clean = host_pubkey_hex.strip().lower()
        fingerprint = self.compute_host_fingerprint(pub_clean)
        if not clean_id or not pub_clean:
            return "INVALID_PARAM", fingerprint

        known = self.config.setdefault("known_hosts", {})
        if clean_id in known:
            pinned = known[clean_id].strip().lower()
            if pinned == pub_clean:
                return "PINNED_OK", fingerprint
            else:
                logger.critical(
                    f"SECURITY ALERT: Host key changed for {clean_id}! "
                    f"Expected {pinned[:16]}..., got {pub_clean[:16]}... Possible MITM or broker tampering!"
                )
                return "KEY_CHANGED", fingerprint

        return "UNPINNED", fingerprint

    def pin_host_key(self, device_id: str, host_pubkey_hex: str) -> bool:
        """
        Persistently pin host public key after explicit user verification.
        Refuses to overwrite an existing pinned key if it differs (prevents silent downgrade).
        """
        clean_id = device_id.replace(" ", "").strip()
        pub_clean = host_pubkey_hex.strip().lower()
        if not clean_id or not pub_clean:
            return False

        known = self.config.setdefault("known_hosts", {})
        if clean_id in known and known[clean_id].strip().lower() != pub_clean:
            logger.error(f"Cannot overwrite pinned key for {clean_id} without explicit removal.")
            return False

        known[clean_id] = pub_clean
        self._save_config()
        logger.info(f"Explicitly pinned host identity key for {clean_id}: {pub_clean[:16]}...")
        return True

    def verify_and_pin_host_key(self, device_id: str, host_pubkey_hex: str) -> tuple[bool, str]:
        """
        Compatibility method for host key verification.
        Returns (True, "VERIFIED"), (True, "FIRST_SEEN"), or (False, "KEY_CHANGED").
        """
        status, fp = self.check_host_pin_status(device_id, host_pubkey_hex)
        if status in ("PINNED_OK", "VERIFIED"):
            return True, "VERIFIED"
        elif status == "UNPINNED":
            self.pin_host_key(device_id, host_pubkey_hex)
            return True, "FIRST_SEEN"
        else:
            return False, "KEY_CHANGED"

    def get_known_host(self, device_id: str) -> Optional[str]:
        """Return pinned public key for device_id if present."""
        clean_id = device_id.replace(" ", "").strip()
        return self.config.get("known_hosts", {}).get(clean_id)

    def remove_known_host(self, device_id: str) -> bool:
        """Remove pinned host key to permit manual re-pairing."""
        clean_id = device_id.replace(" ", "").strip()
        known = self.config.get("known_hosts", {})
        if clean_id in known:
            del known[clean_id]
            self._save_config()
            return True
        return False

    def set_password(self, password: str, iterations: int = 600_000) -> None:
        """Derive master key and persist credentials protected with DPAPI."""
        if self.dpapi_engine is None:
            raise RuntimeError(
                "Secure credential protection engine unavailable on this platform. "
                "Cannot persist credentials at rest without a secure DPAPI provider."
            )

        salt = secrets.token_bytes(16)
        master_key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        pwd_hash = hashlib.sha256(master_key).hexdigest()

        # OS-level secret protection via Windows DPAPI (eliminates plaintext storage)
        dpapi_blob = self.dpapi_engine.protect(master_key)
        dpapi_pwd = self.dpapi_engine.protect(password.encode("utf-8"))

        if dpapi_blob is None or dpapi_pwd is None:
            raise RuntimeError(
                "DPAPI protection failed. Cannot securely persist credentials at rest."
            )

        self.config["pwd_salt"] = salt.hex()
        self.config["iterations"] = iterations
        self.config["pwd_hash"] = pwd_hash
        self.config["master_key_dpapi"] = dpapi_blob.hex()
        self.config["password_display_dpapi"] = dpapi_pwd.hex()

        # Remove plaintext credentials from config
        self.config.pop("master_key", None)
        self.config.pop("password_display", None)

        self._save_config()

    def get_password_display(self) -> str:
        """Returns the configured password via DPAPI, or provisions a strong random password if none exists."""
        # Check legacy unmigrated field first if present
        if "password_display" in self.config and self.config["password_display"]:
            pwd = self.config["password_display"]
            self._migrate_credentials()
            return pwd

        if "password_display_dpapi" in self.config and self.config["password_display_dpapi"]:
            if self.dpapi_engine is None:
                raise RuntimeError(
                    "DPAPI recovery failed: DPAPI engine not available on this platform to unprotect credentials."
                )
            try:
                raw_dpapi = bytes.fromhex(self.config["password_display_dpapi"])
                unprot = self.dpapi_engine.unprotect(raw_dpapi)
                if unprot is not None:
                    return unprot.decode("utf-8")
            except Exception as e:
                logger.error(f"DPAPI password recovery error: {e}")

            logger.error(
                "Impossibile decifrare password_display_dpapi esistente tramite DPAPI. "
                "Blob corrotto o chiavi DPAPI non accessibili."
            )
            raise RuntimeError(
                "DPAPI recovery failed: impossibile recuperare la password configurata. "
                "Le credenziali esistenti non verranno sovrascritte automaticamente; "
                "e' necessario un recupero o reset manuale esplicito."
            )

        # If no password exists, provision a strong random password (no weak default '123456')
        if self.dpapi_engine is None:
            raise RuntimeError(
                "Cannot generate and store initial password: secure DPAPI engine is unavailable on this platform."
            )
        new_pwd = self._generate_secure_password()
        self.set_password(new_pwd)
        return new_pwd

    def get_master_key(self) -> bytes | None:
        """Retrieve stored master key, decrypting DPAPI container if present."""
        if "master_key_dpapi" in self.config and self.config["master_key_dpapi"]:
            if self.dpapi_engine is None:
                logger.error("master_key_dpapi exists but no DPAPI engine is available on this platform.")
                return None
            try:
                raw_dpapi = bytes.fromhex(self.config["master_key_dpapi"])
                unprotected = self.dpapi_engine.unprotect(raw_dpapi)
                if unprotected is not None:
                    return unprotected
            except Exception as e:
                logger.warning(f"Failed to unprotect master_key_dpapi: {e}")
            logger.error("master_key_dpapi exists but could not be decrypted by DPAPI engine.")
            return None

        if "master_key" in self.config and self.config["master_key"]:
            mk = bytes.fromhex(self.config["master_key"])
            self._migrate_credentials()
            return mk

        pwd_salt = self.config.get("pwd_salt")
        if pwd_salt and ("password_display_dpapi" in self.config or "password_display" in self.config):
            try:
                pwd = self.get_password_display()
                if pwd:
                    salt = bytes.fromhex(pwd_salt)
                    iterations = self.config.get("iterations", 600_000)
                    return self.derive_master_key(pwd, salt, iterations)
            except Exception:
                pass
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
