import platform
import pytest
from src.host_daemon import HostDaemon
from src.protocol import ProtocolMessage
from src.security import SecurityManager, MockTestDPAPIEngine

def _test_dpapi():
    return MockTestDPAPIEngine() if platform.system() != "Windows" else None

def test_daemon_sleep_prevention(tmp_path):
    daemon = None
    try:
        daemon = HostDaemon(config_dir=str(tmp_path), headless=True, dpapi_engine=_test_dpapi(), start_worker=False)
        res = daemon.prevent_sleep(True)
        assert res is True
        daemon.prevent_sleep(False)
    finally:
        if daemon is not None:
            daemon.stop()

def test_daemon_status(tmp_path):
    daemon = None
    try:
        daemon = HostDaemon(config_dir=str(tmp_path), headless=True, dpapi_engine=_test_dpapi(), start_worker=False)
        status = daemon.get_status()
        assert "device_id" in status
        assert "running" in status
        assert "connected_clients" in status
    finally:
        if daemon is not None:
            daemon.stop()

def test_daemon_auth_and_input_handling(tmp_path):
    daemon = None
    try:
        daemon = HostDaemon(config_dir=str(tmp_path), headless=True, dpapi_engine=_test_dpapi(), start_worker=True)
        daemon.security.set_password("Secret123")

        # 1. Unauthenticated challenge without token MUST fail (F-02)
        session_state = {}
        salt = b"test_salt_123456"
        resp_missing = daemon.handle_client_message("AUTH_REQUEST", {"salt": salt.hex()}, None, session_state)
        m_type, _, _ = ProtocolMessage.deserialize(resp_missing)
        assert m_type == "AUTH_FAILED"
        assert session_state.get("authenticated") is False

        # 2. Challenge with valid token succeeds (using distinct client SecurityManager)
        client_sec = SecurityManager(config_dir=str(tmp_path / "client_sec"), dpapi_engine=_test_dpapi())
        master = daemon.security.get_master_key()
        session_key = client_sec.derive_session_key(master, salt)
        token = client_sec.encrypt_packet(b"AETHER_AUTH_V1", session_key)
        resp_bytes = daemon.handle_client_message("AUTH_REQUEST", {"salt": salt.hex()}, token, session_state)
        m_type, _, _ = ProtocolMessage.deserialize(resp_bytes)
        assert m_type == "AUTH_OK"
        assert session_state.get("authenticated") is True
        assert "session_key" in session_state

        # 3. Valid encrypted mouse move
        key = session_state["session_key"]
        plaintext_input = b'{"type": "mouse_move", "x": 0.5, "y": 0.5}'
        encrypted = client_sec.encrypt_packet(plaintext_input, key)
        res = daemon.handle_client_message("INPUT", {}, encrypted, session_state)
        assert res is not None
    finally:
        if daemon is not None:
            daemon.stop()

def test_daemon_set_quality(tmp_path):
    daemon = None
    try:
        daemon = HostDaemon(config_dir=str(tmp_path), headless=True, dpapi_engine=_test_dpapi(), start_worker=False)
        # Unauthenticated request rejected (F-01)
        unauth_res = daemon.handle_client_message("SET_QUALITY", {"quality": 85}, None, {})
        m_type, _, _ = ProtocolMessage.deserialize(unauth_res)
        assert m_type == "AUTH_FAILED"

        # Authenticated request succeeds
        res = daemon.handle_client_message("SET_QUALITY", {"quality": 85}, None, {"authenticated": True})
        assert res is not None
        assert daemon.capture.quality == 85
    finally:
        if daemon is not None:
            daemon.stop()


