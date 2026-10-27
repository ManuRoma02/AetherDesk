import pytest
import asyncio
import secrets
import json
import platform
from src.protocol import ProtocolMessage
from src.security import SecurityManager, MockTestDPAPIEngine
from src.capture import MockDesktopCapture
from src.input_handler import InputHandler, MockInputInjector
from src.host_daemon import HostDaemon
from src.client import RemoteClient, ClientState

def _test_dpapi():
    return MockTestDPAPIEngine() if platform.system() != "Windows" else None

@pytest.mark.anyio
async def test_full_e2e_remote_desktop_pipeline(tmp_path):
    daemon = None
    try:
        # 1. Setup Host with MockInputInjector and MockDesktopCapture
        host_config_dir = tmp_path / "host_cfg"
        mock_inj = MockInputInjector()
        mock_cap = MockDesktopCapture(target_width=320, target_height=180, quality=50)
        daemon = HostDaemon(
            config_dir=str(host_config_dir),
            headless=True,
            dpapi_engine=_test_dpapi(),
            input_handler=InputHandler(injector=mock_inj),
            capture=mock_cap,
        )
        dev_id = daemon.security.get_or_create_device_id()
        daemon.security.set_password("SuperSecret2026!")

        # Verify ID format
        assert ClientState.validate_id(dev_id)

        # 2. Derive mutual session key as would happen during E2EE handshake
        client_salt = secrets.token_bytes(16)
        client_mgr = SecurityManager(config_dir=str(tmp_path / "client_cfg"), dpapi_engine=_test_dpapi())
        client_key = client_mgr.derive_session_key("SuperSecret2026!", client_salt)

        host_salt = daemon.security.config["pwd_salt"]
        # Host session key
        host_key = daemon.security.derive_session_key("SuperSecret2026!", client_salt)
        assert client_key == host_key

        # 3. Test Video Frame Transmission pipeline using simulated capture (zero screen/DXcam interaction)
        cap = MockDesktopCapture(target_width=320, target_height=180, quality=50)
        raw_jpeg, (w, h), (cx, cy) = cap.capture_frame()
        cap.close()

        assert len(raw_jpeg) > 0
        # Host encrypts frame
        enc_frame = daemon.security.encrypt_packet(raw_jpeg, host_key)
        # Network message serialization
        wire_msg = ProtocolMessage.serialize("FRAME", {"w": w, "h": h, "cx": cx, "cy": cy}, binary_data=enc_frame)

        # Client receives and deserializes
        msg_type, meta, enc_recv = ProtocolMessage.deserialize(wire_msg)
        assert msg_type == "FRAME"
        assert meta["w"] == w
        assert enc_recv == enc_frame

        # Client decrypts frame
        dec_jpeg = client_mgr.decrypt_packet(enc_recv, client_key)
        assert dec_jpeg == raw_jpeg

        # 4. Test Elevated Input Transmission pipeline via MockInputInjector
        input_payload = {"type": "mouse_move", "x": 0.5, "y": 0.5}
        enc_input = client_mgr.encrypt_packet(json.dumps(input_payload).encode("utf-8"), client_key)
        input_msg = ProtocolMessage.serialize("INPUT", {}, binary_data=enc_input)

        # Host receives input
        in_type, in_meta, in_enc = ProtocolMessage.deserialize(input_msg)
        assert in_type == "INPUT"
        session_state = {"session_key": host_key, "authenticated": True}
        res = daemon.handle_client_message("INPUT", in_meta, in_enc, session_state)
        assert res is not None
        daemon.input_worker.flush(timeout=1.0)
        assert len(mock_inj.sent_inputs) > 0

        # 5. Test invalid password rejection
        wrong_key = client_mgr.derive_session_key("WrongPassword!", client_salt)
        enc_bad_input = client_mgr.encrypt_packet(json.dumps(input_payload).encode("utf-8"), wrong_key)
        bad_res = daemon.handle_client_message("INPUT", in_meta, enc_bad_input, session_state)
        assert bad_res is None
    finally:
        if daemon:
            daemon.stop()
