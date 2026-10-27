import os
import sys
import time
import shutil
import hashlib
import logging
import subprocess
import urllib.request
import urllib.parse
import json
import tempfile
from enum import Enum
from pathlib import Path
from typing import Optional, Callable, Tuple

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.exceptions import InvalidSignature

logger = logging.getLogger("OTA")

CURRENT_VERSION = "1.5.0"
GITHUB_REPO = "ManuRoma02/AetherDesk"

# Embedded Vendor Public Key for OTA Manifest Verification (Ed25519, 32 bytes raw / 64 hex characters).
# The corresponding private key is strictly kept OFFLINE and outside the codebase/repository.
# See docs/OTA_RELEASE_WORKFLOW.md for instructions on configuring your personal offline keypair.
OTA_VENDOR_PUBLIC_KEY_HEX = "18e57b653337e30326f563ce282f1a185feb844e453540de039738738ee429b7"
OTA_STAGING_DIR_NAME = "aether_ota_staging"
OTA_TEMP_FILE_PREFIX = "aether_ota_"

def get_ota_staging_dir() -> Path:
    """Returns the dedicated staging directory for OTA downloaded binaries."""
    staging_dir = Path(tempfile.gettempdir()) / OTA_STAGING_DIR_NAME
    staging_dir.mkdir(parents=True, exist_ok=True)
    return staging_dir

def is_valid_hex(val: str, expected_len: int) -> bool:
    """Check if string has exactly expected_len characters and consists only of hexadecimal digits."""
    if not isinstance(val, str) or len(val) != expected_len:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in val)

class OTAUpdateState(str, Enum):
    """
    Standard update state machine adhering to OTA firmware & software principles:
    Dual-slot staging, cryptographic verification, and failsafe rollback.
    """
    IDLE = "IDLE"
    CHECKING = "CHECKING"
    DOWNLOADING = "DOWNLOADING"
    VERIFYING = "VERIFYING"
    STAGING = "STAGING"
    PENDING_REBOOT = "PENDING_REBOOT"
    TESTING = "TESTING"
    CONFIRMED = "CONFIRMED"
    REVERTING = "REVERTING"
    FAILED = "FAILED"

def get_app_version() -> str:
    """Return the current application semantic version."""
    return CURRENT_VERSION

def parse_version(v_str: str) -> Tuple[int, int, int]:
    """
    Parse and strictly validate a semantic version string in major.minor.patch format.

    Project convention:
    - Accepts strictly: '[vV]?major.minor.patch' where major, minor, patch are
      non-negative integers represented exclusively by ASCII decimal digits '0'-'9'.
    - An optional single leading 'v' or 'V' prefix is supported intentionally to ensure
      compatibility with standard git release tags (e.g. 'v1.4.0', 'V1.4.0').
    - Leading, trailing, and internal whitespace is strictly forbidden (rejects external spaces).
    - Unicode digits (e.g. Arabic-Indic, fullwidth, superscripts) are strictly rejected.
    - Missing or extra components (must be exactly 3 components) are rejected.
    - Leading zeros in multi-digit components (e.g. '01.4.0') are rejected (single '0' is valid).
    - Prerelease tags, build metadata, or suffixes (e.g. '-beta', '+build') are rejected.
    """
    if not isinstance(v_str, str):
        raise ValueError(f"La versione deve essere una stringa, ricevuto {type(v_str).__name__}.")

    if not v_str:
        raise ValueError("La stringa di versione non puo' essere vuota.")

    # Strictly reject any whitespace (external leading/trailing, internal, newlines, tabs)
    if any(c.isspace() for c in v_str):
        raise ValueError(f"Spazi non consentiti nella stringa di versione: '{v_str}'.")

    clean = v_str
    # Allow optional single leading 'v' or 'V' prefix for git tag compatibility
    if clean.startswith(("v", "V")):
        clean = clean[1:]
        if not clean:
            raise ValueError("Prefisso di versione privo di componenti numerici.")
        if clean.startswith(("v", "V")):
            raise ValueError(f"Prefisso di versione duplicato non consentito: '{v_str}'.")

    # Reject prerelease tags, suffixes, or build metadata explicitly
    if any(sep in clean for sep in ("-", "+", "_", "~")):
        raise ValueError(f"Suffissi di prerelease o caratteri non consentiti nella versione: '{v_str}'.")

    parts = clean.split(".")
    if len(parts) != 3:
        raise ValueError(
            f"Formato versione non valido: attesi esattamente 3 componenti (major.minor.patch), trovati {len(parts)} in '{v_str}'."
        )

    validated_parts = []
    for part in parts:
        if not part:
            raise ValueError(f"Componente di versione vuoto in '{v_str}'.")
        # Strictly require ASCII decimal digits '0'-'9' (reject Unicode numerals)
        if not all('0' <= c <= '9' for c in part):
            raise ValueError(f"Componente di versione non numerico ASCII '{part}' in '{v_str}'.")
        # Reject leading zeros in multi-digit numbers (e.g., '01', '00', '000')
        if len(part) > 1 and part.startswith("0"):
            raise ValueError(f"Zeri iniziali non ammessi nel componente '{part}' in '{v_str}'.")
        validated_parts.append(int(part))

    return (validated_parts[0], validated_parts[1], validated_parts[2])

def is_newer_version(remote_v: str, local_v: str = CURRENT_VERSION) -> bool:
    """Compare remote version against local version (Anti-Rollback rule)."""
    try:
        r_parts = parse_version(remote_v)
        l_parts = parse_version(local_v)
        return r_parts > l_parts
    except Exception:
        return False

