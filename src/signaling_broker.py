import asyncio
import logging
from typing import Dict, Any, Optional
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

    def unregister_host(self, device_id: str):
        clean_id = device_id.replace(" ", "")
        if clean_id in self.hosts:
            del self.hosts[clean_id]
        if clean_id in self.host_salts:
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

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            if role == "host" and device_id:
                self.unregister_host(device_id)
            peer = self.routes.pop(websocket, None)
            if peer and peer in self.routes:
                del self.routes[peer]

    async def start(self):
        self.server = await websockets.serve(self.handle_connection, self.host, self.port)
        logger.info(f"Signaling broker listening on ws://{self.host}:{self.port}")

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    broker = SignalingBroker(port=8765)
    asyncio.run(broker.start())
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        pass
