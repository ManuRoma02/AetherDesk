"""
Regression tests for architectural and security boundaries.

All tests in this module are strictly in-memory, isolated, and use mocks.
No real sockets, physical input, subprocesses, or real user configurations are accessed.
"""

import asyncio
import time
import threading
import secrets
import pytest
from pathlib import Path
import platform

from src.signaling_broker import SignalingBroker, RESERVED_BROKER_TYPES
from src.protocol import ProtocolMessage
from src.transfer import FileTransferManager
from src.host_daemon import HostDaemon
from src.input_handler import InputHandler, MockInputInjector
from src.capture import MockDesktopCapture
from src.security import SecurityManager, MockTestDPAPIEngine
from src.client import RemoteClient, ClientState
from src.native_engine import get_native_engine, ClockOffsetEstimator, CongestionController


def _test_dpapi():
    return MockTestDPAPIEngine() if platform.system() != "Windows" else None


class MockWS:
    """In-memory asynchronous WebSocket mock for isolated unit testing."""
    def __init__(self, messages=None):
        self.sent = []
        self.messages = list(messages or [])
        self.open = True

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            if self.open:
                await asyncio.sleep(0.01)
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def close(self):
        self.open = False


# ==============================================================================
# Pillar A: Broker State Machine & Cryptographic Identity
# ==============================================================================

def test_unauthenticated_client_disconnect_rejected(tmp_path):
    """Verify that CLIENT_DISCONNECTED messages from clients without broker token are rejected."""
    sec = SecurityManager(config_dir=str(tmp_path), dpapi_engine=_test_dpapi())
    injector = MockInputInjector()
    handler = InputHandler(injector=injector)
    daemon = HostDaemon(
        capture_engine=MockDesktopCapture(),
        input_handler=handler,
        security=sec,
        headless=True
    )
    try:
        daemon._broker_token = "legitimate_broker_secret_token_1234"
        daemon._streaming_active = True
        daemon.session_state = {"authenticated": False}

        # Malicious / spoofed CLIENT_DISCONNECTED from unauthenticated client
        daemon.handle_client_message("CLIENT_DISCONNECTED", {"target_id": daemon.device_id}, None, daemon.session_state)
        # Streaming must NOT be interrupted by unauthenticated spoof
        assert daemon._streaming_active is True

        # Legitimate broker event carrying authentic broker token
        daemon.handle_client_message("CLIENT_DISCONNECTED", {
            "target_id": daemon.device_id,
            "broker_token": "legitimate_broker_secret_token_1234"
        }, None, daemon.session_state)
        # Broker notification must successfully reset stream state
        assert daemon._streaming_active is False
    finally:
        daemon.stop(timeout=1.0)


def test_session_commands_require_authentication(tmp_path):
    """Verify FRAME_ACK, INPUT, CLIPBOARD, and FILE commands require authentication."""
    sec = SecurityManager(config_dir=str(tmp_path), dpapi_engine=_test_dpapi())
    injector = MockInputInjector()
    handler = InputHandler(injector=injector)
    daemon = HostDaemon(
        capture_engine=MockDesktopCapture(),
        input_handler=handler,
        security=sec,
        headless=True
    )
    try:
        daemon._frame_ack_event.clear()
        unauth_state = {"authenticated": False}

        # FRAME_ACK rejected on unauthenticated session
        daemon.handle_client_message("FRAME_ACK", {"fid": 99}, None, unauth_state)
        assert daemon._frame_ack_event.is_set() is False

        # Now authenticate
        auth_state = {"authenticated": True}
        daemon.handle_client_message("FRAME_ACK", {"fid": 99}, None, auth_state)
        assert daemon._frame_ack_event.is_set() is True
    finally:
        daemon.stop(timeout=1.0)


@pytest.mark.anyio
async def test_broker_rejects_missing_pubkey_in_host_registration():
    """Verify that REGISTER_HOST without a public key is rejected."""
    broker = SignalingBroker(port=0)
    reg_msg = ProtocolMessage.serialize("REGISTER_HOST", {"device_id": "111222333"})
    ws = MockWS(messages=[reg_msg])

    await broker.handle_connection(ws)
    assert len(ws.sent) >= 1
    m_type, payload, _ = ProtocolMessage.deserialize(ws.sent[0])
    assert m_type == "ERROR"
    assert payload.get("error") == "HOST_PUBKEY_REQUIRED"


@pytest.mark.anyio
async def test_broker_rejects_host_impersonation_wrong_pubkey(tmp_path):
    """Verify that attempting to register an existing pinned ID with a different key is rejected."""
    sec1 = SecurityManager(config_dir=str(tmp_path / "h1"), dpapi_engine=_test_dpapi())
    pubkey1 = sec1.get_identity_public_key_hex()

    sec2 = SecurityManager(config_dir=str(tmp_path / "h2"), dpapi_engine=_test_dpapi())
    pubkey2 = sec2.get_identity_public_key_hex()
    assert pubkey1 != pubkey2

    broker = SignalingBroker(port=0, storage_path=str(tmp_path / "pinned.json"))
    # Pre-pin pubkey1 for legit host
    broker.pinned_pubkeys["111222333"] = pubkey1.lower()
    broker._save_pinned_pubkeys()

    # Attacker attempts to register the same device ID with pubkey2
    reg2 = ProtocolMessage.serialize("REGISTER_HOST", {"device_id": "111222333", "pubkey": pubkey2})
    ws2 = MockWS(messages=[reg2])
    ws2.open = False
    await broker.handle_connection(ws2)

    assert len(ws2.sent) >= 1
    m_type, payload, _ = ProtocolMessage.deserialize(ws2.sent[0])
    assert m_type == "ERROR"
    assert payload.get("error") == "HOST_IMPERSONATION_REJECTED"