@pytest.mark.anyio
async def test_run_capture_stream_cleanup_on_ws_close_and_task_isolation(tmp_path):
    """
    Verify coordinated, idempotent cleanup when run_capture_stream terminates.
    1. Normal task exit: verifies cleanup executes, inputs released, in-flight queues cleared,
       and was_cancelled does not cause UnboundLocalError.
    2. Superseded task (both cancelled and normal exit): an active task suspended inside its
       streaming loop when superseded must NOT modify _in_flight_fids, _frame_send_times, flags,
       release inputs, or clear state belonging to the newer stream session when its finally executes,
       and does not raise UnboundLocalError.
    3. Real in-memory InputWorker release: verifies that held keys and mouse buttons are genuinely
       cleared by handle_client_arrived before accepting a new session.
    4. Deterministic fail-closed error handling when input release fails or times out.
    5. All tasks created in the test are guaranteed cancelled and awaited in finally.
    """
    from unittest.mock import AsyncMock, MagicMock
    import asyncio
    from src.input_handler import MockInputInjector, InputHandler
    from src.input_worker import InputWorker

    real_worker = None
    daemon = None
    created_tasks = []
    try:
        mock_injector = MockInputInjector()
        real_handler = InputHandler(injector=mock_injector)
        real_worker = InputWorker(input_handler=real_handler)
        real_worker.start()

        daemon = HostDaemon(config_dir=str(tmp_path), headless=True, dpapi_engine=_test_dpapi(), start_worker=False)
        daemon.running = True
        daemon.input_handler = real_handler
        daemon.input_worker = real_worker

        # Spy on _safe_release_inputs while preserving real InputWorker / InputHandler execution
        real_safe_release = daemon._safe_release_inputs
        released_calls = []

        def spied_safe_release(timeout=1.0):
            released_calls.append(timeout)
            return real_safe_release(timeout)

        daemon._safe_release_inputs = spied_safe_release
        # ----------------------------------------------------------------------
        # Case 1: Normal task exit on websocket closed
        # ----------------------------------------------------------------------
        mock_ws_closed = MagicMock()
        mock_ws_closed.state = MagicMock()
        mock_ws_closed.state.name = "CLOSED"
        mock_ws_closed.send = AsyncMock()

        session_state_1 = {"authenticated": True, "session_key": b"A" * 32}
        daemon._current_session_state = session_state_1
        daemon._stream_generation = 1
        daemon._in_flight_fids = {101}
        daemon._frame_send_times = {101: 1000.0}

        stream_coro_1 = daemon.run_capture_stream(mock_ws_closed, session_state_1["session_key"], session_state_1, stream_generation=1)
        task_1 = asyncio.create_task(stream_coro_1)
        created_tasks.append(task_1)
        daemon._stream_task = task_1
        await task_1

        # Verify normal exit completed without UnboundLocalError
        assert task_1.done() is True
        assert task_1.exception() is None
        assert daemon._streaming_active is False
        assert len(daemon._in_flight_fids) == 0
        assert len(daemon._frame_send_times) == 0
        assert len(released_calls) == 1
        assert len(session_state_1) == 0
        assert daemon._current_session_state is None
        assert daemon._stream_task is None

        # ----------------------------------------------------------------------
        # Case 2: Task suspended INSIDE streaming loop, then superseded by generation 2 (cancelled)
        # ----------------------------------------------------------------------
        released_calls.clear()
        in_loop_barrier = asyncio.Event()
        resume_barrier = asyncio.Event()

        mock_ws_open = MagicMock()
        mock_ws_open.state = MagicMock()
        mock_ws_open.state.name = "OPEN"

        async def mock_ws_send(msg):
            in_loop_barrier.set()
            await resume_barrier.wait()

        mock_ws_open.send = AsyncMock(side_effect=mock_ws_send)

        old_session_state = {"authenticated": True, "session_key": b"OLD" * 10}
        daemon._current_session_state = old_session_state
        daemon._stream_generation = 1

        # Start Task 2 (gen 1)
        old_stream_coro = daemon.run_capture_stream(mock_ws_open, old_session_state["session_key"], old_session_state, stream_generation=1)
        old_task = asyncio.create_task(old_stream_coro)
        created_tasks.append(old_task)
        daemon._stream_task = old_task

        # Wait until old_task is actively executing INSIDE the streaming loop
        await in_loop_barrier.wait()
        assert daemon._streaming_active is True

        # Now, while old_task is suspended inside the loop, a new session arrives (gen 2)
        new_session_state = {"authenticated": True, "session_key": b"NEW" * 10}
        daemon._current_session_state = new_session_state
        daemon._streaming_active = True
        daemon._webrtc_active = True
        daemon._stream_generation = 2
        daemon._in_flight_fids = {701, 702}
        daemon._frame_send_times = {701: 7000.0, 702: 7005.0}

        dummy_new_task = asyncio.create_task(asyncio.sleep(10))
        created_tasks.append(dummy_new_task)
        daemon._stream_task = dummy_new_task

        # Now cancel the old task while it is suspended in the loop
        old_task.cancel()
        resume_barrier.set()
        try:
            await old_task
        except asyncio.CancelledError:
            pass

        # VERIFY that old_task's finally block did NOT corrupt the new session and did not raise UnboundLocalError:
        assert old_task.done() is True
        assert old_task.cancelled() is True
        assert len(released_calls) == 0 # Did not release inputs of the new session
        assert daemon._streaming_active is True # Preserved active flag
        assert daemon._webrtc_active is True # Preserved webrtc flag
        assert daemon._in_flight_fids == {701, 702} # In-flight fids untouched
        assert daemon._frame_send_times == {701: 7000.0, 702: 7005.0} # Timestamps untouched
        assert daemon._current_session_state is new_session_state # Session state untouched
        assert daemon._stream_task is dummy_new_task # Task reference untouched
        assert daemon._stream_generation == 2 # Generation untouched
        assert len(old_session_state) == 0 # Cleaned up only its own state

        # ----------------------------------------------------------------------
        # Case 2b: Superseded task exiting NORMALLY (generation mismatch in loop)
        # ----------------------------------------------------------------------
        in_loop_barrier_2 = asyncio.Event()
        resume_barrier_2 = asyncio.Event()

        mock_ws_open_2 = MagicMock()
        mock_ws_open_2.state = MagicMock()
        mock_ws_open_2.state.name = "OPEN"

        async def mock_ws_send_2(msg):
            in_loop_barrier_2.set()
            await resume_barrier_2.wait()

        mock_ws_open_2.send = AsyncMock(side_effect=mock_ws_send_2)

        gen2_session_state = {"authenticated": True, "session_key": b"GEN2" * 8}
        daemon._current_session_state = gen2_session_state
        daemon._stream_generation = 2

        task_gen2_coro = daemon.run_capture_stream(mock_ws_open_2, gen2_session_state["session_key"], gen2_session_state, stream_generation=2)
        task_gen2 = asyncio.create_task(task_gen2_coro)
        created_tasks.append(task_gen2)
        daemon._stream_task = task_gen2

        await in_loop_barrier_2.wait()

        # Supersede generation to 3 while task_gen2 is in loop
        gen3_session_state = {"authenticated": True, "session_key": b"GEN3" * 8}
        daemon._current_session_state = gen3_session_state
        daemon._stream_generation = 3
        daemon._in_flight_fids = {801}
        daemon._frame_send_times = {801: 8000.0}

        dummy_gen3_task = asyncio.create_task(asyncio.sleep(10))
        created_tasks.append(dummy_gen3_task)
        daemon._stream_task = dummy_gen3_task

        # Release barrier WITHOUT cancelling task_gen2; loop will break due to generation mismatch
        resume_barrier_2.set()
        await task_gen2

        # Verify normal exit on superseded task completed without UnboundLocalError
        assert task_gen2.done() is True
        assert task_gen2.exception() is None
        assert daemon._in_flight_fids == {801}
        assert daemon._frame_send_times == {801: 8000.0}
        assert daemon._stream_generation == 3
        assert daemon._stream_task is dummy_gen3_task

        # ----------------------------------------------------------------------
        # Case 3: Real in-memory InputWorker input release in handle_client_arrived
        # ----------------------------------------------------------------------
        released_calls.clear()

        # Enqueue discrete key down and mouse down events through real InputWorker
        real_worker.enqueue({"action": "key_down", "vk": 0x41}) # Key 'A'
        real_worker.enqueue({"action": "mouse_down", "button": "left"})
        assert real_worker.flush(timeout=2.0) is True

        # Verify that the keys/buttons are actively held in the handler
        assert 0x41 in real_handler._pressed_keys
        assert "left" in real_handler._pressed_mouse_buttons

        daemon._broker_token = "test_broker_token_123"
        daemon._control_saturated = True

        # Now simulate arrival of a new client session:
        arrived_ok = await daemon.handle_client_arrived(
            {"broker_token": "test_broker_token_123", "client_id": "client_99", "salt": "aabb"},
            {}
        )
        assert arrived_ok is True
        assert daemon._control_saturated is False # Latch reset!
        assert len(released_calls) == 1 # Verified inputs were released exactly once

        # Real InputWorker release verification: all held keys & buttons MUST be empty!
        assert len(real_handler._pressed_keys) == 0
        assert len(real_handler._pressed_mouse_buttons) == 0

        # ----------------------------------------------------------------------
        # Case 3b: Fail-closed: simulate failure during input release
        # ----------------------------------------------------------------------
        daemon._control_saturated = True
        daemon._safe_release_inputs = lambda timeout=2.0: False
        fail_session = {}
        arrived_rejected = await daemon.handle_client_arrived(
            {"broker_token": "test_broker_token_123", "client_id": "client_99", "salt": "ccdd"},
            fail_session
        )
        assert arrived_rejected is False
        assert daemon._control_saturated is True # Remains fail-closed!
        assert len(fail_session) == 0 # State wiped on rejection

    finally:
        try:
            if daemon is not None:
                try:
                    daemon.stop()
                except Exception:
                    pass
        finally:
            try:
                if real_worker is not None:
                    try:
                        real_worker.stop()
                    except Exception:
                        pass
            finally:
                for t in created_tasks:
                    if not t.done():
                        t.cancel()
                if created_tasks:
                    try:
                        await asyncio.gather(*created_tasks, return_exceptions=True)
                    except (asyncio.CancelledError, Exception):
                        pass


