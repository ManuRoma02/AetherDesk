"""
In-memory regression tests for Remote Input pipeline, Windows Privileges/UAC detection,
and Video Telemetry / FPS diagnostics.

NOTE: As per strict operational constraints, these tests are written for static verification
and in-memory validation only; they are NOT to be executed at runtime.
"""

import sys
import unittest
from unittest.mock import MagicMock, patch
import ctypes
from ctypes import wintypes

from src.input_handler import (
    InputHandler,
    MockInputInjector,
    WindowsInputInjector,
    get_windows_process_context,
    INPUT_KEYBOARD,
    INPUT_MOUSE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_VIRTUALDESK,
    KEYEVENTF_KEYDOWN,
    KEYEVENTF_KEYUP,
)
from src.input_worker import InputWorker, validate_input_command
from src.host_daemon import HostDaemon
from src.protocol import ProtocolMessage


class TestWindowsPrivilegeAndContext(unittest.TestCase):
    """Verify Windows session and integrity level diagnostics."""

    def test_process_context_structure(self):
        # Keep this test purely in-memory even when the suite runs on Windows.
        with patch("src.input_handler.sys.platform", "linux"):
            ctx = get_windows_process_context()
        self.assertIsInstance(ctx, dict)
        self.assertIn("session_id", ctx)
        self.assertIn("is_elevated", ctx)
        self.assertIn("integrity_level", ctx)
        self.assertIn("integrity_level_code", ctx)
        self.assertIn("can_uipi_bypass", ctx)

    def test_mock_elevation_bypass_logic(self):
        with patch("sys.platform", "win32"):
            mock_kernel32 = MagicMock()
            mock_advapi32 = MagicMock()
            # Simulate high integrity token (0x3000)
            with patch.object(ctypes.windll, "kernel32", mock_kernel32), \
                 patch.object(ctypes.windll, "advapi32", mock_advapi32):
                ctx = get_windows_process_context()
                self.assertIsInstance(ctx["can_uipi_bypass"], bool)


class TestWindowsInputInjectorSignaturesAndLifecycle(unittest.TestCase):
    """Verify ctypes signature binding, safe desktop handle lifecycle, and error capture."""

    def test_ctypes_signatures_binding(self):
        mock_user32 = MagicMock()
        injector = object.__new__(WindowsInputInjector)
        injector.user32 = mock_user32
        injector._setup_ctypes_signatures()

        # Verify function declarations were assigned
        self.assertEqual(mock_user32.OpenInputDesktop.restype, wintypes.HANDLE)
        self.assertEqual(mock_user32.SetThreadDesktop.restype, wintypes.BOOL)
        self.assertEqual(mock_user32.CloseDesktop.restype, wintypes.BOOL)
        self.assertEqual(mock_user32.GetThreadDesktop.restype, wintypes.HANDLE)
        self.assertEqual(mock_user32.GetUserObjectInformationW.restype, wintypes.BOOL)
        self.assertEqual(mock_user32.SendInput.restype, wintypes.UINT)
        self.assertEqual(mock_user32.SetCursorPos.restype, wintypes.BOOL)
        self.assertEqual(mock_user32.GetCursorPos.restype, wintypes.BOOL)

    def test_desktop_handle_safe_lifecycle(self):
        """Verify qualified desktop comparison, avoiding redundant SetThreadDesktop and handle leaks."""
        mock_user32 = MagicMock()
        injector = object.__new__(WindowsInputInjector)
        injector.user32 = mock_user32
        import threading
        injector._lock = threading.Lock()
        injector._winsta_initialized = True
        injector._last_desktop_check = 0.0
        injector._current_hdesk = None
        injector._current_desktop_name = None

        # Case 1: Thread is on "winsta0\\default", input desktop is also "winsta0\\default"
        hdesk_input_1 = 0x1001
        mock_user32.OpenInputDesktop.return_value = hdesk_input_1
        # Mock _get_qualified_desktop_name to return "winsta0\\default"
        injector._get_qualified_desktop_name = MagicMock(return_value="winsta0\\default")

        with patch("src.input_handler.ctypes.windll.kernel32") as mock_kernel32:
            mock_kernel32.GetCurrentThreadId.return_value = 1234
            ok = injector.ensure_input_desktop(force=True)
        self.assertTrue(ok)
        # Thread was already on "winsta0\\default": SetThreadDesktop must NOT be called!
        mock_user32.SetThreadDesktop.assert_not_called()
        # Redundant temporary handle must be closed immediately
        mock_user32.CloseDesktop.assert_called_once_with(hdesk_input_1)
        self.assertEqual(injector._current_desktop_name, "winsta0\\default")

        # Case 2: Desktop switch occurs (e.g. from "winsta0\\default" to "winsta0\\winlogon")
        mock_user32.reset_mock()
        hdesk_input_2 = 0x2002
        mock_user32.OpenInputDesktop.return_value = hdesk_input_2
        mock_user32.SetThreadDesktop.return_value = True

        def mock_desktop_name_switch(h):
            if h == hdesk_input_2:
                return "winsta0\\winlogon"
            return "winsta0\\default"

        injector._get_qualified_desktop_name = mock_desktop_name_switch
        with patch("src.input_handler.ctypes.windll.kernel32") as mock_kernel32:
            mock_kernel32.GetCurrentThreadId.return_value = 1234
            ok2 = injector.ensure_input_desktop(force=True)
        self.assertTrue(ok2)
        # Desktops differed: SetThreadDesktop was called with the new desktop handle
        mock_user32.SetThreadDesktop.assert_called_once_with(hdesk_input_2)
        self.assertEqual(injector._current_hdesk, hdesk_input_2)
        self.assertEqual(injector._current_desktop_name, "winsta0\\winlogon")

        # Case 3: Teardown releases the held desktop handle idempotently
        injector.close()
        self.assertIsNone(injector._current_hdesk)
        self.assertIsNone(injector._current_desktop_name)
        mock_user32.CloseDesktop.assert_called_with(hdesk_input_2)
        # Second call does not error or close again
        injector.close()
        mock_user32.CloseDesktop.assert_called_once_with(hdesk_input_2)

    def test_handler_close_forwards_to_injector(self):
        """Verify InputHandler.close() propagates teardown to injector."""
        mock_inj = MockInputInjector()
        handler = InputHandler(injector=mock_inj)
        self.assertFalse(mock_inj.closed)
        handler.close()
        self.assertTrue(mock_inj.closed)

    def test_command_validation_preserves_seq_and_transport(self):
        """Verify that validate_input_command retains seq correlation and transport tag."""
        raw_cmd = {
            "type": "mouse_click",
            "button": "left",
            "x": 0.5,
            "y": 0.5,
            "seq": 42,
            "transport": "webrtc_control"
        }
        is_valid, err_msg, clean_cmd = validate_input_command(raw_cmd)
        self.assertTrue(is_valid)
        self.assertIsNone(err_msg)
        self.assertEqual(clean_cmd.get("seq"), 42)
        self.assertEqual(clean_cmd.get("transport"), "webrtc_control")
        self.assertEqual(clean_cmd.get("action"), "mouse_click")