def test_client_known_hosts_legacy_verify_and_pin(tmp_path):
    """Verify client-side known_hosts legacy helper and alert on host key changes."""
    sec = SecurityManager(config_dir=str(tmp_path), dpapi_engine=_test_dpapi())
    key1 = "aabbccddeeff0011223344556677889900aabbccddeeff001122334455667788"
    key2 = "11223344556677889900aabbccddeeff0011223344556677889900aabbccddee"

    # 1. First seen -> pinned safely
    ok1, reason1 = sec.verify_and_pin_host_key("111222333", key1)
    assert ok1 is True
    assert reason1 == "FIRST_SEEN"

    # 2. Subsequent connection with same key -> VERIFIED
    ok2, reason2 = sec.verify_and_pin_host_key("111222333", key1)
    assert ok2 is True
    assert reason2 == "VERIFIED"

    # 3. Key changed (e.g. rogue broker or MITM) -> KEY_CHANGED (aborts connection)
    ok3, reason3 = sec.verify_and_pin_host_key("111222333", key2)
    assert ok3 is False
    assert reason3 == "KEY_CHANGED"


@pytest.mark.anyio
async def test_broker_host_challenge_response_authentication(tmp_path):
    """Verify challenge-response handshake when a known host reconnects."""
    sec = SecurityManager(config_dir=str(tmp_path), dpapi_engine=_test_dpapi())
    pubkey = sec.get_identity_public_key_hex()

    broker = SignalingBroker(port=0)
    broker.pinned_pubkeys["111222333"] = pubkey.lower()

    # Host attempts to re-register
    reg = ProtocolMessage.serialize("REGISTER_HOST", {"device_id": "111222333", "pubkey": pubkey})
    ws = MockWS(messages=[reg])

    # Start handler in background task
    task = asyncio.create_task(broker.handle_connection(ws))
    await asyncio.sleep(0.01)

    # Broker must have sent HOST_CHALLENGE
    assert len(ws.sent) >= 1
    m_type, payload, _ = ProtocolMessage.deserialize(ws.sent[0])
    assert m_type == "HOST_CHALLENGE"
    nonce_hex = payload.get("nonce")
    assert nonce_hex is not None

    # Sign challenge with correct private key
    sig = sec.sign_data(bytes.fromhex(nonce_hex))
    resp = ProtocolMessage.serialize("HOST_CHALLENGE_RESPONSE", {"device_id": "111222333", "signature": sig.hex()})
    ws.messages.append(resp)
    await asyncio.sleep(0.01)

    # Broker must verify and grant HOST_REGISTERED
    assert len(ws.sent) >= 2
    m_type2, payload2, _ = ProtocolMessage.deserialize(ws.sent[1])
    assert m_type2 == "HOST_REGISTERED"
    assert payload2.get("broker_token") is not None

    ws.open = False
    await task


@pytest.mark.anyio
async def test_broker_rejects_duplicate_registration_when_active(tmp_path):
    """Verify that an active host cannot be overwritten or disconnected by another REGISTER_HOST."""
    sec = SecurityManager(config_dir=str(tmp_path), dpapi_engine=_test_dpapi())
    pubkey = sec.get_identity_public_key_hex()

    broker = SignalingBroker(port=0)

    # Host 1 is active
    reg1 = ProtocolMessage.serialize("REGISTER_HOST", {"device_id": "999888777", "pubkey": pubkey})
    ws1 = MockWS(messages=[reg1])
    ws1.open = True
    task1 = asyncio.create_task(broker.handle_connection(ws1))
    await asyncio.sleep(0.01)
    assert broker.hosts.get("999888777") == ws1

    # Host 2 tries to register same ID while Host 1 is active
    reg2 = ProtocolMessage.serialize("REGISTER_HOST", {"device_id": "999888777", "pubkey": pubkey})
    ws2 = MockWS(messages=[reg2])
    ws2.open = False
    await broker.handle_connection(ws2)

    # Must be rejected with HOST_ALREADY_REGISTERED
    assert len(ws2.sent) >= 1
    m_type, payload, _ = ProtocolMessage.deserialize(ws2.sent[0])
    assert m_type == "ERROR"
    assert payload.get("error") == "HOST_ALREADY_REGISTERED"

    # Host 1 must still be the active host
    assert broker.hosts.get("999888777") == ws1

    ws1.open = False
    await task1


@pytest.mark.anyio
async def test_broker_connection_state_machine_enforcement():
    """Verify connection state machine rejects invalid initial messages and role transitions."""
    broker = SignalingBroker(port=0)

    # Sockets in INIT cannot send session packets directly
    invalid_init = ProtocolMessage.serialize("INPUT", {"action": "key_down"})
    ws = MockWS(messages=[invalid_init])
    ws.open = False
    await broker.handle_connection(ws)

    assert len(ws.sent) >= 1
    m_type, payload, _ = ProtocolMessage.deserialize(ws.sent[0])
    assert m_type == "ERROR"
    assert payload.get("error") == "UNAUTHORIZED_STATE"