@pytest.mark.anyio
async def test_run_capture_stream_terminal_capture_error_notifies_client_and_teardown(tmp_path):
    """
    Verify in-memory regression for terminal streaming failure with genuine broker routing:
    1. Host connects to MockInMemoryBroker using connect_host.
    2. Client connects to MockInMemoryBroker using connect_client, handshakes, and authenticates.
    3. Host initiates streaming session with real InputWorker in memory.
    4. When capture raises a terminal capture exception, run_capture_stream sends ERROR('CAPTURE_FAILED')
       over the broker to the client and closes the host websocket connection.
    5. Client receives CAPTURE_FAILED, sets is_connected=False, dispatches on_disconnect_callback,
       and initiates deterministic disconnect() teardown.
    6. Host session tears down in finally, cancels active streaming, and safely releases held inputs via InputWorker.
    7. Client disconnect() completes cleanly and is verified idempotent against concurrent triggers without self-cancellation, deadlock, or reconnection attempts.
    8. All tasks, workers, daemon, and broker are cleanly gathered and terminated in finally with zero uncollected tasks.
    """
    import asyncio
    from src.input_handler import MockInputInjector, InputHandler
    from src.input_worker import InputWorker
    from src.capture import MockDesktopCapture
    from src.client import RemoteClient
    from src.signaling_broker import MockInMemoryBroker

    broker = None
    daemon = None
    client = None
    real_worker = None
    created_tasks = []

    try:
        # 1. Start In-Memory Broker (pure asyncio queues, zero OS TCP/UDP sockets)
        broker = MockInMemoryBroker()

        # 2. Setup Host with MockInputInjector, InputHandler, and real InputWorker in memory
        mock_injector = MockInputInjector()
        real_handler = InputHandler(injector=mock_injector)
        real_worker = InputWorker(input_handler=real_handler)
        real_worker.start()

        mock_cap = MockDesktopCapture(target_width=320, target_height=180, quality=50)
        daemon = HostDaemon(
            config_dir=str(tmp_path / "host_cfg"),
            headless=True,
            dpapi_engine=_test_dpapi(),
            input_handler=real_handler,
            capture=mock_cap,
            ws_connector=broker.connect_host,
            start_worker=False,
        )
        daemon.input_worker = real_worker
        dev_id = daemon.security.get_or_create_device_id()
        daemon.security.set_password("TerminalPass123!")

        host_task = asyncio.create_task(daemon.start())
        created_tasks.append(host_task)

        # Wait for host registration on in-memory broker
        clean_id = dev_id.replace(" ", "")
        for _ in range(50):
            if broker.get_host(clean_id) is not None:
                break
            await asyncio.sleep(0.02)
        assert broker.get_host(clean_id) is not None

        # 3. Setup Client connected to in-memory broker
        client = RemoteClient(
            ws_connector=broker.connect_client,
            config_dir=str(tmp_path / "client_cfg"),
            dpapi_engine=_test_dpapi(),
        )

        client_statuses = []
        client_errors = []
        disconnect_event = asyncio.Event()

        client.on_status_callback = lambda txt: client_statuses.append(txt)
        client.on_error_callback = lambda code, payload: client_errors.append((code, payload))
        client.on_disconnect_callback = lambda: disconnect_event.set()

        # Authenticate client
        connected = await client.connect(dev_id, "TerminalPass123!")
        assert connected is True
        assert client.state.is_connected is True

        # Wait for streaming to activate on host
        for _ in range(50):
            if daemon._streaming_active and daemon._stream_task is not None:
                break
            await asyncio.sleep(0.02)
        assert daemon._streaming_active is True

        # Pre-press a key through real InputWorker to verify input release on teardown
        real_worker.enqueue({"action": "key_down", "vk": 0x11})  # CTRL
        assert real_worker.flush(timeout=2.0) is True
        assert 0x11 in real_handler._pressed_keys

        # 4. Inject terminal capture error
        def faulty_capture_frame(*args, **kwargs):
            raise RuntimeError("DXGI surface lost / terminal capture fault")

        mock_cap.capture_frame = faulty_capture_frame

        # 5. Wait for terminal failure propagation through broker to client
        for _ in range(60):
            if not client.state.is_connected and not daemon._streaming_active:
                break
            await asyncio.sleep(0.05)

        # 6. Verify client received CAPTURE_FAILED and transitioned to disconnected
        assert client.state.is_connected is False
        assert any(code == "CAPTURE_FAILED" for code, _ in client_errors) or any(
            "CAPTURE_FAILED" in str(s) or "Sessione interrotta" in str(s) for s in client_statuses
        )
        assert disconnect_event.is_set() is True

        # 7. Verify host tore down streaming session and safely released inputs
        assert daemon._streaming_active is False
        assert daemon._stream_task is None
        assert len(daemon._in_flight_fids) == 0
        assert len(daemon._frame_send_times) == 0
        assert len(real_handler._pressed_keys) == 0
        assert len(real_handler._pressed_mouse_buttons) == 0

        # 8. Verify client disconnect() is safe and idempotent against concurrent triggers
        # (e.g. concurrent GUI window close while receiver teardown already happened)
        disc_task_1 = asyncio.create_task(client.disconnect())
        disc_task_2 = asyncio.create_task(client.disconnect())
        created_tasks.extend([disc_task_1, disc_task_2])
        await asyncio.gather(disc_task_1, disc_task_2)

        assert client.websocket is None
        assert client.state.is_connected is False
        assert len(client._aux_tasks) == 0

    finally:
        try:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        finally:
            try:
                if daemon is not None:
                    try:
                        daemon.stop()
                    except Exception:
                        pass
            finally:
                try:
                    if real_worker is not None:
                        try:
                            real_worker.stop()
                        except Exception:
                            pass
                finally:
                    try:
                        if broker is not None:
                            try:
                                await broker.stop()
                            except Exception:
                                pass
                    finally:
                        for t in created_tasks:
                            if not t.done():
                                t.cancel()
                        if created_tasks:
                            try:
                                await asyncio.gather(*created_tasks, return_exceptions=True)
                            except (asyncio.CancelledError, Exception):
                                pass


def test_clock_offset_estimator_robustness():
    """Verify ClockOffsetEstimator handles None and valid inputs without raising NameError or TypeError."""
    from src.native_engine import ClockOffsetEstimator
    est = ClockOffsetEstimator()
    # Initial state
    assert est.rtt_ms is None
    assert est.offset_ms is None
    assert est.jitter_ms is None

    # Update with None values must return cleanly without raising
    est.update(None, 100.0, 100.0, 110.0)
    assert est.rtt_ms is None

    # Update with valid timestamps in ms
    est.update(t1_ms=1000.0, t2_ms=2010.0, t3_ms=2020.0, t4_ms=1035.0)
    assert est.rtt_ms == 25.0
    assert est.offset_ms == 997.5

    # to_host_time / to_client_time with None
    assert est.to_host_time(None) is None
    assert est.to_client_time(None) is None
    assert est.to_host_time(500.0) == 500.0 + 997.5
