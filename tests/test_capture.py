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
