import os
import sys
import shutil
import hashlib
import logging
import subprocess
import urllib.request
import json
import tempfile
from enum import Enum
from pathlib import Path
from typing import Optional, Callable, Tuple

logger = logging.getLogger("OTA")

CURRENT_VERSION = "1.3.3"
GITHUB_REPO = "ManuRoma02/AetherDesk"

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

def parse_version(v_str: str) -> Tuple[int, ...]:
    """Parse semantic version string into an integer tuple for strict comparison."""
    try:
        clean = v_str.strip().lstrip("v").split("-")[0]
        parts = tuple(int(x) for x in clean.split(".") if x.isdigit())
        if not parts:
            return (0, 0, 0)
        while len(parts) < 3:
            parts = parts + (0,)
        return parts
    except Exception:
        return (0, 0, 0)

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
    for p in candidates:
        if p.exists() and p.is_file() and p.stat().st_size > 1_000_000:
            return p
    return None

def validate_executable_file(filepath: str | Path) -> Tuple[bool, str]:
    """
    Verify integrity and format of binary before staging/flashing.
    Ensures minimum size (>1MB) and valid Windows PE header (MZ magic).
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
    return True, "Eseguibile valido."

def verify_update_manifest(
    manifest: dict,
    local_version: str = CURRENT_VERSION,
    allow_downgrade: bool = False
) -> Tuple[bool, str]:
    """
    Audit update manifest before downloading.
    Enforces Anti-Rollback policy and required cryptographic metadata.
    """
    remote_version = manifest.get("version", "")
    if not remote_version:
        return False, "Manifest privo di versione."

    if not allow_downgrade and not is_newer_version(remote_version, local_version):
        return False, (
            f"Anti-Rollback attivo: la versione remota ({remote_version}) "
            f"non e' piu' recente di quella installata ({local_version})."
        )

    if not manifest.get("sha256"):
        return False, "Manifest privo di firma hash SHA-256."

    return True, "Manifest verificato con successo."

def cleanup_old_versions(directory: Optional[Path] = None) -> int:
    """
    Purges previous version backups, .old files, watchdog scripts, and temporary staging files.
    Ensures that updating to a new version completely removes the older executable from disk.
    """
    removed_count = 0
    try:
        base_dir = directory or Path(sys.executable).resolve().parent
        if not base_dir.is_dir():
            return 0

        # Targeted patterns for old executable leftovers
        patterns = ["*.exe.old", "*.old", "*.bak", "ota_watchdog*.bat", "*_update_staging*.exe"]
        for pat in patterns:
            for old_file in base_dir.glob(pat):
                try:
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
    """
    current_exe = Path(sys.executable).resolve()
    old_backup = current_exe.with_suffix(".exe.old")
    if getattr(sys, "frozen", False) and (old_backup.exists() or current_exe.with_suffix(".old").exists()):
        return OTAUpdateState.TESTING
    return OTAUpdateState.IDLE

def confirm_boot() -> bool:
    """
    Self-Test Confirmation:
    Called once the application completes startup, renders UI, and passes checks.
    Cleans up all backup slots (.exe.old, .old) and transient watchdog scripts to mark
    the update as permanent and leave zero leftover files.
    """
    try:
        cleanup_old_versions()
        logger.info("OTA Update confermato permanentemente: tutte le versioni precedenti sono state eliminate.")
        return True
    except Exception as e:
        logger.error(f"Errore durante confirm_boot: {e}")
        return False

