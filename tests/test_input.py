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
    handler.mouse_down("left")
    handler.mouse_up("left")
    handler.mouse_wheel(120)

def test_input_handler_keyboard_events():
    handler = InputHandler()
    handler.key_down(0x41) # 'A'
    handler.key_up(0x41)

def test_input_handler_special_keys():
    handler = InputHandler()
    res = handler.send_special("win_d")
    assert res is True
    res_bad = handler.send_special("unknown_command")
    assert res_bad is False
