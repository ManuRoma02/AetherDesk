import asyncio
import logging
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

class SignalingBroker:
    """Zero-knowledge rendezvous & signaling broker for remote desktop connections."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765):
        self.host = host
        self.port = port
        self.hosts: Dict[str, Any] = {} # clean_device_id -> websocket
        self.host_salts: Dict[str, str] = {} # clean_device_id -> host_salt
        self.routes: Dict[Any, Any] = {} # ws -> peer_ws
        self.server = None

    def register_host(self, device_id: str, websocket: Any, host_salt: str = ""):
        clean_id = device_id.replace(" ", "")
        self.hosts[clean_id] = websocket
        self.host_salts[clean_id] = host_salt

    def unregister_host(self, device_id: str, websocket=None):
        clean_id = device_id.replace(" ", "")
        if clean_id in self.hosts:
            if websocket is None or self.hosts[clean_id] == websocket:
                del self.hosts[clean_id]
        if clean_id in self.host_salts:
            if websocket is None or clean_id not in self.hosts:
                del self.host_salts[clean_id]

    def get_host(self, device_id: str) -> Any:
        clean_id = device_id.replace(" ", "")
        return self.hosts.get(clean_id)

    def get_host_salt(self, device_id: str) -> str:
        clean_id = device_id.replace(" ", "")
        return self.host_salts.get(clean_id, "")

    async def handle_connection(self, websocket):
        device_id = None
        role = None # "host" or "client"
        try:
            async for raw in websocket:
                if isinstance(raw, str):
                    raw = raw.encode("utf-8")
                try:
                    msg_type, payload, binary = ProtocolMessage.deserialize(raw)
                except Exception as e:
                    logger.warning(f"Malformed message: {e}")
                    continue

                if msg_type == "REGISTER_HOST":
                    device_id = payload.get("device_id", "")
                    host_salt = payload.get("host_salt", "")
                    self.register_host(device_id, websocket, host_salt)
                    role = "host"
                    resp = ProtocolMessage.serialize("HOST_REGISTERED", {"status": "ok", "device_id": device_id})
                    await websocket.send(resp)

                elif msg_type == "CONNECT_CLIENT":
                    target_id = payload.get("device_id", "").replace(" ", "")
                    host_ws = self.get_host(target_id)
                    role = "client"
                    if host_ws:
                        host_salt = self.get_host_salt(target_id)
                        # Pair client and host for bidirectional message relay
                        self.routes[websocket] = host_ws
                        self.routes[host_ws] = websocket
                        client_salt = payload.get("salt", "")
                        forward_msg = ProtocolMessage.serialize("CLIENT_ARRIVED", {
                            "salt": client_salt,
                            "client_id": target_id
                        })
                        await host_ws.send(forward_msg)
                        resp = ProtocolMessage.serialize("HOST_FOUND", {
                            "status": "ok",
                            "device_id": target_id,
                            "host_salt": host_salt
                        })
                        await websocket.send(resp)
                    else:
                        resp = ProtocolMessage.serialize("HOST_NOT_FOUND", {"error": "Device is offline or ID not found"})
                        await websocket.send(resp)

                else:
                    # Forward all data and control packets directly to the paired peer
                    target_ws = self.routes.get(websocket)
                    if not target_ws:
                        # Fallback: check target_id in payload
                        target_id = payload.get("target_id", "").replace(" ", "")
                        if target_id:
                            target_ws = self.get_host(target_id)
                    if target_ws:
                        try:
                            await target_ws.send(raw)
                        except Exception as e:
                            logger.warning(f"Failed to relay message to peer: {e}")

        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"ConnectionClosed in handle_connection for role={role}, dev={device_id}: {e}")
        except Exception as e:
            logger.error(f"Unhandled exception in handle_connection for role={role}, dev={device_id}: {e}", exc_info=True)
        finally:
            if role == "host" and device_id:
                self.unregister_host(device_id, websocket)
            peer = self.routes.pop(websocket, None)
            if peer and self.routes.get(peer) == websocket:
                del self.routes[peer]
                try:
                    asyncio.create_task(peer.send(ProtocolMessage.serialize("CLIENT_DISCONNECTED", {})))
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

if __name__ == "__main__":
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
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        asyncio.run(_run_broker(args.host, args.port))
    except KeyboardInterrupt:
        print("\nBroker arrestato dall'utente.")
