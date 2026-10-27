import json
import time
import pytest
import asyncio
import platform
import threading
from pathlib import Path
from unittest.mock import MagicMock

from src.input_handler import InputHandler, MockInputInjector, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MOVE
from src.input_worker import InputWorker, CoalescingQueue, validate_input_command
from src.host_daemon import HostDaemon
from src.security import SecurityManager, IDPAPIEngine, MockTestDPAPIEngine
from src.protocol import ProtocolMessage

def _test_dpapi():
    return MockTestDPAPIEngine() if platform.system() != "Windows" else None

# 1. Tests for MockInputInjector and InputHandler
def test_input_handler_with_mock_injector():
    mock_inj = MockInputInjector(screen_w=1920, screen_h=1080)
    handler = InputHandler(injector=mock_inj)

    # Test single mouse move
    handler.move_mouse(0.5, 0.5)
    assert len(mock_inj.sent_inputs) == 1
    assert mock_inj.sent_inputs[0].type == 0 # INPUT_MOUSE
    assert len(mock_inj.mouse_events) == 0 # No fallback needed since mock succeeded

    # Test drag operation: down -> move -> up
    mock_inj.sent_inputs.clear()
    handler.mouse_down("left", norm_x=0.2, norm_y=0.2)
    assert "left" in handler._pressed_mouse_buttons
    handler.move_mouse(0.4, 0.4)
    handler.mouse_up("left", norm_x=0.4, norm_y=0.4)
    assert "left" not in handler._pressed_mouse_buttons

    # Test exact single click: must issue exactly 1 down, 1 up (total 2 inputs)
    mock_inj.sent_inputs.clear()
    handler.mouse_click("left")
    assert len(mock_inj.sent_inputs) == 2
    assert mock_inj.sent_inputs[0].union.mi.dwFlags == MOUSEEVENTF_LEFTDOWN
    assert mock_inj.sent_inputs[1].union.mi.dwFlags == MOUSEEVENTF_LEFTUP
    assert len(mock_inj.mouse_events) == 0 # No duplicate mouse_event calls

    # Test key press and release
    mock_inj.sent_inputs.clear()
    handler.key_down(0x42) # 'B'
    assert 0x42 in handler._pressed_keys
    handler.key_up(0x42)
    assert 0x42 not in handler._pressed_keys

    # Test release_all_inputs with stuck buttons and keys
    handler.mouse_down("right")
    handler.key_down(0x11)
    assert "right" in handler._pressed_mouse_buttons
    assert 0x11 in handler._pressed_keys
    handler.release_all_inputs()
    assert len(handler._pressed_mouse_buttons) == 0
    assert len(handler._pressed_keys) == 0


# 2. Tests for CoalescingQueue and Event Preservation
def test_coalescing_queue_preserves_order_and_coalesces_moves():
    q = CoalescingQueue(maxsize=10)

    # Enqueue a drag sequence: down -> move 1 -> move 2 -> move 3 -> up
    q.put({"action": "mouse_down", "button": "left"})
    q.put({"action": "mouse_move", "norm_x": 0.1, "norm_y": 0.1})
    q.put({"action": "mouse_move", "norm_x": 0.2, "norm_y": 0.2})
    q.put({"action": "mouse_move", "norm_x": 0.3, "norm_y": 0.3})
    q.put({"action": "mouse_up", "button": "left"})

    # The 3 moves should coalesce into a single move with the latest coords (0.3, 0.3)
    assert q.qsize() == 3

    item1 = q.get()
    assert item1["action"] == "mouse_down"

    item2 = q.get()
    assert item2["action"] == "mouse_move"
    assert item2["norm_x"] == 0.3 and item2["norm_y"] == 0.3

    item3 = q.get()
    assert item3["action"] == "mouse_up"


def test_coalescing_queue_overflow_drops_moves_before_critical_events():
    q = CoalescingQueue(maxsize=3)
    # Fill with 1 discrete, 1 move
    q.put({"action": "key_down", "vk": 0x41})
    q.put({"action": "mouse_move", "norm_x": 0.5, "norm_y": 0.5})

    # Add another discrete event when near capacity
    q.put({"action": "key_down", "vk": 0x42})
    assert q.qsize() == 3

    # Add another critical event: it should drop the move to make room!
    q.put({"action": "key_up", "vk": 0x41})
    assert q.qsize() == 3

    # The items should be key_down 0x41, key_down 0x42, key_up 0x41
    actions = [q.get()["action"] for _ in range(3)]
    assert "mouse_move" not in actions
    assert actions == ["key_down", "key_down", "key_up"]