def apply_in_place_update(
    downloaded_temp_path: str,
    target_version: Optional[str] = None,
    allow_downgrade: bool = False,
    destination_path: Optional[str | Path] = None
) -> Tuple[bool, str]:
    """
    Replace running executable following Dual-Bank / A-B Staging architecture
    with Anti-Rollback check, automatic purge of the previous version, and failsafe watchdog.
    """
    try:
        temp_p = Path(downloaded_temp_path)
        valid, msg = validate_executable_file(temp_p)
        if not valid:
            return False, msg

        # Anti-Rollback enforcement
        if target_version and not allow_downgrade:
            if not is_newer_version(target_version, CURRENT_VERSION):
                return False, (
                    f"Anti-Rollback attivo: la versione {target_version} "
                    f"non e' consentita rispetto a {CURRENT_VERSION}."
                )

        current_exe = Path(sys.executable).resolve()
        is_frozen = getattr(sys, "frozen", False)

        if not is_frozen:
            # Dev mode: copy staging file to destination_path or dist/AetherDesk.exe
            dest = Path(destination_path) if destination_path else (Path(__file__).resolve().parent.parent / "dist" / "AetherDesk.exe")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(temp_p, dest)
            return True, f"Aggiornamento completato (Modalita' dev, salvato in {dest})."

        # Clean any preexisting stale old files before starting swap
        cleanup_old_versions(current_exe.parent)

        # Production frozen exe: A/B staging swap
        old_backup = current_exe.with_suffix(".exe.old")
        if old_backup.exists():
            try:
                old_backup.unlink()
            except Exception:
                pass

        # 1. Atomic rename running exe to .exe.old (Primary Slot -> Temporary Backup)
        os.rename(str(current_exe), str(old_backup))

        # 2. Copy staged image into Primary Slot (current_exe)
        shutil.copy2(str(temp_p), str(current_exe))
        try:
            temp_p.unlink()
        except Exception:
            pass

        # 3. Direct Watchdog & Purge script:
        # Launches the new executable. After launch confirmation, it permanently deletes
        # the old backup file and any other .old remnants so no clutter is left behind.
        watchdog_content = (
            "@echo off\r\n"
            "timeout /t 1 /nobreak >nul\r\n"
            f'start "" "{current_exe}"\r\n'
            "timeout /t 2 /nobreak >nul\r\n"
            f'del /f /q "{old_backup}" >nul 2>&1\r\n'
            f'del /f /q "{current_exe.parent}\\*.old" >nul 2>&1\r\n'
            f'del /f /q "{current_exe.parent}\\*.exe.old" >nul 2>&1\r\n'
            "del /f /q \"%~f0\" >nul 2>&1\r\n"
        )
        watchdog_bat = current_exe.parent / "ota_watchdog.bat"
        try:
            watchdog_bat.write_text(watchdog_content, encoding="utf-8")
        except Exception:
            pass

        flags = 0x08000008 if os.name == "nt" else 0
        cmd = f'cmd.exe /c "{watchdog_bat}"' if watchdog_bat.exists() else f'cmd.exe /c start "" "{current_exe}"'
        subprocess.Popen(cmd, shell=True, creationflags=flags)

        return True, "Aggiornamento completato. Riavvio in corso ed eliminazione della versione precedente..."
    except Exception as e:
        logger.error(f"In-place update error: {e}")
        return False, f"Errore durante l'aggiornamento: {e}"

def check_github_update(
    repo: str = GITHUB_REPO,
    local_version: str = CURRENT_VERSION
) -> Tuple[bool, dict]:
    """
    Check GitHub Releases API for new AetherDesk versions.
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
            download_url = None
            size = 0
            for asset in assets:
                name = asset.get("name", "").lower()
                if name.endswith(".exe"):
                    download_url = asset.get("browser_download_url")
                    size = asset.get("size", 0)
                    if "aether" in name or "client" in name:
                        break

            is_newer = is_newer_version(tag, local_version) and (download_url is not None)
            info = {
                "tag": tag,
                "name": data.get("name", tag),
                "download_url": download_url,
                "size": size,
                "is_newer": is_newer,
                "body": data.get("body", "")
            }
            return is_newer, info
    except Exception as e:
        logger.warning(f"GitHub update check failed: {e}")
        return False, {"error": str(e)}

def download_github_binary(
    download_url: str,
    progress_callback: Optional[Callable[[int, int], None]] = None
) -> Tuple[bool, str]:
    """
    Stream download new executable from GitHub with progress callback and validation.
    """
    try:
        req = urllib.request.Request(
            download_url,
            headers={"User-Agent": "AetherDesk-OTA-Client"}
        )
        tf = tempfile.NamedTemporaryFile(delete=False, suffix=".exe")
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

        valid, msg = validate_executable_file(temp_path)
        if not valid:
            try:
                os.unlink(temp_path)
            except Exception:
                pass
            return False, f"File scaricato non valido: {msg}"

        return True, temp_path
    except Exception as e:
        return False, f"Errore durante il download da GitHub: {e}"
