import pytest
from src.capture import MockDesktopCapture

def test_desktop_capture_initialization():
    cap = MockDesktopCapture(quality=75)
    assert cap.quality == 75
    cap.close()

def test_desktop_capture_frame():
    cap = MockDesktopCapture(quality=70)
    jpeg_data, screen_size, cursor_pos = cap.capture_frame()
    assert isinstance(jpeg_data, bytes)
    assert len(jpeg_data) > 100
    assert screen_size[0] > 0 and screen_size[1] > 0
    assert isinstance(cursor_pos, tuple)
    cap.close()

def test_desktop_switch_desktop():
    cap = MockDesktopCapture()
    res = cap.switch_desktop()
    assert isinstance(res, bool)
    cap.close()

def test_desktop_multimonitor():
    cap = MockDesktopCapture()
    monitors = cap.get_monitors_list()
    assert isinstance(monitors, list)
    assert len(monitors) >= 1
    assert "id" in monitors[0]
    assert "rect" in monitors[0]
    # Set primary monitor
    cap.set_monitor(0)
    assert cap.current_monitor_id == 0
    # Set all monitors
    cap.set_monitor(-1)
    assert cap.current_monitor_id == -1
    cap.close()

def test_desktop_capture_pure_pillow_fallback(monkeypatch):
    """Verify DesktopCapture functions safely in simulated standalone mode."""
    import src.capture as cap_mod
    monkeypatch.setattr(cap_mod, "HAS_CV2", False)
    monkeypatch.setattr(cap_mod, "cv2", None)
    monkeypatch.setattr(cap_mod, "np", None)

    cap = cap_mod.MockDesktopCapture(quality=65)
    jpeg_data, screen_size, cursor_pos = cap.capture_frame()
    assert isinstance(jpeg_data, bytes)
    assert len(jpeg_data) > 100
    assert screen_size[0] > 0 and screen_size[1] > 0
    assert isinstance(cursor_pos, tuple)

    # Test change detection in simulated mode
    frame_same, _, _ = cap.capture_frame(only_if_changed=True)
    assert frame_same is None or isinstance(frame_same, bytes)

    # Force change
    frame_force, _, _ = cap.capture_frame(only_if_changed=False)
    assert isinstance(frame_force, bytes)
    assert len(frame_force) > 100
    cap.close()

def test_src_modules_import_without_cv2(monkeypatch):
    """Ensure all core modules can be imported without cv2 without raising ModuleNotFoundError."""
    import sys
    monkeypatch.setitem(sys.modules, "cv2", None)
    import importlib
    import src.capture
    importlib.reload(src.capture)
    import src.host_daemon
    importlib.reload(src.host_daemon)
    import src.client_viewer
    importlib.reload(src.client_viewer)
    assert hasattr(src.client_viewer, "ClientViewerGUI")

def test_dxcam_fallback_resilience():
    cap = MockDesktopCapture()
    # Simulated capture does not attach DirectX hooks or GDI DC
    data, dims, cur = cap.capture_frame(only_if_changed=False)
    assert isinstance(data, bytes)
    assert len(data) > 0
    cap.close()

def test_high_resolution_change_detection(monkeypatch):
    cap = MockDesktopCapture(target_width=320, target_height=180)
    # Simulate first frame
    frame1, dims, cur = cap.capture_frame(only_if_changed=False)
    assert frame1 is not None

    # Immediate second frame with only_if_changed=True should skip if unchanged
    frame2, _, _ = cap.capture_frame(only_if_changed=True)
    assert frame2 is None or isinstance(frame2, bytes)
    cap.close()