# 3. Tests for InputWorker Async Execution and Disconnect Cleanup
def test_input_worker_async_execution_and_cleanup():
    mock_inj = MockInputInjector()
    handler = InputHandler(injector=mock_inj)
    worker = InputWorker(handler, maxsize=64)
    worker.start()

    try:
        # Enqueue moves and clicks
        ok1 = worker.enqueue({"action": "mouse_move", "norm_x": 0.1, "norm_y": 0.1})
        ok2 = worker.enqueue({"action": "mouse_click", "button": "left"})
        assert ok1 and ok2

        # Wait for worker to finish
        assert worker.flush(timeout=2.0) is True

        # Simulate stuck inputs and test release_all on disconnect
        handler._pressed_keys.add(0x11)
        handler._pressed_mouse_buttons.add("left")
        worker.release_all()
        assert len(handler._pressed_keys) == 0
        assert len(handler._pressed_mouse_buttons) == 0

    finally:
        worker.stop()


# 4. Strict Validation Tests
def test_validate_input_command_bounds_and_actions():
    # Valid mouse move
    ok, err, clean = validate_input_command({"type": "mouse_move", "x": 0.5, "y": 0.5})
    assert ok is True and clean["action"] == "mouse_move"
    assert clean["norm_x"] == 0.5 and clean["norm_y"] == 0.5

    # Valid key down
    ok, err, clean = validate_input_command({"type": "key_down", "vk": 65})
    assert ok is True and clean["vk"] == 65

    # Invalid action
    ok, err, _ = validate_input_command({"type": "rm_rf_system32", "target": "c:/"})
    assert ok is False
    assert "not in allowed" in err

    # Out of bounds coordinates (e.g. Inf, NaN, huge numbers)
    ok, err, _ = validate_input_command({"type": "mouse_move", "x": 999999.0, "y": 0.5})
    assert ok is False
    assert "out of reasonable range" in err

    # Non-numeric coordinate
    ok, err, _ = validate_input_command({"type": "mouse_move", "x": "bad_coord", "y": 0.5})
    assert ok is False

    # Out of range vk code
    ok, err, _ = validate_input_command({"type": "key_down", "vk": 99999})
    assert ok is False
    assert "byte range" in err

    # Oversized text payload (buffer bloat protection)
    ok, err, _ = validate_input_command({"type": "unicode_char", "text": "A" * 5000})
    assert ok is False
    assert "maximum allowed length" in err


# 5. Insecure Downgrade Elimination in HostDaemon
def test_host_daemon_no_insecure_downgrade(tmp_path):
    daemon = HostDaemon(config_dir=str(tmp_path), headless=True, dpapi_engine=_test_dpapi(), start_worker=False)
    try:
        daemon.security.set_password("SecurePass999")

        # 1. Unauthenticated request rejected
        res = daemon.handle_client_message("INPUT", {"cmd": {"type": "mouse_move"}}, None, {})
        m_type, _, _ = ProtocolMessage.deserialize(res)
        assert m_type == "AUTH_FAILED"

        # 2. Authenticated session without binary encrypted payload is rejected
        session_state = {"authenticated": True, "session_key": b"fake_session_key_32_bytes_123456"}
        res_no_bin = daemon.handle_client_message("INPUT", {"cmd": {"type": "mouse_move"}}, None, session_state)
        assert res_no_bin is None

        # 3. Authenticated session with unencrypted binary payload is rejected (no plaintext downgrade)
        plaintext_bytes = b'{"type": "mouse_move", "x": 0.5, "y": 0.5}'
        res_plain_bin = daemon.handle_client_message("INPUT", {}, plaintext_bytes, session_state)
        assert res_plain_bin is None

        # 4. Authenticated session with properly encrypted payload succeeds
        sec_client = SecurityManager(config_dir=str(tmp_path / "client"), dpapi_engine=_test_dpapi())
        enc_payload = sec_client.encrypt_packet(plaintext_bytes, session_state["session_key"])
        res_valid = daemon.handle_client_message("INPUT", {}, enc_payload, session_state)
        assert res_valid is not None
        m_type, p, _ = ProtocolMessage.deserialize(res_valid)
        assert m_type == "ACK"
        assert p["status"] == "ok"
    finally:
        daemon.stop()


