import pytest
from src.input_handler import InputHandler

def test_input_handler_coordinates():
    handler = InputHandler()
    abs_x, abs_y = handler.normalize_to_screen(0.5, 0.5)
    assert abs_x > 0 and abs_y > 0

def test_input_handler_mouse_events():
    handler = InputHandler()
    # Test valid calls don't raise
    handler.move_mouse(0.1, 0.1)
    handler.mouse_down("left", norm_x=0.2, norm_y=0.2)
    handler.mouse_up("left", norm_x=0.2, norm_y=0.2)
    handler.mouse_down("right", norm_x=0.3, norm_y=0.3)
    handler.mouse_up("right", norm_x=0.3, norm_y=0.3)
    handler.mouse_wheel(120, norm_x=0.2, norm_y=0.2)

def test_input_handler_keyboard_events():
    handler = InputHandler()
    handler.key_down(0x41) # 'A'
    handler.key_up(0x41)
    handler.send_unicode_char("€")
    handler.send_unicode_char("à")

def test_input_handler_special_keys():
    handler = InputHandler()
    assert handler.send_special("win_d") is True
    assert handler.send_special("alt_tab") is True
    assert handler.send_special("task_mgr") is True
    assert handler.send_special("lock") is True
    assert handler.send_special("unknown_command") is False

def test_input_handler_multimonitor_mapping():
    handler = InputHandler()
    captured_inputs = []
    handler._send_input = lambda inp: captured_inputs.append((inp.union.mi.dx, inp.union.mi.dy))

    # Monitor 2 situated from x=1920 to 3840, total virtual screen 3840x1080
    mon2_bounds = (1920, 0, 1920, 1080)
    virtual_bounds = (0, 0, 3840, 1080)

    # Click in middle of Monitor 2 (norm_x=0.5, norm_y=0.5)
    handler.move_mouse(0.5, 0.5, monitor_bounds=mon2_bounds, virtual_bounds=virtual_bounds)

    assert len(captured_inputs) == 1
    abs_x, abs_y = captured_inputs[0]
    # Center of Monitor 2 in virtual space is at 75% width (0.75 * 65535 = 49151)
    assert abs(abs_x - int(0.75 * 65535)) <= 1
    # Center in height is 50% (0.5 * 65535 = 32767)
    assert abs(abs_y - int(0.5 * 65535)) <= 1
