import os
import time
import secrets
from pathlib import Path
from typing import Optional, Tuple, List, Dict
import win32clipboard

class ClipboardManager:
    """Manages Windows clipboard reading, writing and change detection."""

    def __init__(self):
        self.last_text = ""
        self._fallback_text = ""

    def get_text(self) -> str:
        text = ""
        opened = False
        for _ in range(10):
            try:
                win32clipboard.OpenClipboard()
                opened = True
                try:
                    if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                        text = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                finally:
                    win32clipboard.CloseClipboard()
                    opened = False
                break
            except Exception:
                if opened:
                    try:
                        win32clipboard.CloseClipboard()
                    except Exception:
                        pass
                    opened = False
                time.sleep(0.02)
        if text:
            self._fallback_text = text
            return text
        return self._fallback_text

    def set_text(self, text: str, update_last: bool = True) -> bool:
        if not text:
            return False
        self._fallback_text = text
        opened = False
        success = False
        for _ in range(10):
            try:
                win32clipboard.OpenClipboard()
                opened = True
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
                    success = True
                finally:
                    win32clipboard.CloseClipboard()
                    opened = False
                break
            except Exception:
                if opened:
                    try:
                        win32clipboard.CloseClipboard()
                    except Exception:
                        pass
                    opened = False
                time.sleep(0.02)
        if update_last:
            self.last_text = text
        return True

    def has_changed(self) -> bool:
        current = self.get_text()
        if current and current != self.last_text:
            self.last_text = current
            return True
        return False

class FileTransferManager:
    """Handles chunked file transmission and reconstruction for remote desktop."""

    def __init__(self, save_dir: Optional[str] = None):
        if save_dir:
            self.save_dir = Path(save_dir).resolve()
        else:
            downloads = Path(os.environ.get("USERPROFILE", "C:\\")) / "Downloads"
            legacy_dir = downloads / "RemoteDesktop_Transfers"
            primary_dir = downloads / "AetherDesk_Transfers"
            self.save_dir = (legacy_dir if legacy_dir.exists() and not primary_dir.exists() else primary_dir).resolve()

        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.incoming_transfers: Dict[str, Dict] = {} # file_id -> transfer state

    def iter_file_chunks(self, file_path: str, chunk_size: int = 65536):
        """Yield chunks one-by-one to avoid loading entire files into memory."""
        p = Path(file_path)
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")
        with open(p, "rb") as f:
            offset = 0
            while True:
                data = f.read(chunk_size)
                if not data:
                    break
                yield (offset, data)
                offset += len(data)

    def prepare_file_info(self, file_path: str) -> dict:
        """Extract metadata for streaming transfer without memory buffering."""
        p = Path(file_path)
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")
        return {
            "file_id": secrets.token_hex(8),
            "filename": p.name,
            "filesize": p.stat().st_size
        }

    def prepare_file_for_sending(self, file_path: str, chunk_size: int = 65536) -> Tuple[dict, List[Tuple[int, bytes]]]:
        p = Path(file_path)
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        filesize = p.stat().st_size
        file_id = secrets.token_hex(8)
        meta = {
            "file_id": file_id,
            "filename": p.name,
            "filesize": filesize
        }
        chunks = list(self.iter_file_chunks(file_path, chunk_size))
        return (meta, chunks)

    def start_receiving(self, file_id: str, filename: str, filesize: int) -> Path:
        # Sanitize filename against path traversal attacks (strip directory components)
        clean_name = Path(filename).name
        if not clean_name or clean_name in (".", ".."):
            clean_name = f"transfer_{secrets.token_hex(4)}.dat"

        target_path = (self.save_dir / clean_name).resolve()
        # Verify target is strictly within save_dir
        if not str(target_path).startswith(str(self.save_dir)):
            target_path = self.save_dir / clean_name

        # Avoid overwriting existing files
        counter = 1
        base_name = target_path.stem
        extension = target_path.suffix
        while target_path.exists():
            target_path = self.save_dir / f"{base_name}_{counter}{extension}"
            counter += 1

        self.incoming_transfers[file_id] = {
            "path": target_path,
            "filesize": filesize,
            "received_bytes": 0,
            "file_handle": open(target_path, "wb")
        }
        return target_path

    def receive_chunk(self, file_id: str, offset: int, chunk_data: bytes):
        if file_id not in self.incoming_transfers:
            raise ValueError(f"Unknown transfer ID: {file_id}")

        info = self.incoming_transfers[file_id]
        fh = info["file_handle"]
        fh.seek(offset)
        fh.write(chunk_data)
        info["received_bytes"] += len(chunk_data)

    def finish_receiving(self, file_id: str) -> str:
        if file_id not in self.incoming_transfers:
            raise ValueError(f"Unknown transfer ID: {file_id}")

        info = self.incoming_transfers[file_id]
        info["file_handle"].close()
        saved_path = str(info["path"])
        del self.incoming_transfers[file_id]
        return saved_path

    def cancel_transfer(self, file_id: str):
        """Close handle and clean up aborted transfer."""
        if file_id in self.incoming_transfers:
            try:
                self.incoming_transfers[file_id]["file_handle"].close()
            except Exception:
                pass
            del self.incoming_transfers[file_id]

    def cancel_all_transfers(self):
        """Clean up and close all pending or aborted transfers."""
        for file_id in list(self.incoming_transfers.keys()):
            self.cancel_transfer(file_id)