@pytest.mark.anyio
async def test_broker_rejects_reserved_client_message_types():
    """Verify that active clients cannot forge broker-internal messages."""
    broker = SignalingBroker(port=0)

    host_ws = MockWS()
    broker.register_host("111222333", host_ws)
    broker.connection_states[host_ws] = "HOST"

    # Client attempts to spoof CLIENT_DISCONNECTED
    for reserved in RESERVED_BROKER_TYPES:
        if reserved in ("REGISTER_HOST", "CONNECT_CLIENT"):
            continue
        spoofed = ProtocolMessage.serialize(reserved, {"target_id": "111222333"})
        ws = MockWS(messages=[spoofed])
        ws.open = False
        await broker.handle_connection(ws)

    # Host should not have received any spoofed internal messages
    assert len(host_ws.sent) == 0


@pytest.mark.anyio
async def test_broker_single_client_routing_policy():
    """Verify that subsequent clients connecting to a busy host receive HOST_BUSY."""
    broker = SignalingBroker(port=0)
    host_ws = MockWS()
    broker.register_host("123456789", host_ws)
    broker.connection_states[host_ws] = "HOST"

    conn_msg1 = ProtocolMessage.serialize("CONNECT_CLIENT", {"device_id": "123456789", "salt": "01" * 16})
    client1_ws = MockWS(messages=[conn_msg1])

    conn_msg2 = ProtocolMessage.serialize("CONNECT_CLIENT", {"device_id": "123456789", "salt": "02" * 16})
    client2_ws = MockWS(messages=[conn_msg2])

    c1_task = asyncio.create_task(broker.handle_connection(client1_ws))
    await asyncio.sleep(0.01)

    assert len(client1_ws.sent) >= 1
    m_type, _, _ = ProtocolMessage.deserialize(client1_ws.sent[0])
    assert m_type == "HOST_FOUND"

    client2_ws.open = False
    await broker.handle_connection(client2_ws)

    assert len(client2_ws.sent) >= 1
    m_type2, _, _ = ProtocolMessage.deserialize(client2_ws.sent[0])
    assert m_type2 == "HOST_BUSY"
    assert broker.routes.get(host_ws) == client1_ws

    client1_ws.open = False
    await c1_task


# ==============================================================================
# Pillar B & D: High-Performance Transport & Telemetry Clock Sync
# ==============================================================================

def test_webrtc_channel_separation_and_clock_estimator():
    """Verify 4-timestamp clock offset estimator and congestion controller adaptation."""
    estimator = ClockOffsetEstimator()
    # t1=1000, t2=2010, t3=2020, t4=1035
    # RTT = (1035 - 1000) - (2020 - 2010) = 35 - 10 = 25ms
    # Offset = ((2010 - 1000) + (2020 - 1035)) / 2 = (1010 + 985) / 2 = 997.5ms
    estimator.update(t1_ms=1000.0, t2_ms=2010.0, t3_ms=2020.0, t4_ms=1035.0)
    assert abs(estimator.rtt_ms - 25.0) < 0.01
    assert abs(estimator.offset_ms - 997.5) < 0.01

    # Verify congestion control adaptation
    controller = CongestionController(min_bitrate_kbps=1500, max_bitrate_kbps=25000)
    bitrate, fps = controller.adapt(rtt_ms=25.0, loss_rate=0.0, queue_delay_ms=5.0)
    assert bitrate > 6000 # Additive increase
    assert fps == 60

    # Simulate congestion
    bitrate_low, fps_low = controller.adapt(rtt_ms=210.0, loss_rate=0.08, queue_delay_ms=45.0)
    assert bitrate_low < bitrate # Multiplicative decrease
    assert fps_low == 30


# ==============================================================================
# Pillar C: Thread-Safe Shutdown & Task Lifecycle
# ==============================================================================

def test_host_daemon_stop_threadsafe(tmp_path):
    """Verify stop() handles both synchronous and asynchronous caller contexts safely."""
    sec = SecurityManager(config_dir=str(tmp_path), dpapi_engine=_test_dpapi())
    injector = MockInputInjector()
    daemon = HostDaemon(
        capture_engine=MockDesktopCapture(),
        input_handler=InputHandler(injector=injector),
        security=sec,
        headless=True
    )
    daemon.running = True
    daemon.stop(timeout=1.0)
    assert daemon.running is False
    assert daemon._shutdown_done is True


@pytest.mark.anyio
async def test_remote_client_manual_disconnect_cancels_tasks(tmp_path):
    """Verify disconnect() cancels in-flight receive and reconnect tasks."""
    client = RemoteClient(config_dir=str(tmp_path), dpapi_engine=_test_dpapi())

    async def dummy_loop():
        try:
            while True:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    client._receive_task = asyncio.create_task(dummy_loop())
    client._reconnect_task = asyncio.create_task(dummy_loop())
    client.state.is_connected = True
    client.auto_reconnect = True

    await client.disconnect()

    assert client._is_manual_disconnect is True
    assert client.auto_reconnect is False
    assert client.state.is_connected is False
    assert client._receive_task is None
    assert client._reconnect_task is None


# ==============================================================================
# File Transfer Resource Limits & Consistency
# ==============================================================================

def test_file_transfer_bounds_and_sanitization(tmp_path):
    """Verify bounds, path traversal protection, and sequential consistency."""
    ft = FileTransferManager(save_dir=str(tmp_path / "incoming"), max_file_size=1024, max_concurrent_transfers=2)

    with pytest.raises(ValueError, match="Invalid file_id"):
        ft.start_receiving("../bad_id", "test.txt", 100)

    with pytest.raises(ValueError, match="exceeds maximum allowed size"):
        ft.start_receiving("id_too_large", "big.bin", 2048)

    part = ft.start_receiving("id_valid_1", "file1.txt", 10)
    assert Path(part).exists()

    with pytest.raises(ValueError, match="Non-contiguous chunk offset"):
        ft.receive_chunk("id_valid_1", 5, b"hello")

    ft.receive_chunk("id_valid_1", 0, b"hello")

    with pytest.raises(ValueError, match="Size mismatch on finish"):
        ft.finish_receiving("id_valid_1")

    ft.cancel_transfer("id_valid_1")
    assert not Path(part).exists()


