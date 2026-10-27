import pytest
from src.client import ClientState

def test_client_id_validation():
    assert ClientState.validate_id("123 456 789") is True
    assert ClientState.validate_id("123456789") is True
    assert ClientState.validate_id("12 34 56 789") is True
    assert ClientState.validate_id("abc") is False
    assert ClientState.validate_id("12345678") is False # 8 digits
    assert ClientState.validate_id("1234567890") is False # 10 digits
    # Direct IP and Host support
    assert ClientState.validate_id("192.168.1.100") is True
    assert ClientState.validate_id("192.168.1.100:8765") is True
    assert ClientState.validate_id("localhost") is True
    assert ClientState.validate_id("127.0.0.1") is True

def test_client_id_cleaning():
    assert ClientState.clean_id(" 482 910 234 ") == "482910234"

def test_device_id_combobox_extraction():
    sample = "315 747 031 (Ufficio)"
    extracted = sample.split("(")[0].strip()
    assert ClientState.validate_id(extracted) is True
    assert ClientState.clean_id(extracted) == "315747031"

def test_client_connection_handshake_state():
    state = ClientState()
    assert state.is_connected is False
    state.set_connecting(True)
    assert state.is_connecting is True
    state.set_connected(True, b"test_session_key")
    assert state.is_connected is True
    assert state.session_key == b"test_session_key"

def test_lan_discovery_beacon_and_peer_resolution():
    from src.discovery import LANBeaconListener, LANBeaconBroadcaster
    listener = LANBeaconListener()
    broadcaster = LANBeaconBroadcaster(device_id="888 777 666", port=8765, ip="192.168.1.100", hostname="Mock-Desktop")

    # In-memory test: verify payload generation and peer discovery with fixed mock values (zero sockets, zero DNS queries)
    payload = broadcaster.build_beacon_payload(ip="192.168.1.100", hostname="Mock-Desktop", timestamp=1700000000.0)
    assert isinstance(payload, bytes)
    assert len(payload) > 0

    # Feed packet directly into listener without socket
    ok = listener.process_beacon_data(payload, sender_ip="192.168.1.100")
    assert ok is True

    # Verify peer discovery resolution
    url = listener.get_peer_broker_url("888777666")
    assert url == "ws://192.168.1.100:8765"

    # Verify discovered peers list
    peers = listener.get_discovered_peers()
    assert len(peers) == 1
    assert peers[0]["device_id"] == "888777666"
    assert peers[0]["ip"] == "192.168.1.100"
    assert peers[0]["name"] == "Mock-Desktop"


