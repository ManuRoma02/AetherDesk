import pytest
from src.capture import DesktopCapture

def test_desktop_capture_initialization():
    cap = DesktopCapture(quality=75)
    assert cap.quality == 75
    cap.close()

def test_desktop_capture_frame():
    cap = DesktopCapture(quality=70)
    jpeg_data, screen_size, cursor_pos = cap.capture_frame()
    assert isinstance(jpeg_data, bytes)
    assert len(jpeg_data) > 100
    assert screen_size[0] > 0 and screen_size[1] > 0
    assert isinstance(cursor_pos, tuple)
    cap.close()

def test_desktop_switch_desktop():
    cap = DesktopCapture()
    res = cap.switch_desktop()
    assert isinstance(res, bool)
    cap.close()

def test_desktop_multimonitor():
    cap = DesktopCapture()
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