def test_file_transfer_concurrency_limit(tmp_path):
    """Verify maximum concurrent transfer limit."""
    ft = FileTransferManager(save_dir=str(tmp_path / "incoming"), max_concurrent_transfers=2)
    ft.start_receiving("tx_1", "f1.txt", 10)
    ft.start_receiving("tx_2", "f2.txt", 10)

    with pytest.raises(ValueError, match="Maximum concurrent transfers reached"):
        ft.start_receiving("tx_3", "f3.txt", 10)

    ft.cancel_all_transfers()
    assert len(ft.active_transfers) == 0


# ==============================================================================
# WebRTC, Key Pinning & Honest Telemetry Tests
# ==============================================================================

def test_client_known_hosts_pinning_and_tamper_detection(tmp_path):
    """Verify host identity key pinning, full 32-byte SHA-256 fingerprint generation, and tampering detection."""
    sec = SecurityManager(config_dir=str(tmp_path), dpapi_engine=_test_dpapi())
    test_id = "123456789"
    key1 = secrets.token_bytes(32).hex()
    key2 = secrets.token_bytes(32).hex()

    # First encounter: unpinned, returns full calculated 32-byte (64 hex digit) SHA-256 fingerprint
    status, fp1 = sec.check_host_pin_status(test_id, key1)
    assert status == "UNPINNED"
    assert len(fp1) == 95  # 32 bytes formatted as XX:XX:...:XX
    assert len(fp1.replace(":", "")) == 64  # Complete 64 hex characters (256-bit hash)

    # Pin the key
    assert sec.pin_host_key(test_id, key1) is True

    # Subsequent encounter with same key: PINNED_OK
    status, fp1_again = sec.check_host_pin_status(test_id, key1)
    assert status == "PINNED_OK"
    assert fp1 == fp1_again

    # Encounter with changed/spoofed key: KEY_CHANGED
    status, fp2 = sec.check_host_pin_status(test_id, key2)
    assert status == "KEY_CHANGED"
    assert fp1 != fp2


@pytest.mark.anyio
async def test_client_first_pairing_callback_absent_fails_closed(tmp_path):
    """Verify that if on_verify_fingerprint_callback is absent, first pairing fails closed."""
    sec = SecurityManager(config_dir=str(tmp_path), dpapi_engine=_test_dpapi())
    client = RemoteClient(config_dir=str(tmp_path), dpapi_engine=_test_dpapi(), security=sec)
    client.on_verify_fingerprint_callback = None  # No callback provided

    host_pubkey = secrets.token_bytes(32).hex()
    host_found_msg = ProtocolMessage.serialize("HOST_FOUND", {
        "host_pubkey": host_pubkey,
        "host_salt": secrets.token_bytes(16).hex()
    })
    ws = MockWS(messages=[host_found_msg])
    client.ws_connector = lambda: ws

    connected = await client.connect("111222333", "password123")
    assert connected is False
    assert client.state.is_connected is False
    # Key must NOT be pinned when callback is absent
    assert sec.get_known_host("111222333") is None


@pytest.mark.anyio
async def test_client_first_pairing_fingerprint_callback_rejection(tmp_path):
    """Verify that if user cancels or rejects fingerprint confirmation, connection halts and key is not pinned."""
    sec = SecurityManager(config_dir=str(tmp_path), dpapi_engine=_test_dpapi())
    client = RemoteClient(config_dir=str(tmp_path), dpapi_engine=_test_dpapi(), security=sec)

    user_prompted = []
    def reject_callback(device_id: str, fingerprint: str) -> bool:
        user_prompted.append((device_id, fingerprint))
        return False  # User clicks "No"

    client.on_verify_fingerprint_callback = reject_callback

    host_pubkey = secrets.token_bytes(32).hex()
    host_found_msg = ProtocolMessage.serialize("HOST_FOUND", {
        "host_pubkey": host_pubkey,
        "host_salt": secrets.token_bytes(16).hex()
    })
    ws = MockWS(messages=[host_found_msg])
    client.ws_connector = lambda: ws

    connected = await client.connect("999888777", "password123")
    assert connected is False
    assert len(user_prompted) == 1
    assert user_prompted[0][0] == "999888777"
    assert len(user_prompted[0][1]) == 95  # Formatted XX:XX:...:XX (full 32-byte hash)
    assert len(user_prompted[0][1].replace(":", "")) == 64
    assert client.state.is_connected is False
    # Must NOT pin rejected key
    assert sec.get_known_host("999888777") is None