# 6. Legacy Credential Migration Compatibility
def test_legacy_config_migration_to_dpapi(tmp_path):
    config_file = tmp_path / "config.json"
    # Simulate a legacy config containing plaintext credentials
    legacy_data = {
        "device_id": "123 456 789",
        "pwd_salt": "0123456789abcdef0123456789abcdef",
        "iterations": 600000,
        "master_key": "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
        "password_display": "LegacySecret2026!"
    }
    config_file.write_text(json.dumps(legacy_data), encoding="utf-8")

    # Load SecurityManager
    sec = SecurityManager(config_dir=str(tmp_path), dpapi_engine=_test_dpapi())

    # Plaintext fields MUST be removed from config on disk
    saved_cfg = json.loads(config_file.read_text(encoding="utf-8"))
    assert "password_display" not in saved_cfg
    assert "master_key" not in saved_cfg

    # DPAPI-protected fields MUST be present
    assert "password_display_dpapi" in saved_cfg
    assert "master_key_dpapi" in saved_cfg

    # User's password MUST remain intact and readable via DPAPI
    assert sec.get_password_display() == "LegacySecret2026!"
    assert sec.get_or_create_device_id() == "123 456 789"


# 7. Streaming Pipeline Bounded In-Flight & Delayed ACK Handling
def test_streaming_pipeline_bounded_in_flight_and_telemetry(tmp_path):
    daemon = HostDaemon(config_dir=str(tmp_path), headless=True, dpapi_engine=_test_dpapi(), start_worker=False)
    try:
        # Initial state
        assert daemon._in_flight_frame is False
        assert len(daemon._in_flight_fids) == 0

        # Simulate frame sent
        daemon._in_flight_fids.add(101)
        daemon._frame_send_times[101] = time.time() - 0.025 # 25ms ago
        daemon._in_flight_frame = True

        # Receive FRAME_ACK for frame 101
        daemon.handle_client_message("FRAME_ACK", {"fid": 101}, None, {"authenticated": True})

        assert 101 not in daemon._in_flight_fids
        assert daemon._in_flight_frame is False
        assert daemon._frame_ack_event.is_set() is True
        assert daemon.telemetry.get("last_ack_rtt_ms", 0) >= 20.0
        assert daemon.telemetry.get("total_acks", 0) == 1

        # Simulate multiple in-flight frames and out-of-order / cumulative ACK
        daemon._in_flight_fids.add(102)
        daemon._in_flight_fids.add(103)
        daemon._frame_send_times[102] = time.time() - 0.050
        daemon._frame_send_times[103] = time.time() - 0.020

        # Receiving ACK for 103 should clean up 103 and older frame 102
        daemon.handle_client_message("FRAME_ACK", {"fid": 103}, None, {"authenticated": True})
        assert len(daemon._in_flight_fids) == 0
        assert daemon._in_flight_frame is False
    finally:
        daemon.stop()


# 8. Targeted Queue Full Tests (Never drop discrete events)
def test_coalescing_queue_all_discrete_overflow_rejects_without_loss():
    q = CoalescingQueue(maxsize=3)
    assert q.put({"action": "key_down", "vk": 0x41}) is True
    assert q.put({"action": "mouse_down", "button": "left"}) is True
    assert q.put({"action": "key_up", "vk": 0x41}) is True
    assert q.qsize() == 3

    # Attempt to put a 4th discrete event when queue has ONLY discrete events:
    # Must reject (return False), and MUST NOT drop any of the 3 existing discrete events
    res = q.put({"action": "mouse_up", "button": "left"})
    assert res is False
    assert q.qsize() == 3

    actions = [q.get()["action"] for _ in range(3)]
    assert actions == ["key_down", "mouse_down", "key_up"]


# 9. Targeted Relative Movement Coalescing (Delta Summation)
def test_coalescing_queue_relative_move_deltas_summed():
    q = CoalescingQueue(maxsize=5)
    # Put first relative move
    assert q.put({"action": "mouse_move", "dx": 10.0, "dy": 5.0}) is True
    # Put second relative move
    assert q.put({"action": "mouse_move", "dx": 2.5, "dy": -3.0}) is True
    # Put third relative move
    assert q.put({"action": "mouse_move", "dx": -1.0, "dy": 4.0}) is True

    assert q.qsize() == 1
    item = q.get()
    assert item["action"] == "mouse_move"
    assert pytest.approx(item["dx"]) == 11.5
    assert pytest.approx(item["dy"]) == 6.0


