import json
import struct

MAGIC = b"RDT1"

class ProtocolMessage:
    """Binary message framing protocol with JSON metadata and binary payload."""

    @staticmethod
    def serialize(msg_type: str, payload: dict = None, binary_data: bytes = None) -> bytes:
        if payload is None:
            payload = {}
        header_dict = {"t": msg_type, "p": payload}
        header_bytes = json.dumps(header_dict, separators=(",", ":")).encode("utf-8")
        header_len = len(header_bytes)

        # Pack: Magic (4B) + Header Length (4B, big-endian) + Header + BinaryData
        packed = MAGIC + struct.pack("!I", header_len) + header_bytes
        if binary_data:
            packed += binary_data
        return packed

    @staticmethod
    def deserialize(raw_bytes: bytes) -> tuple[str, dict, bytes | None]:
        if len(raw_bytes) < 8:
            raise ValueError("Message too short")
        if raw_bytes[:4] != MAGIC:
            raise ValueError("Invalid magic header")

        header_len = struct.unpack("!I", raw_bytes[4:8])[0]
        if len(raw_bytes) < 8 + header_len:
            raise ValueError("Incomplete header in message")

        header_bytes = raw_bytes[8:8 + header_len]
        binary_data = raw_bytes[8 + header_len:] if len(raw_bytes) > 8 + header_len else None

        header = json.loads(header_bytes.decode("utf-8"))
        msg_type = header.get("t", "")
        payload = header.get("p", {})

        return (msg_type, payload, binary_data)