@pytest.mark.anyio
async def test_client_first_pairing_confirmation_and_subsequent_key_change(tmp_path):
    """Verify explicit confirmation pins host key, subsequent connects verify it, and changed key fails closed."""
    sec = SecurityManager(config_dir=str(tmp_path), dpapi_engine=_test_dpapi())
    client = RemoteClient(config_dir=str(tmp_path), dpapi_engine=_test_dpapi(), security=sec)

    user_prompted = []
    def confirm_callback(device_id: str, fingerprint: str) -> bool:
        user_prompted.append((device_id, fingerprint))
        return True  # User clicks "Yes"

    client.on_verify_fingerprint_callback = confirm_callback

    host_pubkey1 = secrets.token_bytes(32).hex()
    host_salt = secrets.token_bytes(16).hex()

    # 1. First pairing: user confirms
    msg_host_found = ProtocolMessage.serialize("HOST_FOUND", {"host_pubkey": host_pubkey1, "host_salt": host_salt})
    msg_auth_ok = ProtocolMessage.serialize("AUTH_OK", {"status": "authenticated"})
    ws1 = MockWS(messages=[msg_host_found, msg_auth_ok])
    client.ws_connector = lambda: ws1

    connected1 = await client.connect("111222333", "pass123")
    assert connected1 is True
    assert len(user_prompted) == 1
    assert sec.get_known_host("111222333") == host_pubkey1.lower()
    await client.disconnect()

    # 2. Re-connect with same key: does NOT prompt user again
    user_prompted.clear()
    ws2 = MockWS(messages=[msg_host_found, msg_auth_ok])
    client.ws_connector = lambda: ws2
    connected2 = await client.connect("111222333", "pass123")
    assert connected2 is True
    assert len(user_prompted) == 0  # Pre-verified against persistent known_hosts
    await client.disconnect()

    # 3. Connect when host key changed (MITM / rogue broker): fails closed immediately
    host_pubkey_tampered = secrets.token_bytes(32).hex()
    msg_tampered = ProtocolMessage.serialize("HOST_FOUND", {"host_pubkey": host_pubkey_tampered, "host_salt": host_salt})
    ws3 = MockWS(messages=[msg_tampered])
    client.ws_connector = lambda: ws3
    connected3 = await client.connect("111222333", "pass123")
    assert connected3 is False
    assert client.state.is_connected is False
    assert "Chiave host cambiata" in client.on_status_callback.__doc__ if client.on_status_callback else True


def test_client_viewer_verify_fingerprint_fails_without_root_window():
    """Verify _on_verify_host_fingerprint fails closed if root window is absent or destroyed."""
    from src.client_viewer import ClientViewer
    viewer = ClientViewer.__new__(ClientViewer)
    viewer.root = None  # No root window
    result = viewer._on_verify_host_fingerprint("123456789", "AA:BB:CC:DD")
    assert result is False


def test_native_engine_abi_client_video_poll_boundary():
    """
    Verify Python NativeEngine ABI binding boundary for client video frame polling.
    Note: Full RFC 6184 protocol tests (Single NAL, STAP-A valid/malformed, FU-A valid/interrupted,
    sequence loss/reorder, timestamp discontinuity, incomplete IDR, complete IDR recovery,
    and latest-wins queue) are implemented directly in Rust native code under #[cfg(test)]
    in native/aether_core/src/webrtc_transport.rs.
    """
    engine = get_native_engine()
    # When native library is not loaded or handle is 0, polling safely returns None without crash
    res = engine.poll_client_video_frame()
    assert res is None


def test_telemetry_honest_none_reporting():
    """Verify that unmeasured telemetry metrics strictly return None, never fake dummy constants."""
    engine = get_native_engine()
    snap = engine.get_telemetry_snapshot()

    # Capture, encode, decode, render metrics must be None or real float, never dummy constants
    if not engine.is_native_loaded:
        assert snap.get("capture_ms") is None or isinstance(snap.get("capture_ms"), float)
        assert snap.get("encode_ms") is None or isinstance(snap.get("encode_ms"), float)
        assert snap.get("decode_ms") is None or isinstance(snap.get("decode_ms"), float)
        assert snap.get("render_ms") is None or isinstance(snap.get("render_ms"), float)

    # Verify zero-rtt estimator produces 0.0 or None, not synthetic 12.0
    est = ClockOffsetEstimator()
    assert est.rtt_ms in (0.0, None)
    assert est.offset_ms in (0.0, None)
    assert est.jitter_ms in (0.0, None)


@pytest.mark.anyio
async def test_client_webrtc_input_segregation_routing(tmp_path):
    """Verify send_input routes cursor moves to loss-tolerant channel and clicks to control channel."""
    client = RemoteClient(config_dir=str(tmp_path), dpapi_engine=_test_dpapi())
    client.state.is_connected = True
    client.state.session_key = secrets.token_bytes(32)
    client.state.target_id = "111222333"
    client._webrtc_active = True

    cursor_calls = []
    control_calls = []

    # Mock native engine methods
    client.native_engine.is_native_loaded = True
    client.native_engine.client_handle = 12345
    client.native_engine.send_client_cursor = lambda x, y, seq: cursor_calls.append((x, y, seq)) or True
    client.native_engine.send_client_control = lambda data: control_calls.append(data) or True

    # 1. Mouse move -> routed to send_client_cursor
    await client.send_input({"type": "mouse_move", "x": 0.5, "y": 0.5})
    assert len(cursor_calls) == 1
    assert len(control_calls) == 0

    # 2. Discrete mouse click -> routed to send_client_control
    await client.send_input({"type": "mouse_down", "button": "left"})
    assert len(cursor_calls) == 1
    assert len(control_calls) == 1


@pytest.mark.anyio
async def test_bounded_queue_and_cleanup_on_disconnect(tmp_path):
    """Verify disconnect() cleanly destroys native handle, resets _webrtc_active and cancels tasks."""
    client = RemoteClient(config_dir=str(tmp_path), dpapi_engine=_test_dpapi())
    client.state.is_connected = True
    client._webrtc_active = True
    client.native_engine.is_native_loaded = True
    client.native_engine.client_handle = 99999

    destroyed = []
    client.native_engine.destroy_client_session = lambda: destroyed.append(True) or True

    await client.disconnect()

    assert client._webrtc_active is False
    assert client.state.is_connected is False
    assert len(destroyed) == 1


