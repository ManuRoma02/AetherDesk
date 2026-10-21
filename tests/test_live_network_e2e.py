import pytest
import asyncio
import secrets
from pathlib import Path

from src.signaling_broker import SignalingBroker
from src.host_daemon import HostDaemon
from src.client import RemoteClient

@pytest.mark.anyio
async def test_live_network_e2e_broker_host_client(tmp_path):
    """Deterministic end-to-end test verifying live WebSocket communication:
    Broker -> Host Registration -> Client Handshake -> Video Frame -> Input -> File Transfer.
    Zero hard sleeps; condition-based waiting for maximum test reliability.
    """
    # 1. Start Broker on dynamic local port
    broker = SignalingBroker(host="127.0.0.1", port=0)
    await broker.start()
    server_port = broker.server.sockets[0].getsockname()[1]
    broker_url = f"ws://127.0.0.1:{server_port}"

    # 2. Setup and start Host Daemon
    host_cfg = tmp_path / "host_cfg"
    daemon = HostDaemon(config_dir=str(host_cfg), headless=True, broker_url=broker_url)
    dev_id = daemon.security.get_or_create_device_id()
    daemon.security.set_password("MySecureE2EPass123!")

    # Start host daemon listener task
    host_task = asyncio.create_task(daemon.start())

    # Wait for host registration on broker (condition-based wait, max 3s)
    clean_id = dev_id.replace(" ", "")
    for _ in range(30):
        if broker.get_host(clean_id) is not None:
            break
        await asyncio.sleep(0.05)
    assert broker.get_host(clean_id) is not None

    # 3. Setup and connect Remote Client
    client = RemoteClient(broker_url=broker_url)
    frame_received = asyncio.Event()
    received_frames = []

    def on_frame(jpeg_bytes, dims, cur):
        received_frames.append(jpeg_bytes)
        frame_received.set()

    client.on_frame_callback = on_frame

    # Connect client with correct password
    connected = await client.connect(dev_id, "MySecureE2EPass123!")
    assert connected is True
    assert client.state.is_connected is True
    assert client.state.session_key is not None

    # 4. Wait for first video frame (condition-based)
    try:
        await asyncio.wait_for(frame_received.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        pytest.fail("Timeout waiting for remote video frame over live broker connection")
    assert len(received_frames) > 0
    assert len(received_frames[0]) > 0

    # 5. Send input over live connection
    await client.send_input({"type": "mouse_move", "x": 0.5, "y": 0.5})

    # 6. Send file over live connection
    test_file = tmp_path / "test_doc.txt"
    test_file.write_text("Secret content delivered over encrypted tunnel 2026", encoding="utf-8")
    send_ok = await client.send_file(str(test_file))
    assert send_ok is True

    # 7. Clean teardown
    await client.disconnect()
    daemon.stop()
    await broker.stop()
    host_task.cancel()
    try:
        await host_task
    except asyncio.CancelledError:
        pass
