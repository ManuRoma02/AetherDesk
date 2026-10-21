import pytest
import asyncio
from src.protocol import ProtocolMessage
from src.signaling_broker import SignalingBroker

def test_protocol_serialization_json_only():
    data = ProtocolMessage.serialize("HELLO", {"device_id": "123456789"})
    msg_type, payload, binary = ProtocolMessage.deserialize(data)
    assert msg_type == "HELLO"
    assert payload["device_id"] == "123456789"
    assert binary is None

def test_protocol_serialization_with_binary():
    bin_payload = b"\xff\xd8\xff\xe0JPEGDATA_123456"
    data = ProtocolMessage.serialize("FRAME", {"w": 1920, "h": 1080}, binary_data=bin_payload)
    msg_type, payload, binary = ProtocolMessage.deserialize(data)
    assert msg_type == "FRAME"
    assert payload["w"] == 1920
    assert payload["h"] == 1080
    assert binary == bin_payload

@pytest.mark.anyio
async def test_signaling_broker_registration_and_lookup():
    broker = SignalingBroker(port=0)
    # Test internal state management
    broker.register_host("111222333", "mock_host_conn")
    assert broker.get_host("111222333") == "mock_host_conn"
    assert broker.get_host("999999999") is None
    broker.unregister_host("111222333")
    assert broker.get_host("111222333") is None
