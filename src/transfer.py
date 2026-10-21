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

    def get_text(self) -> str:
        text = ""
        for _ in range(5):
            try:
                win32clipboard.OpenClipboard()
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    text = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                win32clipboard.CloseClipboard()
                break
            except Exception:
                time.sleep(0.01)
        return text if text else ""

    def set_text(self, text: str, update_last: bool = True) -> bool:
        if not text:
            return False
        for _ in range(5):
            try:
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
                win32clipboard.CloseClipboard()
                if update_last:
                    self.last_text = text
                return True
            except Exception:
                time.sleep(0.01)
        return False

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
            self.save_dir = Path(save_dir)
        else:
            downloads = Path(os.environ.get("USERPROFILE", "C:\\")) / "Downloads" / "RemoteDesktop_Transfers"
            self.save_dir = downloads

        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.incoming_transfers: Dict[str, Dict] = {} # file_id -> transfer state

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

        chunks = []
        with open(p, "rb") as f:
            offset = 0
            while True:
                data = f.read(chunk_size)
                if not data:
                    break
                chunks.append((offset, data))
                offset += len(data)

        return (meta, chunks)

    def start_receiving(self, file_id: str, filename: str, filesize: int) -> Path:
        target_path = self.save_dir / filename
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
