import pytest
import asyncio
import secrets
import platform
from pathlib import Path

from src.signaling_broker import MockInMemoryBroker
from src.host_daemon import HostDaemon
from src.client import RemoteClient
from src.security import MockTestDPAPIEngine
from src.input_handler import InputHandler, MockInputInjector
from src.capture import MockDesktopCapture

def _test_dpapi():
    return MockTestDPAPIEngine() if platform.system() != "Windows" else None

@pytest.mark.anyio
async def test_live_network_e2e_broker_host_client(tmp_path):
    """Deterministic 100% in-memory E2E test verifying full protocol communication:
    In-Memory Broker -> Host Registration -> Client Handshake -> Video Frame -> Input -> File Transfer.
    Zero OS TCP/UDP sockets opened; transport operates purely via in-memory asyncio queues.
    Uses simulated capture and mock input injector (zero OS desktop/input interactions).
    """
    broker = None
    daemon = None
    host_task = None
    client = None

    try:
        # 1. Start In-Memory Broker (zero OS sockets, pure asyncio queues)
        broker = MockInMemoryBroker()

        # 2. Setup and start Host Daemon with mock injector, mock capture, and in-memory broker connection
        host_cfg = tmp_path / "host_cfg"
        mock_inj = MockInputInjector()
        mock_cap = MockDesktopCapture(target_width=320, target_height=180, quality=50)
        daemon = HostDaemon(
            config_dir=str(host_cfg),
            headless=True,
            dpapi_engine=_test_dpapi(),
            input_handler=InputHandler(injector=mock_inj),
            capture=mock_cap,
            ws_connector=broker.connect_host,
        )
        dev_id = daemon.security.get_or_create_device_id()
        daemon.security.set_password("MySecureE2EPass123!")

        # Start host daemon listener task
        host_task = asyncio.create_task(daemon.start())

        # Wait for host registration on broker in-memory (condition-based wait)
        clean_id = dev_id.replace(" ", "")
        for _ in range(30):
            if broker.get_host(clean_id) is not None:
                break
            await asyncio.sleep(0.02)
        assert broker.get_host(clean_id) is not None

        # 3. Setup and connect Remote Client with in-memory transport
        client = RemoteClient(
            ws_connector=broker.connect_client,
            config_dir=str(tmp_path / "client_cfg"),
            dpapi_engine=_test_dpapi(),
        )

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
            pytest.fail("Timeout waiting for remote video frame over in-memory broker connection")
        assert len(received_frames) > 0
        assert len(received_frames[0]) > 0

        # 5. Send input over in-memory transport
        await client.send_input({"type": "mouse_move", "x": 0.5, "y": 0.5})
        for _ in range(20):
            if daemon.input_worker.queue.qsize() > 0 or len(mock_inj.sent_inputs) > 0:
                break
            await asyncio.sleep(0.02)
        daemon.input_worker.flush(timeout=1.0)
        assert len(mock_inj.sent_inputs) > 0

        # 6. Send file over in-memory transport
        test_file = tmp_path / "test_doc.txt"
        test_file.write_text("Secret content delivered over encrypted tunnel 2026", encoding="utf-8")
        send_ok = await client.send_file(str(test_file))
        assert send_ok is True
    finally:
        # 7. Guaranteed Clean teardown across all initialized resources
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        if daemon:
            try:
                daemon.stop()
            except Exception:
                pass
        if broker:
            try:
                await broker.stop()
            except Exception:
                pass
        if host_task:
            if not host_task.done():
                host_task.cancel()
            try:
                await host_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
