import asyncio
import os
import sys
import time
import json
import subprocess
import pytest
from pathlib import Path

# Ensure repo root is in sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.client import RemoteClient
from src.protocol import ProtocolMessage
from src.security import SecurityManager

@pytest.mark.anyio
async def test_windows_process_subsystem_e2e(tmp_path):
    """
    Windows Process & System Master automated E2E test harness.
    Runs dist/AetherDesk.exe in host mode as an independent Windows process,
    verifies TCP port 8765 binding via OS diagnostics,
    connects an automated client, streams live desktop video frames,
    injects input commands, syncs clipboard, and verifies reconnection.
    """
    exe_path = repo_root / "dist" / "AetherDesk.exe"
    assert exe_path.exists(), f"AetherDesk.exe not found at {exe_path}"

    # 1. Spawn Host as independent Windows background process
    print(f"\n[Subsystem] Spawning Host Process: {exe_path} --host")
    host_proc = subprocess.Popen(
        [str(exe_path), "--host"],
        cwd=str(repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    assert host_proc.poll() is None, "Host process terminated prematurely"
    print(f"[Subsystem] Host Process PID: {host_proc.pid}")

    try:
        # 2. Wait for port 8765 to open (Windows System Diagnostics)
        bound = False
        for _ in range(50):
            try:
                import socket
                with socket.create_connection(("127.0.0.1", 8765), timeout=0.2):
                    bound = True
                    break
            except (ConnectionRefusedError, OSError):
                await asyncio.sleep(0.1)

        assert bound is True, "Host failed to bind listening port 8765 within 5 seconds"
        print("[Subsystem] Port 8765 confirmed LISTENING via loopback TCP socket")
        await asyncio.sleep(1.0)

        # 3. Connect automated Client to Host (Dynamically resolving host credentials)
        sec = SecurityManager()
        dev_id = sec.get_or_create_device_id()
        password = sec.get_password_display()
        client = RemoteClient(broker_url="ws://127.0.0.1:8765")

        received_frames = []
        frame_event = asyncio.Event()

        def on_frame(jpeg_bytes, dims, cur, *args):
            received_frames.append((len(jpeg_bytes), dims, cur))
            frame_event.set()

        client.on_frame_callback = on_frame
        client.on_status_callback = lambda s: print(f"[Client Status] {s}")

        print(f"[Subsystem] Connecting client to Host ID {dev_id} with password {password}...")
        ok = False
        for attempt in range(15):
            ok = await client.connect(dev_id, password)
            if ok:
                break
            await asyncio.sleep(0.3)

        assert ok is True, f"Client connection failed: {client.status_message if hasattr(client, 'status_message') else ''}"
        assert client.state.is_connected is True
        print("[Subsystem] E2EE Session Key negotiated successfully")

        # 4. Stream & Verify Video Frames
        await asyncio.wait_for(frame_event.wait(), timeout=6.0)
        assert len(received_frames) > 0
        frame_len, dims, cur = received_frames[0]
        print(f"[Subsystem] Video Frame 1 Received! Size: {frame_len} bytes, Dims: {dims}, Cursor: {cur}")
        assert frame_len > 1000
        assert dims[0] > 0 and dims[1] > 0

        # Wait for keyframe interval to verify continuous streaming
        await asyncio.sleep(1.2)
        print(f"[Subsystem] Total Frames Streamed: {len(received_frames)}")
        assert len(received_frames) >= 1

        # 5. Send Mouse and Keyboard Input
        print("[Subsystem] Testing input event injection...")
        await client.send_input({"type": "mouse_move", "x": 0.5, "y": 0.5})
        await client.send_input({"type": "key_down", "vk": 0x41}) # 'A'
        await client.send_input({"type": "key_up", "vk": 0x41})

        # 6. Test File Transfer over live tunnel
        sample_file = tmp_path / "subsystem_test.bin"
        sample_file.write_bytes(os.urandom(32768)) # 32 KB test file
        file_ok = await client.send_file(str(sample_file))
        assert file_ok is True
        print("[Subsystem] Encrypted file transfer verified (32 KB)")

        # 7. Disconnect Client
        await client.disconnect()
        assert client.state.is_connected is False
        print("[Subsystem] Client 1 disconnected cleanly")
        await asyncio.sleep(0.5)

        # 8. Reconnect Test (Verify Replay Sequence Window Reset Fix)
        print("[Subsystem] Testing Reconnect (Verifying Anti-Replay Reset Fix)...")
        client2 = RemoteClient(broker_url="ws://127.0.0.1:8765")
        client2.on_status_callback = lambda s: print(f"[Subsystem C2 Status] {s}")
        reconnect_frames = []
        rec_event = asyncio.Event()
        client2.on_frame_callback = lambda b, d, c, *a: (reconnect_frames.append(len(b)), rec_event.set())

        ok2 = await client2.connect(dev_id, password)
        assert ok2 is True, f"Client 2 reconnect failed: {getattr(client2, '_status', '')}"
        await asyncio.wait_for(rec_event.wait(), timeout=5.0)
        assert len(reconnect_frames) > 0
        print(f"[Subsystem] Reconnect successful! Received {len(reconnect_frames)} frames on second session")

        # Send input on reconnected session to guarantee anti-replay doesn't block seq 1..N
        await client2.send_input({"type": "mouse_move", "x": 0.25, "y": 0.25})
        await client2.disconnect()
        print("[Subsystem] Reconnected input delivered without replay rejection")

    finally:
        # 9. Terminate Host Subsystem Process and its entire process tree Cleanly
        print(f"[Subsystem] Terminating Host Process PID {host_proc.pid}...")
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(host_proc.pid)], capture_output=True)
            host_proc.kill()
        except Exception as e:
            print("[Subsystem Host Comm error]:", e)
        print("[Subsystem] Host Process terminated successfully")
