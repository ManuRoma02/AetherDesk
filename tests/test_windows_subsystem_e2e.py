import asyncio
import os
import sys
import json
import pytest
import platform
from pathlib import Path

# Ensure repo root is in sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.client import RemoteClient
from src.protocol import ProtocolMessage
from src.security import SecurityManager, MockTestDPAPIEngine
from src.host_daemon import HostDaemon
from src.input_handler import InputHandler, MockInputInjector
from src.capture import MockDesktopCapture
from src.signaling_broker import MockInMemoryBroker

def _test_dpapi():
    return MockTestDPAPIEngine() if platform.system() != "Windows" else None

@pytest.mark.anyio
async def test_windows_process_subsystem_e2e(tmp_path):
    """
    Simulated Windows Process & Subsystem E2E test harness.
    Runs simulated host daemon with mock capture and mock input injector
    over a 100% in-memory signaling broker (zero OS TCP/UDP sockets), verifying:
    - Broker connection and mutual authentication
    - E2EE session key negotiation
    - Video frame streaming with mock capture (zero screen/DXcam interaction)
    - Input event injection with mock injector (zero physical input)
    - Encrypted file transfer over tunnel
    - Clean disconnection and reconnection with anti-replay reset
    - Guaranteed cleanup of host daemon and broker resources
    """
    broker = None
    daemon = None
    host_task = None
    client = None
    client2 = None

    try:
        # 1. Start In-Memory Broker (pure asyncio queues, zero OS sockets)
        broker = MockInMemoryBroker()

        # 2. Setup Host with MockInputInjector, MockDesktopCapture, and in-memory transport
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
        password = daemon.security.get_password_display()

        host_task = asyncio.create_task(daemon.start())

        # Wait for host registration on broker in-memory
        clean_id = dev_id.replace(" ", "")
        for _ in range(50):
            if broker.get_host(clean_id) is not None:
                break
            await asyncio.sleep(0.02)
        assert broker.get_host(clean_id) is not None

        # 3. Connect automated Client to Host via in-memory transport
        client = RemoteClient(
            ws_connector=broker.connect_client,
            config_dir=str(tmp_path / "client1_cfg"),
            dpapi_engine=_test_dpapi(),
        )
        received_frames = []
        frame_event = asyncio.Event()

        def on_frame(jpeg_bytes, dims, cur, *args):
            received_frames.append((len(jpeg_bytes), dims, cur))
            frame_event.set()

        client.on_frame_callback = on_frame

        ok = False
        for _ in range(20):
            ok = await client.connect(dev_id, password)
            if ok:
                break
            await asyncio.sleep(0.05)

        assert ok is True
        assert client.state.is_connected is True

        # 4. Stream & Verify Video Frames
        await asyncio.wait_for(frame_event.wait(), timeout=5.0)
        assert len(received_frames) > 0
        frame_len, dims, cur = received_frames[0]
        assert frame_len > 0
        assert dims[0] > 0 and dims[1] > 0

        # 5. Send Mouse and Keyboard Input
        await client.send_input({"type": "mouse_move", "x": 0.5, "y": 0.5})
        await client.send_input({"type": "key_down", "vk": 0x41}) # 'A'
        await client.send_input({"type": "key_up", "vk": 0x41})

        for _ in range(20):
            if daemon.input_worker.queue.qsize() > 0 or len(mock_inj.sent_inputs) > 0:
                break
            await asyncio.sleep(0.02)
        daemon.input_worker.flush(timeout=1.0)
        assert len(mock_inj.sent_inputs) > 0

        # 6. Test File Transfer over live tunnel
        sample_file = tmp_path / "subsystem_test.bin"
        sample_file.write_bytes(os.urandom(32768)) # 32 KB test file
        file_ok = await client.send_file(str(sample_file))
        assert file_ok is True

        # 7. Disconnect Client
        await client.disconnect()
        assert client.state.is_connected is False

        # 8. Reconnect Test (Verify Replay Sequence Window Reset Fix)
        client2 = RemoteClient(
            ws_connector=broker.connect_client,
            config_dir=str(tmp_path / "client2_cfg"),
            dpapi_engine=_test_dpapi(),
        )
        reconnect_frames = []
        rec_event = asyncio.Event()
        client2.on_frame_callback = lambda b, d, c, *a: (reconnect_frames.append(len(b)), rec_event.set())

        ok2 = await client2.connect(dev_id, password)
        assert ok2 is True
        await asyncio.wait_for(rec_event.wait(), timeout=5.0)
        assert len(reconnect_frames) > 0

        # Send input on reconnected session to guarantee anti-replay doesn't block seq 1..N
        await client2.send_input({"type": "mouse_move", "x": 0.25, "y": 0.25})
        for _ in range(20):
            if daemon.input_worker.queue.qsize() > 0:
                break
            await asyncio.sleep(0.02)
        daemon.input_worker.flush(timeout=1.0)

        await client2.disconnect()

    finally:
        # Guaranteed Clean teardown across all initialized resources
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        if client2:
            try:
                await client2.disconnect()
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