class TestInputHandlerScanCodesAndInjection(unittest.TestCase):
    """Verify hardware scan code mapping and diagnostic logging in InputHandler."""

    def test_keyboard_scancode_population(self):
        mock_inj = MockInputInjector()
        mock_user32 = MagicMock()
        mock_user32.MapVirtualKeyW.return_value = 0x1E  # 'A' scan code

        handler = InputHandler(injector=mock_inj)
        handler.user32 = mock_user32

        handler.key_down(0x41)  # VK_A
        self.assertEqual(len(mock_inj.sent_inputs), 1)
        inp = mock_inj.sent_inputs[0]
        self.assertEqual(inp.type, INPUT_KEYBOARD)
        self.assertEqual(inp.union.ki.wVk, 0x41)
        self.assertEqual(inp.union.ki.wScan, 0x1E)

        handler.key_up(0x41)
        self.assertEqual(len(mock_inj.sent_inputs), 2)
        inp_up = mock_inj.sent_inputs[1]
        self.assertEqual(inp_up.union.ki.wScan, 0x1E)

    def test_single_send_input_preservation(self):
        """Preserve single SendInput optimization for mouse motion."""
        mock_inj = MockInputInjector(screen_w=1920, screen_h=1080)
        handler = InputHandler(injector=mock_inj)

        handler.move_mouse(0.5, 0.5)
        self.assertEqual(len(mock_inj.sent_inputs), 1)
        inp = mock_inj.sent_inputs[0]
        self.assertEqual(inp.type, INPUT_MOUSE)
        self.assertTrue(inp.union.mi.dwFlags & MOUSEEVENTF_MOVE)
        self.assertTrue(inp.union.mi.dwFlags & MOUSEEVENTF_ABSOLUTE)
        self.assertTrue(inp.union.mi.dwFlags & MOUSEEVENTF_VIRTUALDESK)
        # Ensure fallback mouse_event was not invoked when SendInput succeeded
        self.assertEqual(len(mock_inj.mouse_events), 0)

    def test_injection_diagnostics_and_fallback_tracking(self):
        """Verify that handler accurately tracks SendInput insertion vs fallback attempt."""
        mock_inj = MockInputInjector()
        # Simulate SendInput returning 0 (failure)
        mock_inj.send_input = MagicMock(return_value=False)
        handler = InputHandler(injector=mock_inj)

        with patch("src.input_handler.logger.info") as mock_info:
            handler.mouse_down("left", seq=123)
            mock_info.assert_called_once()
            log_msg = mock_info.call_args[0][0]
            self.assertIn("seq=123", log_msg)
            self.assertIn("send_input_inserted=False", log_msg)
            self.assertIn("fallback_attempted=True", log_msg)
            self.assertNotIn("success=", log_msg)