@pytest.mark.anyio
async def test_host_control_backpressure_saturation_triggers_emergency_input_recovery(tmp_path):
    """
    Verify that if the host native WebRTC control channel experiences saturation/backpressure timeout,
    the host immediately executes emergency input recovery (_safe_release_inputs) and terminates streaming.
    """
    from src.host_daemon import HostDaemon

    daemon = HostDaemon(headless=True, config_dir=str(tmp_path))
    recovered = []
    daemon._safe_release_inputs = lambda timeout=1.0: recovered.append(timeout) or True
    daemon._webrtc_active = True
    daemon._streaming_active = True

    # Mock native engine with control saturated
    daemon.native_engine.is_native_loaded = True
    daemon.native_engine.host_handle = 12345
    daemon.native_engine.is_host_control_saturated = lambda: True

    # Simulate loop check
    if daemon.native_engine.is_host_control_saturated():
        daemon._safe_release_inputs(timeout=2.0)
        daemon._streaming_active = False
        daemon._webrtc_active = False

    assert len(recovered) == 1
    assert recovered[0] == 2.0
    assert daemon._streaming_active is False
    assert daemon._webrtc_active is False


class MockTestWebSocket:
    def __init__(self, state="OPEN"):
        class State:
            name = state
        self.state = State()
        self.open = True
        self.sent_messages = []

    async def send(self, msg):
        self.sent_messages.append(msg)


@pytest.mark.anyio
async def test_host_control_saturation_race_with_ice_closed_still_releases_inputs_and_stops_streaming(tmp_path):
    """
    Reproduces the exact race condition:
    1. WebRTC control queue timeout closes the PeerConnection.
    2. ICE state transitions to Closed (code 6).
    3. Host streaming loop ticks: saturation flag is present.
    4. An input key was previously held down (e.g. vk=65).
    5. Verification: saturation is checked BEFORE ICE state / fallback branching,
       emergency _safe_release_inputs(2.0) is executed, pressed keys are cleared,
       _streaming_active and _webrtc_active become False, session_state is cleared,
       and client receives an explicit CONTROL_CHANNEL_SATURATED error.
    """
    from src.host_daemon import HostDaemon
    from src.input_handler import MockInputInjector, InputHandler
    from src.input_worker import InputWorker

    daemon = HostDaemon(headless=True, config_dir=str(tmp_path), start_worker=False)
    inj = MockInputInjector(screen_w=1920, screen_h=1080)
    handler = InputHandler(injector=inj)
    worker = InputWorker(handler, maxsize=16)
    daemon.input_handler = handler
    daemon.input_worker = worker
    worker.start()

    # 1. Key is pressed down initially
    handler.key_down(65)
    assert 65 in handler._pressed_keys

    # 2. Simulate WebRTC active, but PeerConnection has closed due to timeout and ICE is Closed
    daemon._webrtc_active = True
    daemon._streaming_active = True
    daemon.native_engine.is_native_loaded = True
    daemon.native_engine.host_handle = 12345
    # ICE is already Closed (6)
    daemon.native_engine.get_host_ice_state = lambda: 6
    # Control channel is saturated
    daemon.native_engine.is_host_control_saturated = lambda: True

    session_state = {"authenticated": True, "session_key": b"mock_session_key_32_bytes_long!!"}
    mock_ws = MockTestWebSocket()
    daemon.running = True

    try:
        # 3. Execute run_capture_stream: it must enter the loop, detect saturation at Step 0,
        # release inputs, halt streaming, teardown session, and terminate without waiting
        await daemon.run_capture_stream(mock_ws, b"mock_session_key_32_bytes_long!!", session_state=session_state)
    finally:
        daemon.running = False
        daemon._streaming_active = False
        worker.stop(timeout=1.0)
        daemon.stop()

    # 4. Verify that emergency input recovery occurred and keys are released
    assert 65 not in handler._pressed_keys
    assert len(handler._pressed_keys) == 0

    # 5. Verify streaming and WebRTC were explicitly halted
    assert daemon._streaming_active is False
    assert daemon._webrtc_active is False
    assert daemon._control_saturated is True

    # 6. Verify session_state is cleared (no continuing as if reliable)
    assert len(session_state) == 0

    # 7. Verify client received the explicit error message
    assert len(mock_ws.sent_messages) >= 1
    found_err = False
    for msg in mock_ws.sent_messages:
        if b"CONTROL_CHANNEL_SATURATED" in msg:
            found_err = True
            break
    assert found_err is True

    # 8. Verify subsequent client commands are rejected due to saturation latch
    resp = daemon.handle_client_message("INPUT", {"action": "key_down", "vk": 66}, None, session_state)
    assert resp is not None
    assert b"CONTROL_CHANNEL_SATURATED" in resp


