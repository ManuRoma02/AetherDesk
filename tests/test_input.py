import pytest
from src.input_handler import InputHandler, MockInputInjector

def test_input_handler_coordinates():
    mock_inj = MockInputInjector(screen_w=1920, screen_h=1080)
    handler = InputHandler(injector=mock_inj)
    abs_x, abs_y = handler.normalize_to_screen(0.5, 0.5)
    assert abs_x > 0 and abs_y > 0

def test_input_handler_mouse_events():
    mock_inj = MockInputInjector(screen_w=1920, screen_h=1080)
    handler = InputHandler(injector=mock_inj)
    # Test valid calls don't raise and are recorded safely in mock
    handler.move_mouse(0.1, 0.1)
    handler.mouse_down("left", norm_x=0.2, norm_y=0.2)
    assert "left" in handler._pressed_mouse_buttons
    handler.mouse_up("left", norm_x=0.2, norm_y=0.2)
    assert "left" not in handler._pressed_mouse_buttons
    handler.mouse_down("right", norm_x=0.3, norm_y=0.3)
    assert "right" in handler._pressed_mouse_buttons
    handler.mouse_up("right", norm_x=0.3, norm_y=0.3)
    assert "right" not in handler._pressed_mouse_buttons
    handler.mouse_wheel(120, norm_x=0.2, norm_y=0.2)
    assert len(mock_inj.sent_inputs) > 0

def test_input_handler_keyboard_events():
    mock_inj = MockInputInjector()
    handler = InputHandler(injector=mock_inj)
    handler.key_down(0x41) # 'A'
    assert 0x41 in handler._pressed_keys
    handler.key_up(0x41)
    assert 0x41 not in handler._pressed_keys
    handler.send_unicode_char("€")
    handler.send_unicode_char("à")
    assert len(mock_inj.sent_inputs) > 0

def test_input_handler_special_keys():
    mock_inj = MockInputInjector()
    handler = InputHandler(injector=mock_inj)
    # Verify special system actions without touching real OS session or windows
    mock_inj.sent_inputs.clear()
    assert handler.send_special("win_d") is True
    assert len(mock_inj.sent_inputs) >= 2 # Win + D (down/up)

    mock_inj.sent_inputs.clear()
    assert handler.send_special("alt_tab") is True
    assert len(mock_inj.sent_inputs) >= 2 # Alt + Tab

    mock_inj.sent_inputs.clear()
    assert handler.send_special("task_mgr") is True
    assert len(mock_inj.sent_inputs) >= 3 # Ctrl + Shift + Esc

    mock_inj.sent_inputs.clear()
    assert handler.send_special("lock") is True
    assert len(mock_inj.sent_inputs) >= 2 # Win + L (down/up)

    mock_inj.sent_inputs.clear()
    assert handler.send_special("copy") is True
    assert len(mock_inj.sent_inputs) >= 2 # Ctrl + C

    mock_inj.sent_inputs.clear()
    assert handler.send_special("paste") is True
    assert len(mock_inj.sent_inputs) >= 2 # Ctrl + V

    assert handler.send_special("select_all") is True
    assert handler.send_special("enter") is True
    assert handler.send_special("backspace") is True
    assert handler.send_special("unknown_command") is False
    assert handler.send_shortcut("ctrl+v") is True
    assert handler.send_shortcut("ctrl+shift+esc") is True

def test_input_handler_shortcuts_and_text():
    mock_inj = MockInputInjector()
    handler = InputHandler(injector=mock_inj)
    # Test typing whole string via mock injector
    mock_inj.sent_inputs.clear()
    handler.send_unicode_char("Ciao Mondo!")
    assert len(mock_inj.sent_inputs) >= len("Ciao Mondo!")

    # Test relative move
    new_pos = handler.move_mouse_relative(10, -5)
    assert new_pos == (10, -5)
    assert mock_inj.cursor_pos == (10, -5)

    # Test click and double click
    mock_inj.sent_inputs.clear()
    handler.mouse_click("left")
    assert len(mock_inj.sent_inputs) >= 2 # down + up

    mock_inj.sent_inputs.clear()
    handler.mouse_double_click("left")
    assert len(mock_inj.sent_inputs) >= 4 # 2x (down + up)

def test_input_handler_raw_pixel_auto_detection():
    mock_inj = MockInputInjector()
    handler = InputHandler(injector=mock_inj)
    captured_inputs = []
    handler._send_input = lambda inp: captured_inputs.append((inp.union.mi.dx, inp.union.mi.dy))

    # Passing raw pixels (e.g. 1920x1080 display, passing x=960, y=540)
    handler.move_mouse(960.0, 540.0, monitor_bounds=(0, 0, 1920, 1080), virtual_bounds=(0, 0, 1920, 1080))
    assert len(captured_inputs) == 1
    abs_x, abs_y = captured_inputs[0]
    # Should be normalized to ~0.5 -> 32767
    assert abs(abs_x - int(0.5 * 65535)) <= 1
    assert abs(abs_y - int(0.5 * 65535)) <= 1

def test_input_handler_button_normalization():
    from src.input_handler import normalize_button
    assert normalize_button(0) == "left"
    assert normalize_button("primary") == "left"
    assert normalize_button(2) == "right"
    assert normalize_button("secondary") == "right"
    assert normalize_button(1) == "middle"
    assert normalize_button("unknown") == "left"

def test_input_handler_multimonitor_mapping():
    mock_inj = MockInputInjector()
    handler = InputHandler(injector=mock_inj)
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

def test_input_handler_release_all_keys():
    mock_inj = MockInputInjector()
    handler = InputHandler(injector=mock_inj)
    handler.key_down(0x41)
    handler.key_down(0x11) # Ctrl
    assert 0x41 in handler._pressed_keys
    assert 0x11 in handler._pressed_keys
    handler.release_all_keys()
    assert len(handler._pressed_keys) == 0