# 10. Targeted Flush While Command is Executing
def test_input_worker_flush_waits_for_executing_command():
    mock_inj = MockInputInjector()
    handler = InputHandler(injector=mock_inj)

    original_key_down = handler.key_down
    def slow_key_down(vk):
        time.sleep(0.08)
        return original_key_down(vk)
    handler.key_down = slow_key_down

    worker = InputWorker(handler, maxsize=16)
    worker.start()
    try:
        worker.enqueue({"action": "key_down", "vk": 0x42})
        t_start = time.time()
        ok = worker.flush(timeout=1.0)
        elapsed = time.time() - t_start
        assert ok is True
        assert elapsed >= 0.07
        assert 0x42 in handler._pressed_keys
    finally:
        worker.stop()


# 11. Targeted Daemon Stop and Disconnect Releases Pressed Inputs
def test_daemon_stop_and_disconnect_releases_pressed_inputs(tmp_path):
    mock_inj = MockInputInjector()
    handler = InputHandler(injector=mock_inj)
    daemon = HostDaemon(config_dir=str(tmp_path), headless=True, dpapi_engine=_test_dpapi())
    try:
        daemon.input_handler = handler
        daemon.input_worker.input_handler = handler

        # Simulate client held mouse down and pressed a key
        daemon.input_handler.mouse_down("left")
        daemon.input_handler.key_down(0x11) # CTRL
        assert "left" in daemon.input_handler._pressed_mouse_buttons
        assert 0x11 in daemon.input_handler._pressed_keys

        # Trigger CLIENT_DISCONNECTED
        session_state = {"authenticated": True}
        daemon.handle_client_message("CLIENT_DISCONNECTED", {}, None, session_state)
        assert len(daemon.input_handler._pressed_mouse_buttons) == 0
        assert len(daemon.input_handler._pressed_keys) == 0

        # Now press again and test daemon.stop()
        daemon.input_handler.mouse_down("right")
        daemon.input_handler.key_down(0x41)
        assert "right" in daemon.input_handler._pressed_mouse_buttons
        assert 0x41 in daemon.input_handler._pressed_keys

        daemon.stop()
        assert len(daemon.input_handler._pressed_mouse_buttons) == 0
        assert len(daemon.input_handler._pressed_keys) == 0
    finally:
        daemon.stop()


# 12. Targeted DPAPI Failure and Corruption Safe Failure
def test_dpapi_error_and_corrupt_blob_fails_safely_without_overwriting(tmp_path):
    config_file = tmp_path / "config.json"
    # Config with a corrupt / unrecoverable DPAPI blob
    config_file.write_text(json.dumps({
        "device_id": "999 888 777",
        "password_display_dpapi": "deadbeefcafebabe" # cannot be decrypted
    }), encoding="utf-8")

    sec = SecurityManager(config_dir=str(tmp_path), dpapi_engine=_test_dpapi())

    # Calling get_password_display MUST raise RuntimeError, NOT silently overwrite config
    with pytest.raises(RuntimeError) as exc_info:
        sec.get_password_display()
    assert "DPAPI recovery failed" in str(exc_info.value)

    # Config on disk MUST NOT have been overwritten with a newly generated password
    cfg = json.loads(config_file.read_text(encoding="utf-8"))
    assert cfg["password_display_dpapi"] == "deadbeefcafebabe"

    # When DPAPI protect fails in set_password, it must raise RuntimeError and NOT save corrupt config
    class FailingDPAPIEngine(IDPAPIEngine):
        def protect(self, data: bytes):
            return None
        def unprotect(self, data: bytes):
            return None

    failing_sec = SecurityManager(config_dir=str(tmp_path), dpapi_engine=FailingDPAPIEngine())
    with pytest.raises(RuntimeError) as exc_info2:
        failing_sec.set_password("NewPassword123!")
    assert "DPAPI protection failed" in str(exc_info2.value)


# 13. Targeted Migration Failure Preserves Legacy Plaintext
def test_dpapi_migration_failure_preserves_legacy_credentials(tmp_path):
    config_file = tmp_path / "config.json"
    legacy_data = {
        "device_id": "111 222 333",
        "password_display": "MyPreciousPassword",
        "master_key": "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899"
    }
    config_file.write_text(json.dumps(legacy_data), encoding="utf-8")

    class FailingDPAPIEngine(IDPAPIEngine):
        def protect(self, data: bytes):
            return None
        def unprotect(self, data: bytes):
            return None

    sec = SecurityManager(config_dir=str(tmp_path), dpapi_engine=FailingDPAPIEngine())

    # Since protect failed, legacy plaintext fields MUST NOT be deleted or overwritten
    cfg = json.loads(config_file.read_text(encoding="utf-8"))
    assert cfg.get("password_display") == "MyPreciousPassword"
    assert cfg.get("master_key") == "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899"
    assert "password_display_dpapi" not in cfg