@pytest.mark.anyio
async def test_remote_client_headless_client_disconnected_handling(tmp_path):
    """
    Verify headless RemoteClient lifecycle when remote host disconnects:
    1. RemoteClient receives CLIENT_DISCONNECTED from broker inside its receive loop.
    2. Without any GUI attached, RemoteClient automatically schedules full disconnect() cleanup.
    3. State is set to disconnected, auto_reconnect is disabled, WebSocket is closed,
       auxiliary tasks are cancelled/gathered, and no dangling tasks remain.
    4. Reconnection is never attempted.
    """
    import asyncio
    from src.client import RemoteClient
    from src.protocol import ProtocolMessage
    from src.signaling_broker import InMemoryWebSocket
    from src.security import MockTestDPAPIEngine

    client = None
    created_tasks = []
    try:
        q_srv_to_cli = asyncio.Queue()
        q_cli_to_srv = asyncio.Queue()
        client_ws = InMemoryWebSocket(incoming=q_srv_to_cli, outgoing=q_cli_to_srv)

        client = RemoteClient(
            config_dir=str(tmp_path / "cli_cfg"),
            dpapi_engine=MockTestDPAPIEngine(),
        )
        client.websocket = client_ws
        client.state.set_connected(True, session_key=b"k" * 32)
        client.state.target_id = "test_target"
        client.last_dev_id = "test_target"
        client.auto_reconnect = True

        status_updates = []
        client.on_status_callback = lambda txt: status_updates.append(txt)

        # Start receive loop directly as a tracked task
        receive_task = asyncio.create_task(client._receive_loop())
        created_tasks.append(receive_task)
        client._receive_task = receive_task

        # Send CLIENT_DISCONNECTED into client queue
        disc_msg = ProtocolMessage.serialize("CLIENT_DISCONNECTED", {})
        await q_srv_to_cli.put(disc_msg)

        # Wait for receive loop and disconnect to complete
        for _ in range(50):
            if not client.state.is_connected and client.websocket is None:
                break
            await asyncio.sleep(0.02)

        # Verify deterministic cleanup without GUI
        assert client.state.is_connected is False
        assert client.auto_reconnect is False
        assert client._is_manual_disconnect is True
        assert client.websocket is None
        assert client._reconnect_task is None
        assert len(client._aux_tasks) == 0
        assert any("disconnesso" in s.lower() for s in status_updates)

        # Verify idempotency of subsequent disconnect
        await client.disconnect()
        assert client.websocket is None

    finally:
        try:
            if client is not None:
                try:
                    await client.disconnect()
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
async def test_remote_client_concurrent_disconnect_from_gui_and_receive_loop(tmp_path):
    """
    Verify deterministic in-memory race freedom and deadlock immunity when GUI calls
    disconnect() concurrently with receive_loop processing an inbound CLIENT_DISCONNECTED event.
    """
    import asyncio
    from src.client import RemoteClient
    from src.protocol import ProtocolMessage
    from src.signaling_broker import InMemoryWebSocket
    from src.security import MockTestDPAPIEngine

    client = None
    created_tasks = []
    try:
        q_srv_to_cli = asyncio.Queue()
        q_cli_to_srv = asyncio.Queue()
        client_ws = InMemoryWebSocket(incoming=q_srv_to_cli, outgoing=q_cli_to_srv)

        client = RemoteClient(
            config_dir=str(tmp_path / "cli_cfg"),
            dpapi_engine=MockTestDPAPIEngine(),
        )
        client.websocket = client_ws
        client.state.set_connected(True, session_key=b"k" * 32)
        client.state.target_id = "test_target"

        disconnect_dispatched = asyncio.Event()

        def on_disconnect():
            disconnect_dispatched.set()

        client.on_disconnect_callback = on_disconnect

        receive_task = asyncio.create_task(client._receive_loop())
        created_tasks.append(receive_task)
        client._receive_task = receive_task

        # Simulate concurrent GUI window close triggered right when disconnect callback fires
        async def gui_close():
            await asyncio.wait_for(disconnect_dispatched.wait(), timeout=5.0)
            await client.disconnect()

        gui_task = asyncio.create_task(gui_close())
        created_tasks.append(gui_task)

        # Deliver inbound CLIENT_DISCONNECTED to the receive loop via in-memory queue
        disc_msg = ProtocolMessage.serialize("CLIENT_DISCONNECTED", {"target_id": "test_target"})
        await q_srv_to_cli.put(disc_msg)

        # Concurrently await both receive_task and gui_task without deadlock
        results = await asyncio.gather(receive_task, gui_task, return_exceptions=True)
        for res in results:
            assert not isinstance(res, Exception) or isinstance(res, asyncio.CancelledError)

        # If receive loop scheduled an aux disconnect task, wait for it as well
        if client._aux_tasks:
            aux_tasks = list(client._aux_tasks)
            created_tasks.extend(aux_tasks)
            await asyncio.gather(*aux_tasks, return_exceptions=True)

        assert client.websocket is None
        assert client.state.is_connected is False
        assert len(client._aux_tasks) == 0
        assert receive_task.done()

    finally:
        try:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        finally:
            if client is not None and client._aux_tasks:
                for t in list(client._aux_tasks):
                    if not t.done():
                        t.cancel()
                created_tasks.extend(list(client._aux_tasks))
            for t in created_tasks:
                if not t.done():
                    t.cancel()
            if created_tasks:
                try:
                    await asyncio.gather(*created_tasks, return_exceptions=True)
                except (asyncio.CancelledError, Exception):
                    pass