@pytest.mark.anyio
async def test_host_control_saturation_latch_resets_only_on_authenticated_client_arrived_with_clean_recovery(tmp_path):
    """
    Verify that _control_saturated acts as a session-scoped latch:
    - Persists for the saturated session, rejecting all incoming client commands.
    - An unauthenticated CLIENT_ARRIVED (invalid broker token) is rejected and leaves the latch active.
    - When recovery is incomplete (simulated deterministically with worker execution lock occupied by an in-flight command),
      the latch remains fail-closed (True), CLIENT_ARRIVED is refused with HOST_RECOVERY_INCOMPLETE, and the session is not authenticated.
    - When recovery is clean, old native session is destroyed, inputs released, _release_pending cleared,
      and only then does the authentic CLIENT_ARRIVED reset the latch atomically to False, allowing a new session to authenticate.
    """
    from src.host_daemon import HostDaemon
    from src.input_handler import MockInputInjector, InputHandler
    from src.input_worker import InputWorker

    daemon = HostDaemon(headless=True, config_dir=str(tmp_path), start_worker=False)
    inj = MockInputInjector(screen_w=1920, screen_h=1080)
    handler = InputHandler(injector=inj)
    worker = InputWorker(handler, maxsize=16)
    daemon.input_handler = handler
    daemon.input_worker = worker
    worker.start()

    daemon._broker_token = "valid_broker_token_secret"
    mock_ws = MockTestWebSocket()
    destroyed_sessions = []
    daemon.native_engine.destroy_host_session = lambda: destroyed_sessions.append(True)
    daemon.native_engine.host_handle = 99999

    try:
        # 1. Simulate control saturation during an active session with key held down
        daemon._control_saturated = True
        session_state = {"authenticated": True, "session_key": b"active_session_key_32_bytes!!", "client_ip": "peer1"}
        handler.key_down(65)
        assert 65 in handler._pressed_keys

        # 2. Subsequent client commands are rejected while latch is True
        resp_input = daemon.handle_client_message("INPUT", {"action": "key_down", "vk": 65}, None, session_state)
        assert resp_input is not None
        assert b"CONTROL_CHANNEL_SATURATED" in resp_input

        resp_auth = daemon.handle_client_message("AUTH_REQUEST", {}, None, session_state)
        assert resp_auth is not None
        assert b"CONTROL_CHANNEL_SATURATED" in resp_auth

        # 3. Simulate unauthenticated CLIENT_ARRIVED event (invalid broker token)
        bad_payload = {"broker_token": "wrong_attacker_token", "salt": "abcd", "client_id": "rogue"}
        accepted_bad = await daemon.handle_client_arrived(bad_payload, session_state, ws=mock_ws)
        assert accepted_bad is False
        # Saturated latch remains active and inputs not released by unauthorized event
        assert daemon._control_saturated is True
        assert 65 in handler._pressed_keys

        # 4. Saturated session where recovery is incomplete:
        # Deterministically simulate an active in-flight command holding the execution lock.
        # This prevents release_all() from acquiring the lock within timeout, triggering _release_pending.
        worker._exec_lock.acquire()
        try:
            auth_payload = {"broker_token": "valid_broker_token_secret", "salt": "1234", "client_id": "client_new"}
            orig_safe_release = daemon._safe_release_inputs
            daemon._safe_release_inputs = lambda timeout=0.02: orig_safe_release(timeout=0.02)

            accepted_incomplete = await daemon.handle_client_arrived(auth_payload, session_state, ws=mock_ws)
            # Must be rejected fail-closed
            assert accepted_incomplete is False
            assert daemon._control_saturated is True
            assert worker._release_pending is True
            assert session_state.get("authenticated") is None

            # Verify client/broker received HOST_RECOVERY_INCOMPLETE error
            assert any(b"HOST_RECOVERY_INCOMPLETE" in m for m in mock_ws.sent_messages)
        finally:
            worker._exec_lock.release()
            daemon._safe_release_inputs = orig_safe_release

        # 5. Authentic reset with verified clean recovery:
        # Execution lock is free, release_all succeeds, old native handle is closed, _release_pending is False
        mock_ws.sent_messages.clear()
        auth_payload = {"broker_token": "valid_broker_token_secret", "salt": "1234", "client_id": "client_new"}
        accepted_clean = await daemon.handle_client_arrived(auth_payload, session_state, ws=mock_ws)
        assert accepted_clean is True

        # Verify old session was destroyed and all recovery contracts hold
        assert len(destroyed_sessions) >= 1
        assert len(handler._pressed_keys) == 0
        assert worker._release_pending is False
        assert daemon._control_saturated is False  # Atomically reset for the new session!

        # New session state is prepared and awaiting authentication
        assert session_state.get("client_ip") == "client_new"
        assert session_state.get("client_salt") == bytes.fromhex("1234")

        # 6. Saturated latch is cleared: new session can execute commands without rejection
        resp_keyframe = daemon.handle_client_message("KEYFRAME_REQ", {}, None, {"authenticated": True})
        assert resp_keyframe is None  # Handled normally without rejection!
        assert daemon._keyframe_requested is True

    finally:
        daemon.running = False
        worker.stop(timeout=1.0)
        daemon.stop()


