import pytest
from pathlib import Path
from src.security import SecurityManager
from src.client import RemoteClient
from src.transfer import FileTransferManager
from src.signaling_broker import SignalingBroker
from src.protocol import ProtocolMessage

def test_repro_client_missing_callbacks():
    """Verify RemoteClient has all expected callback attributes in __init__."""
    client = RemoteClient()
    # These should be defined as None, not raise AttributeError
    assert hasattr(client, "on_frame_callback")
    assert hasattr(client, "on_status_callback")
    assert hasattr(client, "on_clipboard_callback")
    assert hasattr(client, "on_file_saved_callback")

def test_repro_file_transfer_path_traversal(tmp_path):
    """Verify that path traversal attempts are sanitized to save_dir."""
    ft = FileTransferManager(save_dir=str(tmp_path / "transfers"))
    malicious_filename = "../../evil.exe"
    target = ft.start_receiving("test_id", malicious_filename, 100)
    ft.finish_receiving("test_id")
    # target MUST be inside ft.save_dir and not traverse upwards
    assert target.parent.resolve() == ft.save_dir.resolve()
    assert target.name == "evil.exe"

@pytest.mark.anyio
async def test_repro_broker_forwards_all_remote_desktop_messages():
    """Verify SignalingBroker correctly relays remote desktop control messages."""
    broker = SignalingBroker(port=0)
    
    class MockWS:
        def __init__(self):
            self.sent = []
        async def send(self, data):
            self.sent.append(data)

    host_ws = MockWS()
    broker.register_host("111222333", host_ws)

    # Client sends an INPUT message targeting host 111 222 333
    input_msg = ProtocolMessage.serialize("INPUT", {"target_id": "111 222 333"}, b"fake_input")
    
    # Simulate broker handling from a client connection
    class ClientWS:
        def __init__(self):
            self.messages = [input_msg]
        def __aiter__(self):
            return self
        async def __anext__(self):
            if not self.messages:
                raise StopAsyncIteration
            return self.messages.pop(0)

    client_ws = ClientWS()
    await broker.handle_connection(client_ws)

    # Host MUST have received the relayed message
    assert len(host_ws.sent) == 1
    m_type, payload, bin_data = ProtocolMessage.deserialize(host_ws.sent[0])
    assert m_type == "INPUT"
    assert bin_data == b"fake_input"


def test_repro_mutual_session_key_derivation(tmp_path):
    """Verify host and client derive identical session keys from the user password."""
    sec = SecurityManager(config_dir=str(tmp_path))
    sec.set_password("MyStrongPass2026")
    host_salt = bytes.fromhex(sec.config["pwd_salt"])
    master_key_host = bytes.fromhex(sec.config["master_key"])

    # Client receives host_salt, has password "MyStrongPass2026"
    session_salt = b"random_session_salt_123"
    client_master = sec.derive_master_key("MyStrongPass2026", host_salt)
    assert client_master == master_key_host

    client_session_key = sec.derive_session_key(client_master, session_salt)
    host_session_key = sec.derive_session_key(master_key_host, session_salt)

    assert client_session_key == host_session_key

    # Encrypt on host, decrypt on client
    msg = sec.encrypt_packet(b"hello client", host_session_key)
    dec = sec.decrypt_packet(msg, client_session_key)
    assert dec == b"hello client"


def test_repro_client_viewer_gui_init():
    """Verify ClientViewerGUI initializes without NameError on Path or missing assets."""
    from src.client_viewer import ClientViewerGUI
    gui = ClientViewerGUI()
    try:
        assert gui.root is not None
        assert "AetherDesk" in gui.root.title()
    finally:
        gui.root.destroy()

