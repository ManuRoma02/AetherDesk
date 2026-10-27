"""
AetherDesk Signaling Broker Layer.

Architectural Boundary:
- Broker Layer: Zero-knowledge signaling broker routing opaque encrypted messages between peer WebSockets.
- Security Isolation: Enforces reservation of broker-internal control message types, attaches cryptographic
  session tokens to legitimate broker events, and guarantees a strict single-client per host policy (rejecting
  secondary connections with HOST_BUSY without routing table corruption).
"""

import asyncio
import logging
import secrets
from typing import Dict, Any, Optional
from pathlib import Path
import sys

# Ensure repository root is in sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

import websockets
from src.protocol import ProtocolMessage

logger = logging.getLogger("SignalingBroker")

RESERVED_BROKER_TYPES = {
    "REGISTER_HOST",
    "CONNECT_CLIENT",
    "HOST_REGISTERED",
    "HOST_CHALLENGE",
    "HOST_CHALLENGE_RESPONSE",
    "CLIENT_ARRIVED",
    "CLIENT_DISCONNECTED",
    "HOST_FOUND",
    "HOST_NOT_FOUND",
    "HOST_BUSY",
    "HOST_ALREADY_REGISTERED",
    "HOST_IMPERSONATION_REJECTED",
}

class SignalingBroker:
    """Zero-knowledge rendezvous & signaling broker for remote desktop connections."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765, storage_path: Optional[str] = None):
        self.host = host
        self.port = port
        self.hosts: Dict[str, Any] = {} # clean_device_id -> websocket
        self.host_salts: Dict[str, str] = {} # clean_device_id -> host_salt
        self.host_tokens: Dict[str, str] = {} # clean_device_id -> broker_token
        self.pinned_pubkeys: Dict[str, str] = {} # clean_device_id -> pinned_pubkey_hex
        self.ws_to_device: Dict[Any, str] = {} # websocket -> clean_device_id
        self.routes: Dict[Any, Any] = {} # ws -> peer_ws
        self.connection_states: Dict[Any, str] = {} # ws -> "INIT"|"CHALLENGED"|"HOST"|"CLIENT"|"TERMINATED"
        self.pending_challenges: Dict[Any, tuple[str, bytes, dict, str]] = {} # ws -> (clean_id, nonce, payload, pubkey)
        self.server = None
        self.storage_path = Path(storage_path) if storage_path else None
        self._load_pinned_pubkeys()

    def _load_pinned_pubkeys(self):
        """Load persistently pinned host public keys from disk if configured."""
        if self.storage_path and self.storage_path.exists():
            try:
                import json
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self.pinned_pubkeys = {str(k).lower(): str(v).lower() for k, v in data.items()}
            except Exception as e:
                logger.warning(f"Failed to load pinned keys from {self.storage_path}: {e}")

    def _save_pinned_pubkeys(self):
        """Persist pinned host public keys to disk atomically."""
        if self.storage_path:
            try:
                import json
                self.storage_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = self.storage_path.with_suffix(".tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(self.pinned_pubkeys, f, indent=2)
                tmp_path.replace(self.storage_path)
            except Exception as e:
                logger.warning(f"Failed to persist pinned keys to {self.storage_path}: {e}")

    def register_host(self, device_id: str, websocket: Any, host_salt: str = "") -> str:
        clean_id = device_id.replace(" ", "")
        token = secrets.token_hex(24)
        self.hosts[clean_id] = websocket
        self.host_salts[clean_id] = host_salt
        self.host_tokens[clean_id] = token
        self.ws_to_device[websocket] = clean_id
        return token

    def unregister_host(self, device_id: str, websocket=None):
        clean_id = device_id.replace(" ", "")
        if clean_id in self.hosts:
            if websocket is None or self.hosts[clean_id] == websocket:
                del self.hosts[clean_id]
        if clean_id in self.host_salts:
            if websocket is None or clean_id not in self.hosts:
                del self.host_salts[clean_id]
        if clean_id in self.host_tokens:
            if websocket is None or clean_id not in self.hosts:
                del self.host_tokens[clean_id]
        if websocket in self.ws_to_device:
            del self.ws_to_device[websocket]

    def get_host(self, device_id: str) -> Any:
        clean_id = device_id.replace(" ", "")
        return self.hosts.get(clean_id)

    def get_host_salt(self, device_id: str) -> str:
        clean_id = device_id.replace(" ", "")
        return self.host_salts.get(clean_id, "")

    def get_host_broker_token(self, device_id: str) -> str:
        clean_id = device_id.replace(" ", "")
        return self.host_tokens.get(clean_id, "")

    async def handle_connection(self, websocket):
        device_id = None
        role = None # "host" or "client"
        self.connection_states[websocket] = "INIT"
        try:
            async for raw in websocket:
                if isinstance(raw, str):
                    raw = raw.encode("utf-8")
                try:
                    msg_type, payload, binary = ProtocolMessage.deserialize(raw)
                except Exception as e:
                    logger.warning(f"Malformed message: {e}")
                    continue

                state = self.connection_states.get(websocket, "INIT")

                # State 1: INIT connection (must establish role as HOST or CLIENT)
                if state == "INIT":
                    if msg_type == "REGISTER_HOST":
                        device_id_raw = payload.get("device_id", "")
                        clean_id = device_id_raw.replace(" ", "")
                        if not clean_id:
                            await websocket.send(ProtocolMessage.serialize("ERROR", {"error": "INVALID_DEVICE_ID"}))
                            continue

                        # Check if host is already actively connected (prevent silent replacement)
                        existing_host = self.hosts.get(clean_id)
                        if existing_host is not None:
                            ws_state = getattr(existing_host, "state", None)
                            is_active = (getattr(ws_state, "name", "") == "OPEN") if ws_state is not None else getattr(existing_host, "open", True)
                            if is_active:
                                logger.warning(f"Rejecting duplicate registration for device {clean_id}: already active.")
                                await websocket.send(ProtocolMessage.serialize("ERROR", {
                                    "error": "HOST_ALREADY_REGISTERED",
                                    "device_id": clean_id
                                }))
                                continue
                            else:
                                self.unregister_host(clean_id, existing_host)

                        host_pubkey = payload.get("pubkey", "").strip()
                        if not host_pubkey:
                            await websocket.send(ProtocolMessage.serialize("ERROR", {
                                "error": "HOST_PUBKEY_REQUIRED",
                                "message": "Host public key required for cryptographic identity registration."
                            }))
                            continue

                        # Public key pinning & authentication
                        if clean_id in self.pinned_pubkeys:
                            pinned = self.pinned_pubkeys[clean_id]
                            if pinned.lower() != host_pubkey.lower():
                                logger.warning(f"Host impersonation rejected: public key mismatch for {clean_id}")
                                await websocket.send(ProtocolMessage.serialize("ERROR", {
                                    "error": "HOST_IMPERSONATION_REJECTED",
                                    "device_id": clean_id
                                }))
                                await websocket.close()
                                self.connection_states[websocket] = "TERMINATED"
                                return

                        # Cryptographic challenge (required for BOTH first registration and re-registrations)
                        # Proves possession of the private key before pinning or activating
                        nonce = secrets.token_bytes(32)
                        self.pending_challenges[websocket] = (clean_id, nonce, payload, host_pubkey.lower())
                        self.connection_states[websocket] = "CHALLENGED"
                        challenge_msg = ProtocolMessage.serialize("HOST_CHALLENGE", {
                            "device_id": clean_id,
                            "nonce": nonce.hex()
                        })
                        await websocket.send(challenge_msg)
                        continue

                    elif msg_type == "CONNECT_CLIENT":
                        target_id = payload.get("device_id", "").replace(" ", "")
                        host_ws = self.get_host(target_id)
                        if not host_ws:
                            resp = ProtocolMessage.serialize("HOST_NOT_FOUND", {"error": "Device is offline or ID not found"})
                            await websocket.send(resp)
                            continue

                        # Single-client policy: check if host already has an active connected client
                        existing_peer = self.routes.get(host_ws)
                        is_active = False
                        if existing_peer is not None:
                            ws_state = getattr(existing_peer, "state", None)
                            is_active = (getattr(ws_state, "name", "") == "OPEN") if ws_state is not None else getattr(existing_peer, "open", True)

                        if is_active:
                            logger.info(f"Host {target_id} is busy. Rejecting new client connection.")
                            resp = ProtocolMessage.serialize("HOST_BUSY", {
                                "error": "Host occupato in un'altra sessione",
                                "device_id": target_id
                            })
                            await websocket.send(resp)
                            continue
                        elif existing_peer is not None:
                            self.routes.pop(existing_peer, None)
                            self.routes.pop(host_ws, None)

                        role = "client"
                        self.connection_states[websocket] = "CLIENT"
                        self.routes[websocket] = host_ws
                        self.routes[host_ws] = websocket

                        host_salt = self.get_host_salt(target_id)
                        broker_token = self.get_host_broker_token(target_id)
                        host_pubkey = self.pinned_pubkeys.get(target_id, "")

                        # Forward notification to host with verified identity token
                        forward_msg = ProtocolMessage.serialize("CLIENT_ARRIVED", {
                            "salt": payload.get("salt", ""),
                            "client_id": target_id,
                            "client_pubkey": payload.get("client_pubkey", ""),
                            "broker_token": broker_token
                        })
                        await host_ws.send(forward_msg)

                        # Acknowledge client with host parameters and pinned public key
                        resp = ProtocolMessage.serialize("HOST_FOUND", {
                            "status": "ok",
                            "device_id": target_id,
                            "host_salt": host_salt,
                            "host_pubkey": host_pubkey
                        })
                        await websocket.send(resp)
                        continue

                    else:
                        logger.warning(f"Connection in INIT state attempted unauthorized message '{msg_type}'")
                        await websocket.send(ProtocolMessage.serialize("ERROR", {
                            "error": "UNAUTHORIZED_STATE",
                            "message": f"Cannot send '{msg_type}' before registering as host or connecting as client."
                        }))
                        await websocket.close()
                        self.connection_states[websocket] = "TERMINATED"
                        return

                # State 2: CHALLENGED host proving ownership of pinned public key
                elif state == "CHALLENGED":
                    if msg_type != "HOST_CHALLENGE_RESPONSE":
                        logger.warning(f"Expected HOST_CHALLENGE_RESPONSE in CHALLENGED state, got '{msg_type}'")
                        await websocket.send(ProtocolMessage.serialize("ERROR", {"error": "CHALLENGE_REQUIRED"}))
                        await websocket.close()
                        self.connection_states[websocket] = "TERMINATED"
                        return

                    challenge_data = self.pending_challenges.pop(websocket, None)
                    if not challenge_data:
                        await websocket.close()
                        return
                    clean_id, nonce, orig_payload, target_pubkey = challenge_data
                    sig_hex = payload.get("signature", "")
                    try:
                        sig_bytes = bytes.fromhex(sig_hex)
                        pub_bytes = bytes.fromhex(target_pubkey)
                        from src.security import SecurityManager
                        valid = SecurityManager.verify_data_signature(pub_bytes, nonce, sig_bytes)
                    except Exception as sig_err:
                        logger.warning(f"Signature verification exception: {sig_err}")
                        valid = False

                    if not valid:
                        logger.warning(f"Invalid host challenge signature for device {clean_id}")
                        await websocket.send(ProtocolMessage.serialize("ERROR", {
                            "error": "HOST_CHALLENGE_FAILED",
                            "message": "Cryptographic signature validation failed."
                        }))
                        await websocket.close()
                        self.connection_states[websocket] = "TERMINATED"
                        return

                    # Challenge verified! Pin public key (first registration or verified refresh) and persist
                    self.pinned_pubkeys[clean_id] = target_pubkey
                    self._save_pinned_pubkeys()

                    token = self.register_host(clean_id, websocket, orig_payload.get("host_salt", ""))
                    device_id = clean_id
                    role = "host"
                    self.connection_states[websocket] = "HOST"
                    resp = ProtocolMessage.serialize("HOST_REGISTERED", {
                        "status": "ok",
                        "device_id": clean_id,
                        "broker_token": token,
                        "pinned_pubkey": target_pubkey
                    })
                    await websocket.send(resp)
                    continue

                # State 3: Active HOST or CLIENT connection
                elif state in ("HOST", "CLIENT"):
                    # Security: strict rejection of reserved broker-internal message types
                    if msg_type in RESERVED_BROKER_TYPES:
                        logger.warning(f"Rejected reserved broker-internal message type '{msg_type}' from active {state}")
                        continue

                    # Forward legitimate payload to paired peer
                    target_ws = self.routes.get(websocket)
                    if target_ws:
                        # Strip any injected broker_token to prevent spoofing
                        if isinstance(payload, dict) and "broker_token" in payload:
                            clean_payload = dict(payload)
                            clean_payload.pop("broker_token", None)
                            raw = ProtocolMessage.serialize(msg_type, clean_payload, binary)
                        try:
                            await target_ws.send(raw)
                        except Exception as e:
                            logger.warning(f"Failed to relay message to peer: {e}")
                else:
                    logger.warning(f"Message received in terminated state {state}")
                    break

        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"ConnectionClosed in handle_connection for role={role}, dev={device_id}: {e}")
        except Exception as e:
            logger.error(f"Unhandled exception in handle_connection for role={role}, dev={device_id}: {e}", exc_info=True)
        finally:
            self.connection_states.pop(websocket, None)
            self.pending_challenges.pop(websocket, None)
            if role == "host" and device_id:
                self.unregister_host(device_id, websocket)
            peer = self.routes.pop(websocket, None)
            if peer and self.routes.get(peer) == websocket:
                del self.routes[peer]
                if role == "client":
                    host_dev_id = self.ws_to_device.get(peer, "")
                    token = self.get_host_broker_token(host_dev_id)
                    try:
                        disc_msg = ProtocolMessage.serialize("CLIENT_DISCONNECTED", {"broker_token": token})
                        await peer.send(disc_msg)
                    except Exception:
                        pass
                else:
                    try:
                        disc_msg = ProtocolMessage.serialize("CLIENT_DISCONNECTED", {})
                        await peer.send(disc_msg)
                    except Exception:
                        pass

    async def start(self):
        self.server = await websockets.serve(
            self.handle_connection,
            self.host,
            self.port,
            max_size=16 * 1024 * 1024,
            reuse_address=True
        )
        logger.info(f"Signaling broker listening on ws://{self.host}:{self.port}")

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

class InMemoryWebSocket:
    """Simulated bidirectional in-memory WebSocket transport using asyncio.Queue."""

    def __init__(self, incoming: asyncio.Queue, outgoing: asyncio.Queue):
        self._in_q = incoming
        self._out_q = outgoing
        self.open = True

    async def send(self, data):
        if not self.open:
            raise websockets.exceptions.ConnectionClosed(1000, "Connection closed")
        await self._out_q.put(data)

    async def recv(self):
        item = await self._in_q.get()
        if item is None:
            self.open = False
            raise websockets.exceptions.ConnectionClosed(1000, "Normal closure")
        return item

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.recv()
        except websockets.exceptions.ConnectionClosed:
            raise StopAsyncIteration

    async def close(self):
        if self.open:
            self.open = False
            await self._out_q.put(None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

class MockInMemoryBroker:
    """
    100% in-memory broker routing engine for automated testing.
    Uses the real SignalingBroker routing logic via in-memory queues,
    with zero OS TCP/UDP sockets, zero port binds, and zero OS network stack calls.
    """

    def __init__(self):
        self.broker = SignalingBroker(port=0)
        self._tasks: list[asyncio.Task] = []
        self._sockets: list[InMemoryWebSocket] = []

    def get_host(self, device_id: str):
        return self.broker.get_host(device_id)

    def connect_host(self) -> InMemoryWebSocket:
        q_h2b = asyncio.Queue()
        q_b2h = asyncio.Queue()
        host_ws = InMemoryWebSocket(incoming=q_b2h, outgoing=q_h2b)
        broker_side = InMemoryWebSocket(incoming=q_h2b, outgoing=q_b2h)
        self._sockets.extend([host_ws, broker_side])
        task = asyncio.create_task(self.broker.handle_connection(broker_side))
        self._tasks.append(task)
        return host_ws

    def connect_client(self) -> InMemoryWebSocket:
        q_c2b = asyncio.Queue()
        q_b2c = asyncio.Queue()
        client_ws = InMemoryWebSocket(incoming=q_b2c, outgoing=q_c2b)
        broker_side = InMemoryWebSocket(incoming=q_c2b, outgoing=q_b2c)
        self._sockets.extend([client_ws, broker_side])
        task = asyncio.create_task(self.broker.handle_connection(broker_side))
        self._tasks.append(task)
        return client_ws

    async def stop(self):
        for ws in self._sockets:
            try:
                await ws.close()
            except Exception:
                pass
        self._sockets.clear()
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

async def _run_broker(host: str, port: int):
    broker = SignalingBroker(host=host, port=port)
    await broker.start()
    try:
        # Keep running until cancelled or interrupted
        await asyncio.Future()
    except asyncio.CancelledError:
        pass
    finally:
        await broker.stop()

def build_parser():
    import argparse
    from src.ota import get_app_version
    parser = argparse.ArgumentParser(
        description="AetherDesk Signaling Broker - Zero-Knowledge E2EE Rendezvous Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python src/signaling_broker.py
  python src/signaling_broker.py --host 0.0.0.0 --port 8765
        """
    )
    parser.add_argument("-H", "--host", default="0.0.0.0", help="Host address to bind (default: 0.0.0.0)")
    parser.add_argument("-p", "--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    parser.add_argument("-v", "--version", action="version", version=f"AetherDesk Broker v{get_app_version()}")
    return parser

if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        asyncio.run(_run_broker(args.host, args.port))
    except KeyboardInterrupt:
        print("\nBroker arrestato dall'utente.")