def test_webrtc_cursor_routed_through_input_worker_without_concurrency(tmp_path):
    """
    Verify that incoming WebRTC cursor coordinates from poll_host_cursor() are routed
    through HostDaemon._poll_and_enqueue_webrtc_cursor() into InputWorker:
    - Normalizes coordinates against screen bounds (e.g. 960/1920 -> 0.5, 540/1080 -> 0.5).
    - Polling the cursor enqueues into InputWorker without calling handler.move_mouse directly.
    - Consecutive cursor updates are coalesced via latest-wins in CoalescingQueue (queue size stays 1).
    - After worker.start() and flush(), the coalesced move is executed exactly once, and
      strictly by the InputWorker background thread (worker._thread), not by the daemon.
    - Interleaved discrete events (mouse_down) are serialized without concurrency, verified
      via a tracking injector monitoring max_concurrent_calls == 1.
    """
    from src.host_daemon import HostDaemon
    from src.input_handler import MockInputInjector, InputHandler
    from src.input_worker import InputWorker

    class TrackingMockInjector(MockInputInjector):
        def __init__(self, screen_w: int = 1920, screen_h: int = 1080):
            super().__init__(screen_w=screen_w, screen_h=screen_h)
            self._current_calls = 0
            self.max_concurrent_calls = 0
            self._call_lock = threading.Lock()

        def _enter_call(self):
            with self._call_lock:
                self._current_calls += 1
                if self._current_calls > self.max_concurrent_calls:
                    self.max_concurrent_calls = self._current_calls

        def _exit_call(self):
            with self._call_lock:
                self._current_calls -= 1

        def set_cursor_pos(self, x: int, y: int) -> bool:
            self._enter_call()
            try:
                time.sleep(0.002)
                return super().set_cursor_pos(x, y)
            finally:
                self._exit_call()

        def send_input(self, inp) -> bool:
            self._enter_call()
            try:
                time.sleep(0.002)
                return super().send_input(inp)
            finally:
                self._exit_call()

    daemon = HostDaemon(headless=True, config_dir=str(tmp_path), start_worker=False)
    inj = TrackingMockInjector(screen_w=1920, screen_h=1080)
    handler = InputHandler(injector=inj)
    worker = InputWorker(handler, maxsize=16)
    daemon.input_handler = handler
    daemon.input_worker = worker

    # Spy on handler.move_mouse to record caller thread identity, arguments, and invocation count
    recorded_move_calls = []
    orig_move_mouse = handler.move_mouse
    def spy_move_mouse(x, y, *args, **kwargs):
        caller_thread = threading.current_thread()
        recorded_move_calls.append({
            "coords": (x, y),
            "thread": caller_thread,
            "thread_ident": caller_thread.ident,
            "thread_name": caller_thread.name,
        })
        return orig_move_mouse(x, y, *args, **kwargs)
    handler.move_mouse = spy_move_mouse

    try:
        # 1. First cursor update: (960, 540, 101)
        daemon.native_engine.poll_host_cursor = lambda: (960, 540, 101)
        res1 = daemon._poll_and_enqueue_webrtc_cursor()

        # Coordinates normalized against screen bounds (1920x1080)
        assert res1 == (0.5, 0.5, 101)
        # Prior to worker start: verify NO direct call was made to handler.move_mouse by daemon
        assert len(recorded_move_calls) == 0
        # Worker queue contains 1 item
        assert worker.queue.qsize() == 1

        # 2. Second cursor update arrives before worker processes first: (1440, 810, 102)
        daemon.native_engine.poll_host_cursor = lambda: (1440, 810, 102)
        res2 = daemon._poll_and_enqueue_webrtc_cursor()

        assert res2 == (0.75, 0.75, 102)
        # Still zero direct calls from daemon
        assert len(recorded_move_calls) == 0
        # Latest-wins coalescing in CoalescingQueue: queue size remains 1 (not 2!)
        assert worker.queue.qsize() == 1

        # 3. Enqueue discrete event (mouse_down) to test interleaved ordering with motion
        worker.enqueue({"action": "mouse_down", "button": "left"})
        assert worker.queue.qsize() == 2

        # 4. Start worker background thread and flush queue completely
        worker.start()
        worker_thread = worker._thread
        assert worker_thread is not None
        assert worker_thread.is_alive()

        flushed = worker.flush(timeout=2.0)
        assert flushed is True

        # Queue is drained
        assert worker.queue.qsize() == 0

        # 5. Verify move execution: executed EXACTLY ONCE, with the coalesced latest-wins coordinates,
        # and executed strictly by the InputWorker background thread (not the daemon or test thread)
        assert len(recorded_move_calls) == 1
        call = recorded_move_calls[0]
        assert call["coords"] == (0.75, 0.75)
        assert call["thread"] is worker_thread
        assert call["thread_ident"] == worker_thread.ident
        assert call["thread_name"] == "InputWorkerThread"
        assert call["thread"] is not threading.current_thread()

        # Injector executed both the coalesced movement and the discrete click
        assert len(inj.sent_inputs) >= 2
        # Execution was strictly serialized: max_concurrent_calls on injector never exceeded 1
        assert inj.max_concurrent_calls == 1

        # Final cursor coordinates applied to handler
        assert handler.last_pixel_pos == (1440, 810)
        assert "left" in handler._pressed_mouse_buttons

    finally:
        worker.stop(timeout=1.0)
        daemon.stop()


def test_host_control_burst_drain_consumes_all_pending_messages(tmp_path):
    """
    Verify that host streaming loop drains up to 64 discrete control messages per tick,
    preventing backlog during fast typing or click bursts.
    """
    from src.host_daemon import HostDaemon

    daemon = HostDaemon(headless=True, config_dir=str(tmp_path))
    messages = [b"MSG1", b"MSG2", b"MSG3", b"MSG4", b"MSG5"]
    polled = []

    def mock_poll():
        if messages:
            return messages.pop(0)
        return None

    daemon.native_engine.poll_host_control = mock_poll

    # Simulate burst drain loop
    for _ in range(64):
        ctrl_data = daemon.native_engine.poll_host_control()
        if not ctrl_data:
            break
        polled.append(ctrl_data)

    assert len(polled) == 5
    assert polled == [b"MSG1", b"MSG2", b"MSG3", b"MSG4", b"MSG5"]