# 14. Real End-to-End Test for run_capture_stream with Mock Capture & WebSocket
def test_run_capture_stream_end_to_end_with_mock_ws_and_backpressure(tmp_path):
    async def _test_body():
        daemon = HostDaemon(config_dir=str(tmp_path), headless=True, dpapi_engine=_test_dpapi())
        try:
            daemon.running = True

            # Mock capture returning simulated frame bytes
            mock_cap = MagicMock()
            mock_cap.capture_frame.return_value = (b"\xff\xd8\xff\xe0fake_jpeg", (1280, 720), None)
            mock_cap.get_active_monitor_bounds.return_value = None
            mock_cap.get_virtual_desktop_bounds.return_value = None
            mock_cap.switch_desktop.return_value = None
            mock_cap._last_thumb = None
            daemon.capture = mock_cap

            sent_messages = []
            class MockWebSocket:
                def __init__(self):
                    self.open = True
                async def send(self, data):
                    sent_messages.append(data)
                    try:
                        msg_type, p, _ = ProtocolMessage.deserialize(data)
                        if msg_type == "FRAME":
                            fid = p.get("fid")
                            if fid == 1:
                                # Immediately ACK frame 1
                                daemon.handle_client_message("FRAME_ACK", {"fid": 1}, None, {"authenticated": True})
                            # Frame 2 is delayed to trigger backpressure/timeout
                    except Exception:
                        pass

            ws = MockWebSocket()
            session_key = b"0" * 32

            stream_task = asyncio.create_task(daemon.run_capture_stream(ws, session_key))
            # Give stream loop time to capture multiple frames and hit in-flight bounds
            await asyncio.sleep(0.15)
            daemon._streaming_active = False
            daemon.running = False
            daemon._frame_ack_event.set()
            await asyncio.wait_for(stream_task, timeout=1.0)

            assert len(sent_messages) >= 2
            assert "last_cap_ms" in daemon.telemetry
            assert "last_enc_ms" in daemon.telemetry
            assert daemon.telemetry.get("frames_sent", 0) >= 2
        finally:
            daemon.stop()

    asyncio.run(_test_body())


# 15. Queue headroom for releases and safe recovery when release is rejected
def test_full_queue_down_accepted_and_up_rejected_releases_input():
    mock_inj = MockInputInjector()
    handler = InputHandler(injector=mock_inj)
    worker = InputWorker(handler, maxsize=3, reserve_releases=0)
    worker.start()
    try:
        # Accept down
        assert worker.enqueue({"action": "key_down", "vk": 0x41}) is True
        assert worker.flush(timeout=1.0) is True
        assert 0x41 in handler._pressed_keys

        # Fill queue with other discrete events
        assert worker.enqueue({"action": "key_down", "vk": 0x42}) is True
        assert worker.enqueue({"action": "key_down", "vk": 0x43}) is True

        # Now queue is completely saturated (2 items + 1 being executed/executing or full 3 items)
        # Attempt to enqueue key_up: when full, rejection must safely release input
        assert worker.enqueue({"action": "key_down", "vk": 0x44}) is True
        ok = worker.enqueue({"action": "key_up", "vk": 0x41})
        assert ok is False  # Rejected due to saturation!

        # Because key_up was rejected, emergency release must have freed 0x41
        assert 0x41 not in handler._pressed_keys
    finally:
        worker.stop()


# 16. Worker release_all timeout aborts concurrent injection
def test_worker_release_all_timeout_aborts_concurrent_injection():
    mock_inj = MockInputInjector()
    handler = InputHandler(injector=mock_inj)
    worker = InputWorker(handler)

    # Acquire worker._exec_lock externally to simulate long/hung injection command
    worker._exec_lock.acquire()
    try:
        # Calling release_all with short timeout must abort and return False without calling handler
        res = worker.release_all(timeout=0.02)
        assert res is False
    finally:
        worker._exec_lock.release()