class TestWebRTCCursorNormalization(unittest.TestCase):
    """Verify cursor coordinate normalization avoids pixel clipping / jump bugs."""

    def test_cursor_normalization_no_pixel_jump(self):
        daemon = object.__new__(HostDaemon)
        mock_native = MagicMock()
        mock_handler = MagicMock()
        mock_handler.get_screen_bounds.return_value = (1920, 1080)
        mock_worker = MagicMock()

        daemon.native_engine = mock_native
        daemon.input_handler = mock_handler
        daemon.input_worker = mock_worker

        # Case 1: cx = 1 (pixel 1, previously bugged with cx > 1.0)
        mock_native.poll_host_cursor.return_value = (1, 1, 101)
        res = daemon._poll_and_enqueue_webrtc_cursor()
        self.assertIsNotNone(res)
        norm_x, norm_y, seq = res
        self.assertAlmostEqual(norm_x, 1.0 / 1920.0, places=5)
        self.assertAlmostEqual(norm_y, 1.0 / 1080.0, places=5)

        # Case 2: cx = 960 (half screen)
        mock_native.poll_host_cursor.return_value = (960, 540, 102)
        res2 = daemon._poll_and_enqueue_webrtc_cursor()
        norm_x2, norm_y2, _ = res2
        self.assertAlmostEqual(norm_x2, 0.5, places=3)
        self.assertAlmostEqual(norm_y2, 0.5, places=3)
        mock_worker.enqueue.assert_called_with({
            "action": "mouse_move",
            "norm_x": 0.5,
            "norm_y": 0.5,
            "seq": 102,
            "transport": "webrtc_cursor",
        })

    def test_webrtc_control_input_is_drained_when_video_falls_back(self):
        """A native video failure must not disable the still-connected input data channel."""
        daemon = object.__new__(HostDaemon)
        daemon._webrtc_active = True
        daemon._webrtc_video_active = False
        daemon._poll_and_enqueue_webrtc_cursor = MagicMock()
        packet = ProtocolMessage.serialize("INPUT", {"type": "key_down", "vk": 65})
        daemon.native_engine = MagicMock()
        daemon.native_engine.poll_host_control.side_effect = [packet, b""]
        daemon.handle_client_message = MagicMock()
        session_state = {"authenticated": True, "session_key": b"test-key"}

        daemon._drain_webrtc_input(b"test-key", session_state)

        daemon._poll_and_enqueue_webrtc_cursor.assert_called_once()
        daemon.native_engine.poll_host_control.assert_called()
        daemon.handle_client_message.assert_called_once()
        self.assertTrue(daemon._webrtc_active)
        self.assertFalse(daemon._webrtc_video_active)


class TestTelemetryAndFPSOverlay(unittest.TestCase):
    """Verify multi-stage metrics, transport tagging, and telemetry fields."""

    def test_telemetry_metadata_fields(self):
        payload = {
            "w": 1920,
            "h": 1080,
            "cx": 100,
            "cy": 200,
            "fid": 42,
            "t_send": 1000.0,
            "cap_ms": 12.5,
            "enc_ms": 8.2,
            "rtt_ms": 35.0,
            "webrtc": False,
            "transport": "WebSocket JPEG",
            "backend": "GDI/Pillow",
            "in_flight": 2,
            "dropped_acks": 0,
        }
        raw = ProtocolMessage.serialize("FRAME", payload, binary_data=b"\xff\xd8\xff...")
        msg_type, dec_payload, dec_bin = ProtocolMessage.deserialize(raw)

        self.assertEqual(msg_type, "FRAME")
        self.assertEqual(dec_payload["transport"], "WebSocket JPEG")
        self.assertEqual(dec_payload["backend"], "GDI/Pillow")
        self.assertEqual(dec_payload["in_flight"], 2)
        self.assertEqual(dec_payload["dropped_acks"], 0)


class TestInputWorkerCorrelatedFailure(unittest.TestCase):
    """Verify worker failures are correlated without exposing exception contents."""

    def test_handler_exception_logs_correlated_completion_and_finishes_queue(self):
        class OneCommandQueue:
            def __init__(self, worker, command):
                self.worker = worker
                self.command = command
                self.finished = 0

            def pop_for_execution(self, timeout=0.05):
                if self.command is None:
                    return None
                command, self.command = self.command, None
                return command

            def finish_execution(self):
                self.finished += 1
                self.worker._running = False

        worker = InputWorker(input_handler=object())
        queue = OneCommandQueue(
            worker,
            {"action": "mouse_click", "seq": 314, "transport": "webrtc_control"},
        )
        worker.queue = queue
        worker._running = True
        worker._execute_command = MagicMock(side_effect=RuntimeError("must not appear in logs"))

        with self.assertLogs("InputWorker", level="INFO") as captured:
            worker._worker_loop()

        output = "\n".join(captured.output)
        self.assertIn("[INPUT_ERROR] seq=314 transport=webrtc_control action=mouse_click", output)
        self.assertIn("[INPUT_COMPLETE] seq=314 transport=webrtc_control action=mouse_click status=handler_error", output)
        self.assertIn("exception_type=RuntimeError", output)
        self.assertNotIn("must not appear in logs", output)
        self.assertEqual(queue.finished, 1)