@pytest.mark.anyio
async def test_remote_client_receive_loop_sibling_task_cleanup_on_error_and_reconnect_handoff(tmp_path):
    """
    Verify in-memory regression for _receive_loop() child task lifecycle:
    1. _ws_receiver and _webrtc_pump start concurrently inside _receive_loop.
    2. _webrtc_pump is actively running its loop while _ws_receiver encounters an error (ConnectionClosed).
    3. _receive_loop terminates and gathers the sibling _webrtc_pump task before exiting.
    4. Sibling loop task is explicitly proven cancelled and done without sleep-based heuristics.
    5. Reconnection task takes over lifecycle ownership without premature disconnect teardown.
    6. All reconnections use in-memory WebSockets exclusively; websockets.connect is guarded fail-fast.
    7. Teardown via client.disconnect() terminates the reconnection cleanly with zero dangling tasks.
    """
    import asyncio
    from typing import Optional
    import websockets
    from src.client import RemoteClient
    from src.signaling_broker import InMemoryWebSocket
    from src.security import MockTestDPAPIEngine

    client = None
    created_tasks = []
    created_inmemory_sockets = []
    orig_ws_connect = getattr(websockets, "connect", None)

    def forbidden_os_connect(*args, **kwargs):
        raise AssertionError("Security violation: websockets.connect was invoked in a purely in-memory test!")

    def inmemory_ws_connector():
        q_s2c = asyncio.Queue()
        q_c2s = asyncio.Queue()
        new_ws = InMemoryWebSocket(incoming=q_s2c, outgoing=q_c2s)
        created_inmemory_sockets.append(new_ws)
        return new_ws

    try:
        # 1. Guard against any accidental OS socket opening
        websockets.connect = forbidden_os_connect

        # 2. Setup initial in-memory websocket and queues
        client_ws = inmemory_ws_connector()
        q_srv_to_cli = client_ws._in_q

        # 3. Create client with in-memory ws_connector injected
        client = RemoteClient(
            config_dir=str(tmp_path / "cli_cfg"),
            dpapi_engine=MockTestDPAPIEngine(),
            ws_connector=inmemory_ws_connector,
        )
        client.websocket = client_ws
        client.state.set_connected(True, session_key=b"k" * 32)
        client.state.target_id = "test_target"
        client.last_dev_id = "test_target"
        client.last_password = "test_password"
        client.auto_reconnect = True

        # 4. Instrument _webrtc_pump to capture its exact Task reference and signal active execution
        pump_task_ref: Optional[asyncio.Task] = None
        pump_first_iteration = asyncio.Event()

        def mock_poll_ice():
            nonlocal pump_task_ref
            if pump_task_ref is None:
                pump_task_ref = asyncio.current_task()
                pump_first_iteration.set()
            return ""

        client.native_engine.is_native_loaded = True
        client.native_engine.client_handle = 12345
        client.native_engine.poll_client_ice_candidate = mock_poll_ice

        # Start receive loop as a tracked task
        receive_task = asyncio.create_task(client._receive_loop())
        created_tasks.append(receive_task)
        client._receive_task = receive_task

        # 5. Wait until _webrtc_pump has actively started running and captured pump_task_ref
        await asyncio.wait_for(pump_first_iteration.wait(), timeout=5.0)
        assert pump_task_ref is not None
        assert not pump_task_ref.done()

        # 6. Inject unexpected ConnectionClosed error into _ws_receiver while pump is running
        await q_srv_to_cli.put(None)

        # 7. Wait for _receive_loop to terminate cleanly
        await asyncio.wait_for(receive_task, timeout=5.0)
        assert receive_task.done() is True

        # 8. Sibling task termination check (deterministic, explicit, without sleep heuristics):
        # pump_task_ref MUST be cancelled and done before receive_task completes!
        assert pump_task_ref.done() is True
        assert pump_task_ref.cancelled() is True

        # 9. Verify reconnection task took over lifecycle ownership
        for _ in range(50):
            if client.state.is_reconnecting and client._reconnect_task is not None:
                break
            await asyncio.sleep(0.01)

        assert client.state.is_reconnecting is True
        assert client._reconnect_task is not None
        assert not client._reconnect_task.done()
        created_tasks.append(client._reconnect_task)
        assert len(client._aux_tasks) == 0  # No premature teardown scheduled into _aux_tasks

        # 10. Verify manual disconnect cancels reconnection and tears down cleanly
        await client.disconnect()
        assert client._reconnect_task is None
        assert client.state.is_reconnecting is False
        assert client.state.is_connected is False
        assert client.websocket is None
        assert len(client._aux_tasks) == 0

    finally:
        # Restore websockets.connect
        if orig_ws_connect is not None:
            websockets.connect = orig_ws_connect

        try:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        finally:
            # Close all in-memory WebSockets created by connector
            for ws in created_inmemory_sockets:
                try:
                    await ws.close()
                except Exception:
                    pass

            if client is not None and client._reconnect_task is not None:
                if not client._reconnect_task.done():
                    client._reconnect_task.cancel()
                created_tasks.append(client._reconnect_task)

            if client is not None and client._aux_tasks:
                for t in list(client._aux_tasks):
                    if not t.done():
                        t.cancel()
                created_tasks.extend(list(client._aux_tasks))

            for t in created_tasks:
                if not t.done():
                    t.cancel()

            if created_tasks:
                try:
                    await asyncio.gather(*created_tasks, return_exceptions=True)
                except (asyncio.CancelledError, Exception):
                    pass