# 17. Deterministic atomic flush verification
def test_flush_atomic_no_gap_between_pop_and_executing():
    mock_inj = MockInputInjector()
    handler = InputHandler(injector=mock_inj)
    worker = InputWorker(handler, maxsize=16)

    # Hook handler to record state during execution
    executed = []
    def slow_exec(vk):
        # Under queue._lock, is_idle MUST be False while executing
        assert worker.queue.is_idle() is False
        time.sleep(0.04)
        executed.append(vk)

    handler.key_down = slow_exec
    worker.start()
    try:
        worker.enqueue({"action": "key_down", "vk": 0x50})
        # flush must wait and not return until execution is done
        ok = worker.flush(timeout=1.0)
        assert ok is True
        assert len(executed) == 1
    finally:
        worker.stop()


# 18. Sleep prevention lifecycle on HostDaemon
def test_host_daemon_sleep_prevention_lifecycle(tmp_path):
    daemon = HostDaemon(config_dir=str(tmp_path), headless=True, dpapi_engine=_test_dpapi(), start_worker=False)
    try:
        # 1. Idle host in attesa: sleep prevention must be False
        assert daemon._sleep_prevented is False

        # 2. Authenticated session active: sleep prevention must become True
        session_state = {"client_ip": "127.0.0.1"}
        pwd = daemon.security.get_password_display()
        stored_master = daemon.security.get_master_key()
        salt = b"salt"
        key = daemon.security.derive_session_key(stored_master or pwd, salt)
        enc_token = daemon.security.encrypt_packet(b"AETHER_AUTH_V1", key)
        daemon.handle_client_message("AUTH_REQUEST", {"salt": salt.hex()}, enc_token, session_state)
        assert session_state.get("authenticated") is True
        assert daemon._sleep_prevented is True

        # 3. Client disconnected: sleep prevention must revert to False
        daemon.handle_client_message("CLIENT_DISCONNECTED", {}, None, session_state)
        assert daemon._sleep_prevented is False

        # 4. Daemon stop restores sleep state
        daemon.prevent_sleep(True)
        daemon.stop()
        assert daemon._sleep_prevented is False
    finally:
        daemon.stop()


# 19. Direct clipboard actions in input command
def test_input_worker_executes_clipboard_actions():
    mock_inj = MockInputInjector()
    handler = InputHandler(injector=mock_inj)
    mock_clip = MagicMock()
    worker = InputWorker(handler, clipboard=mock_clip)

    worker.execute_now({"action": "set_clipboard", "text": "ClipTextFromInput"})
    mock_clip.set_text.assert_called_with("ClipTextFromInput", update_last=True)


# 20. Client handles host ERROR and INPUT_REJECTED messages
def test_client_handles_host_error_and_input_rejected(tmp_path):
    from src.client import RemoteClient, ConnectionClosed
    client = RemoteClient(config_dir=str(tmp_path), dpapi_engine=_test_dpapi())
    status_updates = []
    error_payloads = []
    client.on_status_callback = lambda msg: status_updates.append(msg)
    client.on_error_callback = lambda err, payload: error_payloads.append((err, payload))

    # Simulate receiving ERROR frame from host
    err_packet = ProtocolMessage.serialize("ERROR", {"error": "input_queue_saturated", "action": "key_down"})

    async def _test_client():
        class MockWS:
            def __init__(self):
                self.messages = [err_packet]
            async def recv(self):
                if self.messages:
                    return self.messages.pop(0)
                client.state.is_connected = False
                raise ConnectionClosed(None, None)
        client.websocket = MockWS()
        client.state.is_connected = True
        await client._receive_loop()

    asyncio.run(_test_client())
    assert any("input_queue_saturated" in s for s in status_updates)
    assert len(error_payloads) == 1
    assert error_payloads[0][0] == "input_queue_saturated"


# 21. Non-Windows SecurityManager without mock fails safely without saving
def test_security_manager_non_windows_without_mock_fails_safely(tmp_path, monkeypatch):
    import platform
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    sec = SecurityManager(config_dir=str(tmp_path), dpapi_engine=None)
    assert sec.dpapi_engine is None

    # Without secure DPAPI engine, set_password must raise RuntimeError
    with pytest.raises(RuntimeError) as exc:
        sec.set_password("SomePass123!")
    assert "Secure credential protection engine unavailable" in str(exc.value)

    # get_password_display on empty config must also raise without saving
    with pytest.raises(RuntimeError) as exc2:
        sec.get_password_display()
    assert "secure DPAPI engine is unavailable" in str(exc2.value)