def compute_file_sha256(filepath: str | Path) -> str:
    """Calculate cryptographic SHA256 checksum of an executable binary."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def get_host_dist_executable() -> Optional[Path]:
    """Find the latest compiled executable available on host."""
    candidates = [
        Path(os.environ.get("TEMP", "")) / "aether_dist" / "AetherDesk.exe",
        Path(__file__).resolve().parent.parent / "dist" / "AetherDesk.exe",
        Path(__file__).resolve().parent.parent / "dist" / "RemoteClient.exe",
        Path(os.environ.get("USERPROFILE", "")) / "Downloads" / "AetherDesk.exe",
    ]
    existing = [p for p in candidates if p.exists() and p.is_file() and p.stat().st_size > 1_000_000]
    if existing:
        return max(existing, key=lambda p: p.stat().st_mtime)
    return None

def build_manifest_canonical_bytes(
    version: str,
    filename: str,
    sha256: str,
    size: Optional[int] = None
) -> bytes:
    """
    Construct canonical, deterministic byte sequence for cryptographic signature verification.
    Binds version, asset filename, SHA-256 digest, and optional size together.
    """
    clean_version = str(version).strip()
    clean_filename = Path(filename).name.strip()
    clean_sha = str(sha256).lower().strip()
    size_str = str(size) if size is not None else ""
    return f"AETHER_OTA:{clean_version}:{clean_filename}:{clean_sha}:{size_str}".encode("utf-8")

def verify_manifest_signature(
    manifest: dict,
    public_key_hex: Optional[str] = None
) -> Tuple[bool, str]:
    """
    Cryptographically verify digital signature of an update manifest using Ed25519.
    Fail-closed: returns (False, reason) if signature is missing, malformed, or invalid.
    """
    if not isinstance(manifest, dict):
        return False, "Manifest non valido (atteso dizionario JSON)."

    sig_hex = manifest.get("signature") or manifest.get("signature_hex")
    if not sig_hex or not isinstance(sig_hex, str):
        return False, "Firma digitale assente nel manifest (rifiuto fail-closed)."

    if not is_valid_hex(sig_hex.strip(), 128):
        return False, "Firma digitale non valida (attesi esattamente 128 caratteri esadecimali per Ed25519)."

    version = manifest.get("version", "")
    filename = manifest.get("filename", "")
    sha = manifest.get("sha256", "")
    size = manifest.get("size")

    if not version or not filename or not sha:
        return False, "Manifest incompleto: versione, filename o sha256 mancanti."

    target_pub_hex = (public_key_hex or OTA_VENDOR_PUBLIC_KEY_HEX).strip()
    if not is_valid_hex(target_pub_hex, 64):
        return False, "Chiave pubblica Ed25519 non valida (attesi esattamente 64 caratteri esadecimali)."

    try:
        pub_bytes = bytes.fromhex(target_pub_hex)
        sig_bytes = bytes.fromhex(sig_hex.strip())
        canonical_data = build_manifest_canonical_bytes(version, filename, sha, size)
        pub_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_bytes)
        pub_key.verify(sig_bytes, canonical_data)
        return True, "Firma digitale Ed25519 verificata con successo."
    except InvalidSignature:
        return False, "Firma digitale non valida: manifest manomesso o non autentico."
    except Exception as e:
        return False, f"Errore durante la verifica crittografica della firma: {e}"

def sign_update_manifest(
    manifest_data: dict,
    private_key: ed25519.Ed25519PrivateKey
) -> dict:
    """
    Sign an update manifest using an Ed25519 private key.
    Used exclusively by release build pipelines (key outside repo) or unit tests.
    """
    version = manifest_data.get("version", "")
    filename = manifest_data.get("filename", "")
    sha = manifest_data.get("sha256", "")
    size = manifest_data.get("size")
    canonical_data = build_manifest_canonical_bytes(version, filename, sha, size)
    sig_bytes = private_key.sign(canonical_data)
    signed = dict(manifest_data)
    signed["signature"] = sig_bytes.hex()
    return signed

def validate_executable_file(
    filepath: str | Path,
    expected_sha256: Optional[str] = None,
    signed_manifest: Optional[dict] = None
) -> Tuple[bool, str]:
    """
    Verify integrity, format and cryptographic digest of binary before staging/flashing.
    Ensures minimum size (>1MB), valid Windows PE header (MZ magic),
    and exact SHA-256 hash match against verified manifest or expected digest.
    """
    p = Path(filepath)
    if not p.exists() or not p.is_file():
        return False, "File di aggiornamento non trovato."
    if p.stat().st_size < 1_000_000:
        return False, "File incompleto o corrotto (< 1 MB)."
    try:
        with open(p, "rb") as f:
            header = f.read(2)
            if header != b"MZ":
                return False, "Intestazione binaria non valida (manca magic header MZ/PE)."
    except Exception as e:
        return False, f"Impossibile verificare l'integrita' del file: {e}"

    if signed_manifest:
        man_ok, man_msg = verify_update_manifest(signed_manifest)
        if not man_ok:
            return False, f"Manifest di autenticazione non valido: {man_msg}"
        expected_sha256 = signed_manifest.get("sha256")
        expected_size = signed_manifest.get("size")
        if expected_size is not None and p.stat().st_size != expected_size:
            return False, f"Dimensione file non corrispondente: attesi {expected_size} byte, trovati {p.stat().st_size} byte."

    if expected_sha256:
        actual_sha = compute_file_sha256(p).lower().strip()
        expected = str(expected_sha256).lower().strip()
        if actual_sha != expected:
            return False, f"Verifica integrita' SHA-256 fallita: atteso {expected[:16]}..., calcolato {actual_sha[:16]}..."

    return True, "Eseguibile valido."

def verify_update_manifest(
    manifest: dict,
    local_version: str = CURRENT_VERSION,
    allow_downgrade: bool = False,
    public_key_hex: Optional[str] = None,
    require_signature: bool = True
) -> Tuple[bool, str]:
    """
    Audit and cryptographically verify update manifest before downloading.
    Enforces strict schema validation:
    - version: non-empty string parsable as semver, subject to Anti-Rollback policy
    - filename: non-empty basename ending in .exe without directory traversal
    - sha256: exactly 64 hexadecimal characters
    - size: integer >= 1_000_000 bytes (if present)
    - signature: exactly 128 hexadecimal characters (Ed25519) verified against vendor public key
    """
    if not isinstance(manifest, dict):
        return False, "Manifest non valido (atteso dizionario JSON)."

    raw_version = manifest.get("version")
    if raw_version is None:
        return False, "Manifest privo di versione."
    if not isinstance(raw_version, str):
        return False, f"Versione nel manifest non valida: attesa stringa, ricevuto {type(raw_version).__name__}."
    if not raw_version:
        return False, "Manifest con versione vuota."

    try:
        remote_parts = parse_version(raw_version)
    except ValueError as e:
        return False, f"Formato versione manifest non valido: {e}"

    try:
        local_parts = parse_version(local_version)
    except ValueError as e:
        return False, f"Versione locale non valida: {e}"

    if not allow_downgrade and remote_parts <= local_parts:
        return False, (
            f"Anti-Rollback attivo: la versione remota ({raw_version}) "
            f"non e' piu' recente di quella installata ({local_version})."
        )

    # Asset filename validation
    filename = str(manifest.get("filename", "")).strip()
    if not filename:
        return False, "Manifest privo del nome asset eseguibile ('filename')."
    clean_filename = Path(filename).name
    if clean_filename != filename or ("/" in filename) or ("\\" in filename) or (".." in filename):
        return False, f"Nome asset non valido (path traversal o separatori non consentiti): '{filename}'."
    if not filename.lower().endswith(".exe"):
        return False, f"Nome asset non valido (richiesta estensione .exe): '{filename}'."

    # SHA-256 digest validation (exactly 64 hex characters)
    sha = str(manifest.get("sha256", "")).strip()
    if not sha:
        return False, "Manifest privo di digest SHA-256."
    if not is_valid_hex(sha, 64):
        return False, f"Digest SHA-256 non valido: attesi esattamente 64 caratteri esadecimali, ricevuti {len(sha)}."

    # Asset size validation (minimum 1 MB)
    size = manifest.get("size")
    if size is not None:
        if not isinstance(size, int) or size < 1_000_000:
            return False, f"Dimensione asset non valida nel manifest: atteso intero >= 1.000.000 byte, ricevuto {size}."

    if require_signature:
        sig_ok, sig_err = verify_manifest_signature(manifest, public_key_hex=public_key_hex)
        if not sig_ok:
            return False, f"Verifica autenticita' manifest fallita: {sig_err}"

    return True, "Manifest verificato con successo."

def cleanup_old_versions(directory: Optional[Path] = None, force: bool = False) -> int:
    """
    Purges previous version backups, watchdog scripts, and temporary staging files
    strictly owned and identifiable as AetherDesk OTA artifacts.
    Prevents over-broad deletion: generic patterns (*.old, *.bak, *.exe.old) are strictly avoided,
    ensuring files belonging to other applications or the user (e.g., altri_app.exe.old,
    documento.bak, backup.old) are never touched.

    SAFETY GUARD:
    - If force is False and the application is currently in TESTING state
      (newly booted after an OTA update and awaiting self-test verification),
      this function strictly PRESERVES the active .exe.old backup slot to prevent
      destroying the rollback safety net.
    - confirm_boot() writes a unique confirmation marker (.ota_confirmed_{PID}) that the
      watchdog supervisor consumes to safely delete the backup slot.
    - The confirmation marker of the active running process (.ota_confirmed_{os.getpid()})
      is strictly PRESERVED even when force=True, ensuring it remains available on disk until
      the watchdog supervisor detects and consumes it in the normal flow.
    - force=True purges the backup slot (.exe.old) and leftover confirmation markers from
      previous/other PIDs.
    """
    removed_count = 0
    try:
        base_dir = directory or Path(sys.executable).resolve().parent
        if not base_dir.is_dir():
            return 0

        current_exe = Path(sys.executable).resolve()
        active_backup = current_exe.with_suffix(".exe.old")
        active_backup_alt = current_exe.with_suffix(".old")
        is_testing = (not force) and (check_boot_state() == OTAUpdateState.TESTING)

        # 1. Candidate backups strictly owned by AetherDesk (derived from current_exe or standard name)
        candidate_backup_names = set()
        for exe_name in {current_exe.name, "AetherDesk.exe"}:
            p = Path(exe_name)
            candidate_backup_names.add(p.with_suffix(".exe.old").name)
            candidate_backup_names.add(p.with_suffix(".old").name)
            candidate_backup_names.add(f"{exe_name}.old")

        # 2. Strict owned candidate files to examine
        candidates_to_clean: list[Path] = []

        # Add exact owned backup files if not in TESTING state
        if not is_testing:
            for b_name in candidate_backup_names:
                f = base_dir / b_name
                if f.is_file():
                    candidates_to_clean.append(f)

        # 3. Specific owned staging files
        for s_name in {f"~{current_exe.name}.new", "~AetherDesk.exe.new"}:
            f = base_dir / s_name
            if f.is_file():
                candidates_to_clean.append(f)

        for pat in ("aether_ota_staging*.exe", "aether_update_staging*.exe"):
            candidates_to_clean.extend(base_dir.glob(pat))

        # 4. Owned scripts generated with explicit OTA prefixes
        for pat in ("ota_watchdog*.bat", "aether_restart_*.bat"):
            candidates_to_clean.extend(base_dir.glob(pat))

        # 5. Protocol markers
        for marker in base_dir.glob(".ota_ready_*"):
            if marker.is_file():
                candidates_to_clean.append(marker)

        for marker in base_dir.glob(".ota_confirmed_*"):
            if marker.is_file():
                # Guard confirmation markers in TESTING state
                if is_testing:
                    continue
                # Always preserve active running process's confirmation marker for watchdog
                if marker.name == f".ota_confirmed_{os.getpid()}":
                    continue
                candidates_to_clean.append(marker)

        # De-duplicate while preserving Path objects
        seen_paths = set()
        for old_file in candidates_to_clean:
            try:
                resolved = old_file.resolve()
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)

                # Extra safeguard: never delete the active executable itself
                if old_file.name in (current_exe.name, "AetherDesk.exe"):
                    continue

                # Extra safeguard: guard active backup slot while in TESTING state
                if is_testing:
                    try:
                        if resolved in (active_backup.resolve(), active_backup_alt.resolve()):
                            continue
                    except Exception:
                        if old_file.name in (active_backup.name, active_backup_alt.name):
                            continue

                if old_file.is_file():
                    old_file.unlink()
                    removed_count += 1
                    logger.info(f"File versione precedente eliminato con successo: {old_file.name}")
            except Exception as ex:
                logger.debug(f"Impossibile rimuovere {old_file}: {ex}")
    except Exception as e:
        logger.warning(f"Errore durante cleanup_old_versions: {e}")
    return removed_count

def check_boot_state() -> OTAUpdateState:
    """
    Detect whether the running application is currently in TESTING state
    (i.e., newly booted after an OTA swap with a backup slot still present).

    REALISTIC RUNTIME BEHAVIOR:
    - This function executes ONLY when the application process has already started.
    - If the primary executable (current_exe) is physically missing on disk,
      Windows kernel loader will refuse to launch the process (ERROR_FILE_NOT_FOUND).
      In that event, check_boot_state() cannot run on its own; recovery across an
      interrupted power loss requires an external launcher/service or manual rename of .exe.old.
    - If invoked by an administrative script or installer tool when current_exe is missing
      but .exe.old exists, it performs a fallback restoration of .exe.old to current_exe.
    """
    current_exe = Path(sys.executable).resolve()
    old_backup = current_exe.with_suffix(".exe.old")
    old_backup_alt = current_exe.with_suffix(".old")

    # Diagnostic / helper recovery: only reachable if an external helper or python environment invokes this
    if getattr(sys, "frozen", False) and not current_exe.exists():
        backup_to_restore = old_backup if old_backup.exists() else (old_backup_alt if old_backup_alt.exists() else None)
        if backup_to_restore:
            try:
                os.rename(str(backup_to_restore), str(current_exe))
                logger.warning(
                    f"Ripristino helper eseguito in check_boot_state: ripristinato {backup_to_restore.name} -> {current_exe.name}"
                )
            except Exception as ex_rec:
                logger.critical(
                    f"Ripristino fallito in check_boot_state: impossibile ripristinare {backup_to_restore}: {ex_rec}"
                )

    if getattr(sys, "frozen", False) and (old_backup.exists() or old_backup_alt.exists()):
        return OTAUpdateState.TESTING
    return OTAUpdateState.IDLE

def confirm_boot() -> bool:
    """
    Self-Test Confirmation:
    Called once the application completes startup, renders UI, and passes health checks.
    Leaves a unique positive confirmation signal (.ota_confirmed_{PID}) on disk
    so the watchdog supervisor can detect it, consume it, and safely purge the backup slot.
    """
    try:
        current_exe = Path(sys.executable).resolve()
        pid = os.getpid()
        confirm_marker = current_exe.parent / f".ota_confirmed_{pid}"
        try:
            confirm_marker.write_text("confirmed", encoding="utf-8")
        except Exception as ex_m:
            logger.warning(f"Impossibile creare il marker di conferma {confirm_marker}: {ex_m}")
            return False

        logger.info(f"OTA Update confermato per PID {pid}: marker di conferma emesso ({confirm_marker.name}).")
        return True
    except Exception as e:
        logger.error(f"Errore durante confirm_boot: {e}")
        return False

def ensure_watchdog_terminated(
    proc: Optional[subprocess.Popen],
    term_timeout: float = 2.0,
    kill_timeout: float = 1.0
) -> bool:
    """
    Terminates the watchdog process deterministically and verifies its exit state.

    Protocol:
    1. If proc is None: returns True (no active process).
    2. Check initial poll(): if proc has already exited, return True.
    3. Attempt terminate() and bounded wait(timeout=term_timeout).
    4. If wait times out or raises an error, attempt fallback kill() and bounded wait(timeout=kill_timeout).
    5. Positively verify that proc.poll() is not None.

    Returns:
    - True: termination is positively confirmed by proc.poll().
    - False: the process is still running, poll() returned None, or termination could not be verified.
    """
    if proc is None:
        return True

    # 1. Initial check: has it already exited?
    try:
        if hasattr(proc, "poll") and proc.poll() is not None:
            return True
    except Exception as e_poll:
        logger.debug(f"Errore poll iniziale watchdog: {e_poll}")

    # 2. Attempt terminate()
    try:
        if hasattr(proc, "terminate"):
            proc.terminate()
    except Exception as e_term:
        logger.warning(f"Errore durante terminate() del watchdog: {e_term}")

    # 3. Wait with bounded timeout
    try:
        if hasattr(proc, "wait"):
            proc.wait(timeout=term_timeout)
    except Exception as e_twait:
        logger.debug(f"Attesa post-terminate del watchdog non completata ({e_twait}); tentato kill()")

    # 4. Check if exited after terminate
    try:
        if hasattr(proc, "poll") and proc.poll() is not None:
            return True
    except Exception:
        pass

    # 5. Attempt fallback kill()
    try:
        if hasattr(proc, "kill"):
            proc.kill()
    except Exception as e_kill:
        logger.warning(f"Errore durante kill() del watchdog: {e_kill}")

    # 6. Wait with bounded timeout after kill
    try:
        if hasattr(proc, "wait"):
            proc.wait(timeout=kill_timeout)
    except Exception as e_kwait:
        logger.warning(f"Attesa post-kill del watchdog non completata o scaduta: {e_kwait}")

    # 7. Final positive confirmation via poll()
    try:
        if hasattr(proc, "poll"):
            is_dead = (proc.poll() is not None)
            if is_dead:
                return True
            logger.critical("Watchdog ancora attivo dopo terminate() e kill() (poll() ha restituito None).")
            return False
    except Exception as e_fpoll:
        logger.critical(f"Impossibile verificare lo stato del watchdog tramite poll(): {e_fpoll}")
        return False

    return False

def apply_in_place_update(
    downloaded_temp_path: str,
    target_version: Optional[str] = None,
    allow_downgrade: bool = False,
    destination_path: Optional[str | Path] = None,
    signed_manifest: Optional[dict] = None,
    expected_sha256: Optional[str] = None,
    allow_unverified_dev_hash: bool = False,
    readiness_timeout: float = 3.0
) -> Tuple[bool, str]:
    """
    Replace running executable following Same-Volume Staging & Supervised A/B architecture
    with Anti-Rollback check, failsafe watchdog monitoring, and automatic rollback.

    Guarantees:
    - Strict ownership of downloaded_temp_path: deleted only if it belongs strictly
      to get_ota_staging_dir() and matches the expected naming convention (aether_ota_*.exe).
      Arbitrary paths outside staging are strictly never deleted.
    - Same-volume staging: the validated binary is staged on the same volume as the target
      executable (~{name}.new) and re-verified prior to launching the supervisor.
    - Supervisor ready BEFORE touching active binary: the detached watchdog supervisor is
      spawned while current_exe is completely intact. If spawning fails, current_exe is untouched.
    - Watchdog readiness handshake and verified exit: verifies that the supervisor batch initializes
      its recovery environment and writes a unique readiness marker before the caller
      considers the supervisor ready. If the marker is not produced within readiness_timeout
      or an error occurs, ensure_watchdog_terminated() attempts terminate() and fallback kill(),
      verifying with poll() that the watchdog has exited. If termination is confirmed, attempt
      artifacts are safely unlinked. If termination cannot be confirmed, supervisor artifacts
      (ready_marker, restart_bat, staged_target) and active binaries are strictly preserved to
      prevent race conditions with a lingering supervisor, while the independent downloaded
      temp file is safely cleaned up.
    - Caller exit timeout in watchdog: the supervisor waits with a bounded timeout (30s)
      for the caller PID to exit. If the caller does not exit, the swap is cancelled safely
      without touching current_exe.
    - Staging missing protection: if ~{name}.new is missing when swap executes, the supervisor
      logs a critical error, restarts current_exe, and exits without entering update_ok.
    - Backup failure recovery: if moving current_exe to old_backup fails, the supervisor preserves
      both files, restarts the verified intact executable, and exits with code 1.
    - Supervised rollback: if the new binary crashes or fails boot confirmation, the watchdog
      terminates exclusively NEW_PID, safely restores old_backup, verifies the restore, and
      if restore fails, preserves old_backup and leaves a clear diagnostic log (.ota_rollback_failure.log).
    """
    temp_p = Path(downloaded_temp_path).resolve()
    current_exe = Path(sys.executable).resolve()
    old_backup = current_exe.with_suffix(".exe.old")
    old_backup_alt = current_exe.with_suffix(".old")
    staged_target = current_exe.parent / f"~{current_exe.name}.new"
    staging_dir = get_ota_staging_dir().resolve()
    watchdog_proc = None
    ready_marker = None
    restart_bat = None

    def _safe_cleanup_temp():
        try:
            if not temp_p.is_file():
                return
            # Strict safety guard: never delete active executable, backup slot, or staged target
            if temp_p in (current_exe, old_backup, old_backup_alt, staged_target):
                logger.warning(
                    f"_safe_cleanup_temp: evitato tentativo di eliminazione di binario vitale ({temp_p})"
                )
                return

            # Strict ownership validation:
            # 1. Path must be strictly inside dedicated OTA staging directory
            try:
                resolved_parent = temp_p.resolve().parent
                is_strictly_in_staging = (resolved_parent == staging_dir)
            except Exception:
                is_strictly_in_staging = False

            # 2. Filename must have OTA prefix and .exe extension
            has_expected_format = (
                temp_p.name.startswith(OTA_TEMP_FILE_PREFIX)
                and temp_p.suffix.lower() == ".exe"
            )

            # Both conditions must hold: strict directory containment AND expected naming format
            if is_strictly_in_staging and has_expected_format:
                temp_p.unlink()
                logger.debug(f"_safe_cleanup_temp: file temporaneo OTA eliminato ({temp_p.name})")
            else:
                logger.warning(
                    f"_safe_cleanup_temp: rifiutata eliminazione per mancata ownership "
                    f"(in_staging={is_strictly_in_staging}, format={has_expected_format}): {temp_p}"
                )
        except Exception as ex_clean:
            logger.warning(f"Impossibile eliminare il file temporaneo {temp_p}: {ex_clean}")

    try:
        is_frozen = getattr(sys, "frozen", False)

        if is_frozen and check_boot_state() == OTAUpdateState.TESTING:
            _safe_cleanup_temp()
            return False, (
                "Rifiuto aggiornamento fail-closed: un aggiornamento precedente e' ancora "
                "in fase di verifica (stato TESTING). Completare e confermare l'avvio "
                "prima di applicare un nuovo aggiornamento."
            )

        if is_frozen:
            if allow_unverified_dev_hash:
                _safe_cleanup_temp()
                return False, "Rifiuto aggiornamento fail-closed: il bypass dev non e' consentito in un binario frozen di produzione."
            if not signed_manifest:
                _safe_cleanup_temp()
                return False, "Rifiuto aggiornamento fail-closed: in produzione frozen e' richiesto obbligatoriamente un manifest firmato dal fornitore (un expected_sha256 isolato non autentica la provenienza)."
            man_ok, man_msg = verify_update_manifest(signed_manifest, allow_downgrade=allow_downgrade)
            if not man_ok:
                _safe_cleanup_temp()
                return False, f"Rifiuto aggiornamento fail-closed: manifest firmato non valido ({man_msg})."
            expected_sha256 = signed_manifest["sha256"]
            target_version = target_version or signed_manifest.get("version")
        else:
            if signed_manifest:
                man_ok, man_msg = verify_update_manifest(signed_manifest, allow_downgrade=allow_downgrade)
                if not man_ok:
                    _safe_cleanup_temp()
                    return False, f"Rifiuto aggiornamento dev fail-closed: manifest firmato non valido ({man_msg})."
                expected_sha256 = signed_manifest["sha256"]
                target_version = target_version or signed_manifest.get("version")
            elif not allow_unverified_dev_hash:
                _safe_cleanup_temp()
                return False, "Rifiuto aggiornamento dev fail-closed: manifest firmato non fornito (e bypass dev non abilitato esplicitamente)."

        valid, msg = validate_executable_file(temp_p, expected_sha256=expected_sha256, signed_manifest=signed_manifest)
        if not valid:
            _safe_cleanup_temp()
            return False, msg

        # Anti-Rollback enforcement
        if target_version and not allow_downgrade:
            try:
                target_parts = parse_version(target_version)
                current_parts = parse_version(CURRENT_VERSION)
            except ValueError as e:
                _safe_cleanup_temp()
                return False, f"Versione non valida per Anti-Rollback: {e}"
            if target_parts <= current_parts:
                _safe_cleanup_temp()
                return False, (
                    f"Anti-Rollback attivo: la versione {target_version} "
                    f"non e' consentita rispetto a {CURRENT_VERSION}."
                )

        if not is_frozen:
            # Dev mode: copy staging file to destination_path or dist/AetherDesk.exe
            dest = Path(destination_path) if destination_path else (Path(__file__).resolve().parent.parent / "dist" / "AetherDesk.exe")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(temp_p, dest)
            _safe_cleanup_temp()
            return True, f"Aggiornamento completato (Modalita' dev, salvato in {dest})."

        # Check executable filename consistency with signed manifest
        if signed_manifest:
            manifest_filename = Path(str(signed_manifest.get("filename", ""))).name.lower().strip()
            if manifest_filename and current_exe.name.lower() != manifest_filename:
                logger.warning(
                    f"Nome eseguibile locale ('{current_exe.name}') differisce dal nome vincolato "
                    f"nel manifest firmato ('{manifest_filename}'). L'aggiornamento in-place aggiornera' "
                    f"l'eseguibile attivo preservando il nome locale corrente '{current_exe.name}'."
                )

        # Clean preexisting stale files without deleting any currently active backup
        cleanup_old_versions(current_exe.parent, force=False)

        # 1. Same-Volume Staging: copy candidate to same directory as current_exe
        if staged_target.exists():
            try:
                staged_target.unlink()
            except Exception as ex_st:
                _safe_cleanup_temp()
                return False, f"Impossibile rimuovere il file di staging obsoleto ({staged_target}): {ex_st}"

        try:
            shutil.copy2(str(temp_p), str(staged_target))
        except Exception as copy_err:
            if staged_target.exists():
                try:
                    staged_target.unlink()
                except Exception:
                    pass
            _safe_cleanup_temp()
            return False, f"Impossibile copiare il nuovo binario nella staging dello stesso volume ({staged_target}): {copy_err}"

        # Re-verify candidate binary on destination volume before touching current_exe
        valid_staged, msg_staged = validate_executable_file(
            staged_target, expected_sha256=expected_sha256, signed_manifest=signed_manifest
        )
        if not valid_staged:
            if staged_target.exists():
                try:
                    staged_target.unlink()
                except Exception:
                    pass
            _safe_cleanup_temp()
            return False, f"Validazione fallita sul file di staging locale: {msg_staged}"

        # 2. Prepare Failsafe Supervisor Watchdog BEFORE touching current_exe
        pid = os.getpid()
        cmd_args_list = [f'"{a}"' for a in sys.argv[1:]]
        cmd_args_str = " ".join(cmd_args_list)

        if sys.argv[1:]:
            escaped_ps_args = [a.replace("'", "''") for a in sys.argv[1:]]
            ps_args_items = ", ".join(f"'{a}'" for a in escaped_ps_args)
            ps_args_clause = f"-ArgumentList @({ps_args_items})"
        else:
            ps_args_clause = ""

        restart_bat = Path(tempfile.gettempdir()) / f"aether_restart_{pid}.bat"
        diag_log = current_exe.parent / ".ota_rollback_failure.log"
        ready_marker = current_exe.parent / f".ota_ready_{pid}"

        bat_content = (
            "@echo off\r\n"
            f"set _PYI_APPLICATION_HOME_DIR=\r\n"
            f"set _PYI_ARCHIVE_FILE=\r\n"
            f"set _PYI_PARENT_PROCESS_LEVEL=\r\n"
            f"set _PYI_SPLASH_IPC=\r\n"
            f"set _MEIPASS=\r\n"
            f"set _MEIPASS2=\r\n"
            f"set PYINSTALLER_RESET_ENVIRONMENT=1\r\n"
            f"cd /d \"{current_exe.parent}\"\r\n"
            f":: 1. Segnala readiness al processo chiamante\r\n"
            f"echo ready > \"{ready_marker}\"\r\n"
            f":: 2. Attesa terminazione del processo chiamante con timeout\r\n"
            f"set /a caller_wait_count=0\r\n"
            f":wait_exit\r\n"
            f"tasklist /fi \"PID eq {pid}\" /fo csv 2>nul | findstr /i \"\\\"{pid}\\\"\" >nul\r\n"
            f"if errorlevel 1 goto caller_exited\r\n"
            f"timeout /t 1 /nobreak >nul\r\n"
            f"set /a caller_wait_count+=1\r\n"
            f"if %caller_wait_count% geq 30 goto caller_timed_out\r\n"
            f"goto wait_exit\r\n"
            f":caller_timed_out\r\n"
            f"echo [WARNING] Watchdog: Timeout (30s) in attesa della chiusura del processo chiamante PID {pid}. Swap annullato. > \"{diag_log}\"\r\n"
            f"echo Timestamp: %DATE% %TIME% >> \"{diag_log}\"\r\n"
            f"del /f /q \"{ready_marker}\" 2>nul\r\n"
            f"del /f /q \"{staged_target}\" 2>nul\r\n"
            f"goto cleanup\r\n"
            f":caller_exited\r\n"
            f"del /f /q \"{ready_marker}\" 2>nul\r\n"
            f"timeout /t 1 /nobreak >nul\r\n"
            f":do_same_volume_swap\r\n"
            f"if not exist \"{staged_target}\" goto staging_missing\r\n"
            f"if exist \"{old_backup}\" del /f /q \"{old_backup}\" 2>nul\r\n"
            f"if exist \"{old_backup_alt}\" del /f /q \"{old_backup_alt}\" 2>nul\r\n"
            f"if exist \"{current_exe}\" (\r\n"
            f"    move /y \"{current_exe}\" \"{old_backup}\" >nul 2>nul\r\n"
            f"    if errorlevel 1 goto swap_failed_backup\r\n"
            f"    if not exist \"{old_backup}\" goto swap_failed_backup\r\n"
            f")\r\n"
            f"move /y \"{staged_target}\" \"{current_exe}\" >nul 2>nul\r\n"
            f"if errorlevel 1 goto swap_failed_install\r\n"
            f"if not exist \"{current_exe}\" goto swap_failed_install\r\n"
            f"if not exist \"{current_exe}\" goto do_rollback\r\n"
            f"set NEW_PID=\r\n"
            f"for /f \"usebackq tokens=*\" %%p in (`powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command \"$ErrorActionPreference='Stop'; (Start-Process -FilePath '{current_exe}' {ps_args_clause} -PassThru).Id\" 2^>nul`) do set NEW_PID=%%p\r\n"
            f"if \"%NEW_PID%\"==\"\" goto do_rollback\r\n"
            f"set /a wait_count=0\r\n"
            f":monitor_boot\r\n"
            f"timeout /t 1 /nobreak >nul\r\n"
            f"set /a wait_count+=1\r\n"
            f"if exist \"{current_exe.parent}\\.ota_confirmed_%NEW_PID%\" (\r\n"
            f"    del /f /q \"{current_exe.parent}\\.ota_confirmed_%NEW_PID%\" 2>nul\r\n"
            f"    del /f /q \"{old_backup}\" 2>nul\r\n"
            f"    del /f /q \"{old_backup_alt}\" 2>nul\r\n"
            f"    goto update_ok\r\n"
            f")\r\n"
            f"if not exist \"{old_backup}\" goto unconfirmed_missing_backup\r\n"
            f"tasklist /fi \"PID eq %NEW_PID%\" /fo csv 2>nul | findstr /i \"\\\"%NEW_PID%\\\"\" >nul\r\n"
            f"if errorlevel 1 goto do_rollback\r\n"
            f"if %wait_count% geq 25 goto do_rollback\r\n"
            f"goto monitor_boot\r\n"
            f":unconfirmed_missing_backup\r\n"
            f"echo [CRITICAL ERROR] Watchdog: File di backup {old_backup} assente senza marker di conferma valido (.ota_confirmed_%NEW_PID%). Aggiornamento NON confermato. > \"{diag_log}\"\r\n"
            f"echo Timestamp: %DATE% %TIME% >> \"{diag_log}\"\r\n"
            f"echo Unconfirmed Process PID: %NEW_PID% >> \"{diag_log}\"\r\n"
            f"exit /b 1\r\n"
            f":staging_missing\r\n"
            f"echo [CRITICAL ERROR] OTA Swap: File di staging {staged_target} non trovato. Aggiornamento annullato. > \"{diag_log}\"\r\n"
            f"echo Timestamp: %DATE% %TIME% >> \"{diag_log}\"\r\n"
            f"if exist \"{current_exe}\" (\r\n"
            f"    echo [RECOVERY] Riavvio dell'eseguibile attivo intatto: {current_exe} >> \"{diag_log}\"\r\n"
            f"    start \"\" \"{current_exe}\" {cmd_args_str}\r\n"
            f")\r\n"
            f"exit /b 1\r\n"
            f":swap_failed_backup\r\n"
            f"echo [CRITICAL ERROR] OTA Swap: Impossibile creare backup {old_backup} da {current_exe} > \"{diag_log}\"\r\n"
            f"echo Timestamp: %DATE% %TIME% >> \"{diag_log}\"\r\n"
            f"del /f /q \"{staged_target}\" 2>nul\r\n"
            f"if exist \"{current_exe}\" (\r\n"
            f"    echo [RECOVERY] Riavvio dell'eseguibile attivo intatto: {current_exe} >> \"{diag_log}\"\r\n"
            f"    start \"\" \"{current_exe}\" {cmd_args_str}\r\n"
            f"    exit /b 1\r\n"
            f")\r\n"
            f"if exist \"{old_backup}\" (\r\n"
            f"    echo [RECOVERY] Tentativo di ripristino ed avvio da {old_backup} >> \"{diag_log}\"\r\n"
            f"    move /y \"{old_backup}\" \"{current_exe}\" >nul 2>nul\r\n"
            f"    if exist \"{current_exe}\" (\r\n"
            f"        start \"\" \"{current_exe}\" {cmd_args_str}\r\n"
            f"    ) else (\r\n"
            f"        start \"\" \"{old_backup}\" {cmd_args_str}\r\n"
            f"    )\r\n"
            f"    exit /b 1\r\n"
            f")\r\n"
            f"echo [CRITICAL ERROR] OTA Swap: Nessun eseguibile integro disponibile ({current_exe} o {old_backup}) per il riavvio >> \"{diag_log}\"\r\n"
            f"exit /b 1\r\n"
            f":swap_failed_install\r\n"
            f"echo [WARNING] OTA Swap: Impossibile installare {staged_target} su {current_exe}. Avvio rollback... > \"{diag_log}\"\r\n"
            f"echo Timestamp: %DATE% %TIME% >> \"{diag_log}\"\r\n"
            f"del /f /q \"{staged_target}\" 2>nul\r\n"
            f"goto do_rollback\r\n"
            f":do_rollback\r\n"
            f"if not \"%NEW_PID%\"==\"\" (\r\n"
            f"    taskkill /f /pid %NEW_PID% >nul 2>nul\r\n"
            f")\r\n"
            f"timeout /t 1 /nobreak >nul\r\n"
            f"if exist \"{current_exe}\" (\r\n"
            f"    del /f /q \"{current_exe}\" 2>nul\r\n"
            f")\r\n"
            f"del /f /q \"{staged_target}\" 2>nul\r\n"
            f"if not exist \"{old_backup}\" goto rollback_missing_backup\r\n"
            f"move /y \"{old_backup}\" \"{current_exe}\" >nul 2>nul\r\n"
            f"if errorlevel 1 goto rollback_failed\r\n"
            f"if not exist \"{current_exe}\" goto rollback_failed\r\n"
            f"if exist \"{current_exe}\" (\r\n"
            f"    start \"\" \"{current_exe}\" {cmd_args_str}\r\n"
            f")\r\n"
            f"goto cleanup\r\n"
            f":rollback_failed\r\n"
            f"echo [CRITICAL ERROR] OTA Rollback: Impossibile ripristinare {old_backup} su {current_exe} > \"{diag_log}\"\r\n"
            f"echo Timestamp: %DATE% %TIME% >> \"{diag_log}\"\r\n"
            f"echo Failed Process PID: %NEW_PID% >> \"{diag_log}\"\r\n"
            f"echo Backup valido preservato in: {old_backup} >> \"{diag_log}\"\r\n"
            f"echo Azione manuale richiesta: rinominare \"{old_backup}\" in \"{current_exe}\" >> \"{diag_log}\"\r\n"
            f"exit /b 1\r\n"
            f":rollback_missing_backup\r\n"
            f"echo [CRITICAL ERROR] OTA Rollback: File di backup {old_backup} non trovato >> \"{diag_log}\"\r\n"
            f"echo Timestamp: %DATE% %TIME% >> \"{diag_log}\"\r\n"
            f"echo Failed Process PID: %NEW_PID% >> \"{diag_log}\"\r\n"
            f"exit /b 1\r\n"
            f":update_ok\r\n"
            f"del /f /q \"{old_backup}\" 2>nul\r\n"
            f"del /f /q \"{old_backup_alt}\" 2>nul\r\n"
            f"del /f /q \"{staged_target}\" 2>nul\r\n"
            f"if not \"%NEW_PID%\"==\"\" (\r\n"
            f"    del /f /q \"{current_exe.parent}\\.ota_confirmed_%NEW_PID%\" 2>nul\r\n"
            f")\r\n"
            f"del /f /q \"{current_exe.parent}\\.ota_confirmed_*\" 2>nul\r\n"
            f"del /f /q \"{current_exe.parent}\\.ota_ready_*\" 2>nul\r\n"
            f":cleanup\r\n"
            f"(goto) 2>nul & del \"%~f0\"\r\n"
        )

        # Clean preexisting ready tag if present
        if ready_marker.exists():
            try:
                ready_marker.unlink()
            except Exception:
                pass

        try:
            restart_bat.write_text(bat_content, encoding="utf-8")
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE (No console window)
            clean_env = os.environ.copy()
            for var in ("_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL", "_PYI_SPLASH_IPC", "_MEIPASS", "_MEIPASS2"):
                clean_env.pop(var, None)
            clean_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            watchdog_proc = subprocess.Popen(
                ["cmd.exe", "/c", str(restart_bat)],
                startupinfo=startupinfo,
                creationflags=0x08000000 | 0x00000200,  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
                close_fds=True,
                env=clean_env
            )
        except Exception as ex_spawn:
            logger.critical(
                f"Impossibile avviare il supervisore di riavvio: {ex_spawn}. Interruzione fail-closed."
            )
            try:
                if restart_bat.exists():
                    restart_bat.unlink()
            except Exception:
                pass
            if staged_target.exists():
                try:
                    staged_target.unlink()
                except Exception:
                    pass

            _safe_cleanup_temp()
            return False, f"Impossibile avviare il supervisore sicuro ({ex_spawn}). Eseguibile attivo rimasto intatto."

        # 3. Watchdog Readiness Handshake with bounded wait
        ready = False
        start_wait = time.monotonic()
        while time.monotonic() - start_wait < readiness_timeout:
            if ready_marker.exists():
                ready = True
                break
            time.sleep(0.05)

        if not ready:
            logger.critical(
                f"Handshake di readiness con il supervisore watchdog non riuscito "
                f"entro {readiness_timeout:.1f}s. Annullamento fail-closed."
            )
            terminated_ok = ensure_watchdog_terminated(watchdog_proc, term_timeout=2.0, kill_timeout=1.0)
            if not terminated_ok:
                logger.critical(
                    "STATO CRITICO: Impossibile confermare la terminazione del supervisore watchdog. "
                    "Gli artefatti (ready_marker, restart_bat, staged_target) NON vengono rimossi "
                    "per evitare corruzioni se il supervisore e' ancora in esecuzione."
                )
                _safe_cleanup_temp()
                return False, (
                    "ERRORE CRITICO: Il supervisore watchdog non ha confermato la readiness e non e' stato "
                    "possibile confermarne la terminazione; il processo supervisore potrebbe essere ancora attivo. "
                    "Gli artefatti di staging sono stati preservati per sicurezza. Eseguibile attivo non modificato."
                )

            # Terminazione confermata: pulisci soltanto gli artefatti appartenenti al tentativo corrente
            try:
                if ready_marker is not None and ready_marker.exists():
                    ready_marker.unlink()
            except Exception:
                pass
            try:
                if restart_bat is not None and restart_bat.exists():
                    restart_bat.unlink()
            except Exception:
                pass
            if staged_target is not None and staged_target.exists():
                try:
                    staged_target.unlink()
                except Exception:
                    pass

            _safe_cleanup_temp()
            return False, (
                f"Il supervisore watchdog non ha confermato la readiness entro "
                f"{readiness_timeout:.1f}s. Supervisore terminato ed eseguibile attivo rimasto intatto."
            )

        _safe_cleanup_temp()
        return True, "Aggiornamento validato in staging locale e supervisore pronto. Riavvio in corso..."
    except Exception as e:
        if watchdog_proc is not None:
            terminated_ok = ensure_watchdog_terminated(watchdog_proc, term_timeout=2.0, kill_timeout=1.0)
            if not terminated_ok:
                logger.critical(
                    f"STATO CRITICO: Eccezione durante l'aggiornamento ({e}) e impossibilita' di confermare "
                    f"la terminazione del supervisore watchdog. Gli artefatti NON vengono rimossi."
                )
                _safe_cleanup_temp()
                return False, (
                    f"ERRORE CRITICO: Eccezione durante l'aggiornamento ({e}) e mancata conferma della terminazione "
                    f"del supervisore watchdog; il processo supervisore potrebbe essere ancora attivo. "
                    f"Gli artefatti sono stati preservati per sicurezza. Eseguibile attivo non modificato."
                )

        # Terminazione confermata (o watchdog mai avviato): rimuovi esclusivamente gli artefatti di questo tentativo
        if ready_marker is not None:
            try:
                if ready_marker.exists():
                    ready_marker.unlink()
            except Exception:
                pass
        if restart_bat is not None:
            try:
                if restart_bat.exists():
                    restart_bat.unlink()
            except Exception:
                pass
        if staged_target is not None and staged_target.exists():
            try:
                staged_target.unlink()
            except Exception:
                pass

        _safe_cleanup_temp()
        logger.error(f"In-place update error: {e}")
        return False, f"Errore durante l'aggiornamento: {e}"

def check_github_update(
    repo: str = GITHUB_REPO,
    local_version: str = CURRENT_VERSION,
    public_key_hex: Optional[str] = None
) -> Tuple[bool, dict]:
    """
    Check GitHub Releases API for new AetherDesk versions.
    Fail-closed: requires valid cryptographically signed manifest; rejects unauthenticated releases.
    Returns (has_newer_version, info_dict).
    """
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "AetherDesk-OTA-Client",
            "Accept": "application/vnd.github.v3+json"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tag = data.get("tag_name", "").lstrip("v")
            assets = data.get("assets", [])
            manifest_url = None
            for asset in assets:
                name = asset.get("name", "")
                name_lower = name.lower()
                if name_lower in ("manifest.json", "update_manifest.json", "aether_manifest.json") or (name_lower.endswith(".json") and "manifest" in name_lower):
                    manifest_url = asset.get("browser_download_url")
                    break

            # 1. Fetch and verify external manifest asset if present
            verified_manifest = None
            manifest_err = None
            if manifest_url:
                try:
                    m_req = urllib.request.Request(manifest_url, headers={"User-Agent": "AetherDesk-OTA-Client"})
                    with urllib.request.urlopen(m_req, timeout=6) as m_resp:
                        raw_m = json.loads(m_resp.read().decode("utf-8"))
                        m_ok, m_msg = verify_update_manifest(raw_m, local_version=local_version, public_key_hex=public_key_hex)
                        if m_ok:
                            verified_manifest = raw_m
                        else:
                            manifest_err = f"Manifest GitHub non valido: {m_msg}"
                except Exception as m_ex:
                    manifest_err = f"Errore caricamento manifest GitHub: {m_ex}"

            # 2. Fallback check: look for signed manifest block in release body
            if not verified_manifest and not manifest_err:
                body_text = data.get("body", "")
                if "AETHER_MANIFEST:" in body_text:
                    try:
                        part = body_text.split("AETHER_MANIFEST:")[1].split("---")[0].strip()
                        raw_m = json.loads(part)
                        m_ok, m_msg = verify_update_manifest(raw_m, local_version=local_version, public_key_hex=public_key_hex)
                        if m_ok:
                            verified_manifest = raw_m
                        else:
                            manifest_err = f"Manifest nel body non valido: {m_msg}"
                    except Exception as b_ex:
                        manifest_err = f"Errore parsing manifest nel body: {b_ex}"

            # Fail-closed policy: If no verified signed manifest, reject release
            if not verified_manifest:
                err_reason = manifest_err or "Release GitHub priva di manifest crittografico firmato dal fornitore (rifiuto fail-closed)."
                return False, {
                    "tag": tag,
                    "name": data.get("name", tag),
                    "download_url": None,
                    "size": 0,
                    "is_newer": False,
                    "error": err_reason,
                    "body": data.get("body", "")
                }

            # Bind executable asset strictly to the filename authenticated in the manifest
            bound_filename = Path(str(verified_manifest.get("filename", ""))).name.lower().strip()
            if not bound_filename:
                return False, {
                    "tag": tag,
                    "name": data.get("name", tag),
                    "download_url": None,
                    "size": 0,
                    "is_newer": False,
                    "error": "Manifest privo di nome asset valido (filename).",
                    "body": data.get("body", "")
                }

            matching_assets = [
                a for a in assets
                if Path(str(a.get("name", ""))).name.lower().strip() == bound_filename
            ]

            if not matching_assets:
                return False, {
                    "tag": tag,
                    "name": data.get("name", tag),
                    "download_url": None,
                    "size": 0,
                    "is_newer": False,
                    "error": f"Asset vincolato nel manifest ('{bound_filename}') non trovato tra gli asset della release GitHub.",
                    "body": data.get("body", "")
                }

            if len(matching_assets) > 1:
                return False, {
                    "tag": tag,
                    "name": data.get("name", tag),
                    "download_url": None,
                    "size": 0,
                    "is_newer": False,
                    "error": f"Rilevati molteplici asset ambigui per '{bound_filename}' nella release GitHub.",
                    "body": data.get("body", "")
                }

            selected_asset = matching_assets[0]
            download_url = selected_asset.get("browser_download_url")
            asset_size = selected_asset.get("size", 0)

            # Cryptographically bound release metadata
            man_ver = verified_manifest.get("version", tag)
            is_newer = is_newer_version(man_ver, local_version) and (download_url is not None)
            info = {
                "tag": man_ver,
                "name": data.get("name", man_ver),
                "download_url": download_url,
                "size": verified_manifest.get("size", asset_size),
                "is_newer": is_newer,
                "body": data.get("body", ""),
                "manifest": verified_manifest,
                "expected_sha256": verified_manifest.get("sha256")
            }
            return is_newer, info
    except Exception as e:
        logger.warning(f"GitHub update check failed: {e}")
        return False, {"error": str(e)}

def download_github_binary(
    download_url: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    expected_sha256: Optional[str] = None,
    signed_manifest: Optional[dict] = None,
    allow_unverified_dev_hash: bool = False
) -> Tuple[bool, str]:
    """
    Stream download new executable from GitHub with progress callback and cryptographic validation.
    Fail-closed:
    - In frozen production binaries, signed_manifest is strictly mandatory.
      An isolated expected_sha256 does NOT authenticate origin and is strictly rejected.
    - Dev mode bypass is isolated and rejected if attempted from a frozen production binary.
    - Binds the requested download URL asset to the filename specified in the signed manifest.
    """
    is_frozen = getattr(sys, "frozen", False)
    if is_frozen:
        if allow_unverified_dev_hash:
            return False, "Rifiuto download fail-closed: il bypass dev non puo' essere utilizzato in un binario frozen di produzione."
        if not signed_manifest:
            return False, "Rifiuto download fail-closed: in produzione e' richiesto obbligatoriamente un manifest firmato verificabile dal fornitore (un expected_sha256 isolato non autentica la provenienza)."
    else:
        if not signed_manifest and not (allow_unverified_dev_hash and expected_sha256):
            return False, "Rifiuto download fail-closed: impossibile procedere senza manifest firmato verificabile dal fornitore."

    if signed_manifest:
        man_ok, man_msg = verify_update_manifest(signed_manifest)
        if not man_ok:
            return False, f"Manifest firmato non valido: {man_msg}"
        expected_sha256 = signed_manifest.get("sha256")

        # Verify that the URL filename matches the asset filename bound in the signed manifest
        manifest_asset_name = Path(signed_manifest.get("filename", "")).name.lower().strip()
        url_path = urllib.parse.urlparse(download_url).path
        url_asset_name = Path(url_path).name.lower().strip()
        if manifest_asset_name and url_asset_name and url_asset_name != manifest_asset_name:
            return False, (
                f"Rifiuto download fail-closed: l'asset nell'URL ({url_asset_name}) "
                f"non corrisponde all'asset vincolato nel manifest firmato ({manifest_asset_name})."
            )

    temp_path: Optional[str] = None
    success: bool = False
    try:
        req = urllib.request.Request(
            download_url,
            headers={"User-Agent": "AetherDesk-OTA-Client"}
        )
        staging_dir = get_ota_staging_dir()
        tf = tempfile.NamedTemporaryFile(
            delete=False,
            dir=str(staging_dir),
            prefix=OTA_TEMP_FILE_PREFIX,
            suffix=".exe"
        )
        temp_path = tf.name
        tf.close()

        with urllib.request.urlopen(req, timeout=45) as resp, open(temp_path, "wb") as f:
            total_size = int(resp.headers.get("Content-Length", 0))
            received = 0
            while chunk := resp.read(65536):
                f.write(chunk)
                received += len(chunk)
                if progress_callback:
                    progress_callback(received, total_size)

        valid, msg = validate_executable_file(temp_path, expected_sha256=expected_sha256, signed_manifest=signed_manifest)
        if not valid:
            return False, f"File scaricato non valido: {msg}"

        success = True
        return True, temp_path
    except Exception as e:
        return False, f"Errore durante il download da GitHub: {e}"
    finally:
        if not success and temp_path:
            try:
                p = Path(temp_path)
                if p.is_file():
                    p.unlink()
            except Exception as clean_err:
                logger.warning(f"Impossibile eliminare il file temporaneo residuo {temp_path}: {clean_err}")
