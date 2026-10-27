import pytest
import os
import platform
from pathlib import Path
from src.client import ClientSettings, ClientState, RemoteClient
from src.host_daemon import HostDaemon
from src.security import MockTestDPAPIEngine

def _test_dpapi():
    return MockTestDPAPIEngine() if platform.system() != "Windows" else None

def test_client_settings_save_and_load(tmp_path):
    settings = ClientSettings(config_dir=str(tmp_path))
    assert settings.load() == ("", "", False)

    settings.save("482 910 234", "MyPassword123", remember=True)
    dev_id, pwd, remember = settings.load()
    assert dev_id == "482 910 234"
    assert pwd == "MyPassword123"
    assert remember is True

    # Test forgetting
    settings.save("", "", remember=False)
    dev_id, pwd, remember = settings.load()
    assert dev_id == ""
    assert pwd == ""
    assert remember is False

def test_client_state_reconnection():
    state = ClientState()
    assert state.is_reconnecting is False
    state.set_reconnecting(True)
    assert state.is_reconnecting is True
    state.set_reconnecting(False)
    assert state.is_reconnecting is False

def test_client_settings_address_book_and_broker(tmp_path):
    settings = ClientSettings(config_dir=str(tmp_path))
    assert settings.get_broker_url() == "ws://127.0.0.1:8765"
    assert settings.get_devices() == []

    settings.save("111 222 333", "pass1", remember=True, broker_url="ws://192.168.1.50:8765")
    assert settings.get_broker_url() == "ws://192.168.1.50:8765"
    devices = settings.get_devices()
    assert len(devices) == 1
    assert devices[0]["id"] == "111 222 333"
    assert devices[0]["broker_url"] == "ws://192.168.1.50:8765"

    settings.save_device("999 888 777", "Fisso Studio", "passStudio", "ws://studio.local:8765")
    devices = settings.get_devices()
    assert len(devices) == 2

    settings.delete_device("111 222 333")
    devices = settings.get_devices()
    assert len(devices) == 1
    assert devices[0]["id"] == "999 888 777"

def test_desktop_capture_change_detection():
    from src.capture import MockDesktopCapture
    cap = MockDesktopCapture(quality=70)
    # First frame always captured
    frame1, dims1, cur1 = cap.capture_frame(only_if_changed=True)
    assert frame1 is not None

    # Immediate second frame without screen change returns None (saving bandwidth/CPU)
    frame2, dims2, cur2 = cap.capture_frame(only_if_changed=True)
    # Could be None if unchanged, or bytes if screen naturally changed
    if frame2 is None:
        assert dims2 == dims1

    # Forced keyframe (only_if_changed=False) always returns bytes
    frame3, dims3, cur3 = cap.capture_frame(only_if_changed=False)
    assert isinstance(frame3, bytes)
    assert len(frame3) > 100
    cap.close()

def test_file_transfer_prepare_file_info(tmp_path):
    from src.transfer import FileTransferManager
    ft = FileTransferManager(save_dir=str(tmp_path))
    test_f = tmp_path / "big_sample.bin"
    test_f.write_bytes(b"A" * 1024)

    info = ft.prepare_file_info(str(test_f))
    assert info["filename"] == "big_sample.bin"
    assert info["filesize"] == 1024
    assert len(info["file_id"]) == 16

def test_host_daemon_frame_ack_backpressure(tmp_path):
    daemon = HostDaemon(config_dir=str(tmp_path), headless=True, dpapi_engine=_test_dpapi(), start_worker=False)
    try:
        daemon._in_flight_frame = True
        daemon._frame_ack_event.clear()

        # Client sends back FRAME_ACK on authenticated session
        daemon.handle_client_message("FRAME_ACK", {"fid": 42}, None, {"authenticated": True})
        assert daemon._in_flight_frame is False
        assert daemon._frame_ack_event.is_set() is True
    finally:
        daemon.stop()

def test_desktop_capture_cached_bounds():
    from src.capture import MockDesktopCapture
    cap = MockDesktopCapture()
    b1 = cap.get_active_monitor_bounds()
    b2 = cap.get_active_monitor_bounds()
    assert b1 == b2
    v1 = cap.get_virtual_desktop_bounds()
    v2 = cap.get_virtual_desktop_bounds()
    assert v1 == v2
    cap.close()