# 22. InputHandler initialization without ctypes.windll (non-Windows check)
def test_input_handler_initialization_without_windll():
    import ctypes
    orig_windll = getattr(ctypes, "windll", None)
    try:
        # Simulate non-Windows environment where ctypes has no windll attribute
        if hasattr(ctypes, "windll"):
            delattr(ctypes, "windll")
        mock_inj = MockInputInjector()
        # Must not raise AttributeError: module 'ctypes' has no attribute 'windll'
        h = InputHandler(injector=mock_inj)
        assert h.injector is mock_inj
        assert h.user32 is None
    finally:
        if orig_windll is not None:
            ctypes.windll = orig_windll


# 23. Concurrency Tracking Mock Injector for Deterministic Regression Tests
class ConcurrencyTrackingInjector(MockInputInjector):
    def __init__(self):
        super().__init__()
        self.active_calls = 0
        self.max_concurrent_calls = 0
        self._block_event = threading.Event()
        self._entered_event = threading.Event()
        self._lock = threading.Lock()

    def _enter(self):
        with self._lock:
            self.active_calls += 1
            if self.active_calls > self.max_concurrent_calls:
                self.max_concurrent_calls = self.active_calls

    def _exit(self):
        with self._lock:
            self.active_calls -= 1

    def send_input(self, inp):
        self._enter()
        try:
            self._entered_event.set()
            # Wait if blocking event is not set (with 2s fallback timeout)
            self._block_event.wait(timeout=2.0)
            return super().send_input(inp)
        finally:
            self._exit()

    def set_cursor_pos(self, x, y):
        self._enter()
        try:
            return super().set_cursor_pos(x, y)
        finally:
            self._exit()

    def mouse_event(self, flags, dx, dy, data, extra_info):
        self._enter()
        try:
            return super().mouse_event(flags, dx, dy, data, extra_info)
        finally:
            self._exit()

    def keybd_event(self, vk, scan, flags, extra_info):
        self._enter()
        try:
            return super().keybd_event(vk, scan, flags, extra_info)
        finally:
            self._exit()


# 24. Deterministic Test: Queued down followed by rejected up never leaves key/mouse pressed
def test_queued_down_followed_by_rejected_up_does_not_leave_input_pressed():
    mock_inj = MockInputInjector()
    handler = InputHandler(injector=mock_inj)
    # Queue maxsize=1, reserve_releases=0 so a second item is guaranteed to be rejected on queue saturation
    worker = InputWorker(handler, maxsize=1, reserve_releases=0)

    # 1. Enqueue key_down(vk=65) without starting worker thread yet: remains in queue
    ok_down = worker.enqueue({"action": "key_down", "vk": 65})
    assert ok_down is True
    assert worker.queue.qsize() == 1

    # 2. Enqueue key_up(vk=65) while queue is full: rejected
    # Emergency recovery must purge pending queued down and run synchronized release
    ok_up = worker.enqueue({"action": "key_up", "vk": 65})
    assert ok_up is False

    # Pending queued down MUST be cleared so it cannot execute later
    assert worker.queue.qsize() == 0

    # Start worker and flush to verify no down event is executed
    worker.start()
    try:
        worker.flush(timeout=0.5)
    finally:
        worker.stop(timeout=0.5)

    assert 65 not in handler._pressed_keys
    assert len(handler._pressed_keys) == 0
    assert len(handler._pressed_mouse_buttons) == 0

    # Also test case where down was already executed by worker before up is rejected
    worker2 = InputWorker(handler, maxsize=1, reserve_releases=0)
    worker2.start()
    try:
        worker2.enqueue({"action": "mouse_down", "button": "left"})
        worker2.flush(timeout=1.0)
        assert "left" in handler._pressed_mouse_buttons

        # Fill queue with a dummy command to saturate it
        worker2.enqueue({"action": "click", "button": "left"})

        # Now enqueue mouse_up which gets rejected due to saturation
        ok_m_up = worker2.enqueue({"action": "mouse_up", "button": "left"})
        assert ok_m_up is False

        # Final state verification: button must be safely released, never left pressed
        worker2.flush(timeout=1.0)
        assert "left" not in handler._pressed_mouse_buttons
        assert len(handler._pressed_mouse_buttons) == 0
    finally:
        worker2.stop(timeout=1.0)


