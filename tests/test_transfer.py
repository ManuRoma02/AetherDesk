import pytest
import os
from pathlib import Path
from src.transfer import ClipboardManager, FileTransferManager

def test_clipboard_manager_get_set():
    mgr = ClipboardManager()
    mgr.set_text("HelloFromTest2026")
    assert mgr.get_text() == "HelloFromTest2026"

def test_clipboard_change_detection():
    mgr = ClipboardManager()
    mgr.set_text("InitialText")
    mgr.last_text = "InitialText"
    assert mgr.has_changed() is False
    mgr.set_text("ChangedText", update_last=False)
    assert mgr.has_changed() is True

def test_file_transfer_chunking(tmp_path):
    ft = FileTransferManager(save_dir=str(tmp_path))
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("Remote Desktop File Content Test 1234567890", encoding="utf-8")

    meta, chunks = ft.prepare_file_for_sending(str(sample_file), chunk_size=10)
    assert meta["filename"] == "sample.txt"
    assert meta["filesize"] > 0
    assert len(chunks) > 1

    # Receive and reconstruct
    file_id = meta["file_id"]
    ft.start_receiving(file_id, "reconstructed.txt", meta["filesize"])
    for offset, chunk_data in chunks:
        ft.receive_chunk(file_id, offset, chunk_data)
    out_path = ft.finish_receiving(file_id)

    assert Path(out_path).exists()
    assert Path(out_path).read_text(encoding="utf-8") == "Remote Desktop File Content Test 1234567890"