# 25. Deterministic Test: HostDaemon.stop() & disconnect during blocked injection avoid concurrent calls
def test_host_daemon_stop_and_disconnect_while_injection_blocked_no_concurrent_calls(tmp_path):
    inj = ConcurrencyTrackingInjector()
    handler = InputHandler(injector=inj)
    daemon = HostDaemon(config_dir=str(tmp_path), headless=True, dpapi_engine=_test_dpapi())
    try:
        daemon.input_handler = handler
        daemon.input_worker.input_handler = handler

        # 1. Enqueue command that enters send_input and blocks
        daemon.input_worker.enqueue({"action": "mouse_down", "button": "left"})
        entered = inj._entered_event.wait(timeout=1.0)
        assert entered is True
        assert inj.active_calls == 1

        # 2. While injection is blocked inside injector, simulate CLIENT_DISCONNECTED
        session_state = {"authenticated": True, "session_key": b"0" * 32}
        daemon.handle_client_message("CLIENT_DISCONNECTED", {}, None, session_state)
        # Must NOT have made concurrent call on injector
        assert inj.max_concurrent_calls == 1

        # 3. Simulate HostDaemon.stop() while still blocked
        daemon.stop(timeout=0.05)
        # Must NOT have made concurrent call on injector
        assert inj.max_concurrent_calls == 1

        # 4. Unblock injector so command completes
        inj._block_event.set()
        daemon.input_worker.flush(timeout=1.0)
        daemon.input_worker.stop(timeout=1.0)

        # In no point did concurrent calls ever exceed 1
        assert inj.max_concurrent_calls == 1
        assert inj.active_calls == 0
    finally:
        inj._block_event.set()
        daemon.stop(timeout=0.05)


# 26. Deterministic Test: Worker timeout and subsequent input state recovery
def test_worker_timeout_and_subsequent_input_state_recovery():
    inj = ConcurrencyTrackingInjector()
    handler = InputHandler(injector=inj)
    worker = InputWorker(handler, maxsize=16)
    worker.start()
    try:
        # Press a key initially
        inj._block_event.set() # allow immediate completion
        worker.enqueue({"action": "key_down", "vk": 70})
        worker.flush(timeout=1.0)
        assert 70 in handler._pressed_keys

        # Now make the injector block
        inj._block_event.clear()
        inj._entered_event.clear()

        # Enqueue a mouse_down that will block in injector
        worker.enqueue({"action": "mouse_down", "button": "left"})
        assert inj._entered_event.wait(timeout=1.0) is True

        # Call release_all with a small timeout (0.05s) - it MUST time out and return False
        ok_release = worker.release_all(timeout=0.05)
        assert ok_release is False
        assert worker._release_pending is True

        # No concurrent call was made while blocked
        assert inj.max_concurrent_calls == 1

        # Unblock the injector
        inj._block_event.set()

        # Subsequent recovery: recover_inputs() clears pending and releases all inputs under lock
        recovered = worker.recover_inputs(timeout=1.0)
        assert recovered is True
        assert worker._release_pending is False

        # All inputs must be safely released
        assert len(handler._pressed_keys) == 0
        assert len(handler._pressed_mouse_buttons) == 0

        worker.stop(timeout=1.0)
    finally:
        inj._block_event.set()
        worker.stop(timeout=1.0)


def test_input_worker_release_pending_executed_when_queue_empty():
    """
    Verify that when release_all times out while a long command is executing,
    and then the queue is emptied/cleared, the worker loop thread immediately
    executes release_all_inputs() under _exec_lock as soon as the lock is freed,
    even without any new incoming commands.
    """
    inj = ConcurrencyTrackingInjector()
    handler = InputHandler(injector=inj)
    worker = InputWorker(handler, maxsize=16)
    worker.start()
    try:
        # Press key 75
        inj._block_event.set()
        worker.enqueue({"action": "key_down", "vk": 75})
        worker.flush(timeout=1.0)
        assert 75 in handler._pressed_keys

        # Block injector on a command
        inj._block_event.clear()
        inj._entered_event.clear()
        worker.enqueue({"action": "key_down", "vk": 76})
        assert inj._entered_event.wait(timeout=1.0) is True

        # Timeout on release_all sets _release_pending = True
        ok = worker.release_all(timeout=0.02)
        assert ok is False
        assert worker._release_pending is True

        # Clear queue so queue is completely empty
        worker.clear()
        assert worker.queue.qsize() == 0

        # Unblock injector: the worker thread should finish the command,
        # notice _release_pending, and execute release_all_inputs under _exec_lock!
        inj._block_event.set()

        # Wait briefly for worker thread to process _release_pending
        deadline = time.time() + 1.0
        while worker._release_pending and time.time() < deadline:
            time.sleep(0.01)

        assert worker._release_pending is False
        assert len(handler._pressed_keys) == 0
        assert inj.max_concurrent_calls == 1
    finally:
        inj._block_event.set()
        worker.stop(timeout=1.0)
