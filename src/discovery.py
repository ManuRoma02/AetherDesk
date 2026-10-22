"""
LAN Peer Auto-Discovery for AetherDesk.
Uses lightweight UDP beacons on port 8766 to announce and detect active AetherDesk hosts on the local network.
"""
import os
import sys
import json
import time
import socket
import logging
import threading
from typing import Optional, Dict, List

logger = logging.getLogger("LAN_Discovery")

DISCOVERY_PORT = 8766
BEACON_INTERVAL = 2.0  # seconds
PEER_TIMEOUT = 8.0      # seconds until a peer is marked offline

def get_primary_local_ip() -> str:
    """Detects the primary outbound IPv4 address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass
    return "127.0.0.1"


class LANBeaconBroadcaster:
    """Periodically broadcasts host presence over UDP broadcast."""

    def __init__(self, device_id: str, port: int = 8765):
        self.device_id = device_id.replace(" ", "")
        self.port = port
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._broadcast_loop, daemon=True, name="AetherBeaconBroadcaster")
        self._thread.start()

    def stop(self):
        self.running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _broadcast_loop(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception as e:
            logger.warning(f"Failed to create broadcast socket: {e}")
            return

        hostname = socket.gethostname()
        while self.running:
            try:
                ip = get_primary_local_ip()
                payload = json.dumps({
                    "app": "aetherdesk",
                    "device_id": self.device_id,
                    "ip": ip,
                    "port": self.port,
                    "name": hostname,
                    "ts": time.time()
                }).encode("utf-8")

                self._sock.sendto(payload, ("<broadcast>", DISCOVERY_PORT))
            except Exception as e:
                logger.debug(f"Broadcast error: {e}")

            time.sleep(BEACON_INTERVAL)


class LANBeaconListener:
    """Listens for UDP beacons from other AetherDesk instances on the local network."""

    def __init__(self):
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self._peers: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True, name="AetherBeaconListener")
        self._thread.start()

    def stop(self):
        self.running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _listen_loop(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # In Windows, bind to '' on discovery port
            self._sock.bind(("", DISCOVERY_PORT))
            self._sock.settimeout(1.0)
        except Exception as e:
            logger.warning(f"Failed to bind discovery listener: {e}")
            return

        while self.running:
            try:
                data, addr = self._sock.recvfrom(2048)
                msg = json.loads(data.decode("utf-8"))
                if msg.get("app") == "aetherdesk":
                    dev_id = str(msg.get("device_id", "")).replace(" ", "")
                    if dev_id:
                        ip = msg.get("ip") or addr[0]
                        port = msg.get("port", 8765)
                        peer_info = {
                            "device_id": dev_id,
                            "ip": ip,
                            "port": port,
                            "name": msg.get("name", "Remote PC"),
                            "broker_url": f"ws://{ip}:{port}",
                            "last_seen": time.time()
                        }
                        with self._lock:
                            self._peers[dev_id] = peer_info
            except socket.timeout:
                pass
            except Exception as e:
                logger.debug(f"Discovery listen error: {e}")

    def get_discovered_peers(self) -> List[dict]:
        """Returns list of currently active peers seen within PEER_TIMEOUT."""
        now = time.time()
        active = []
        with self._lock:
            # Clean expired
            expired = [k for k, v in self._peers.items() if now - v.get("last_seen", 0) > PEER_TIMEOUT]
            for k in expired:
                del self._peers[k]
            active = list(self._peers.values())
        return active

    def get_peer_broker_url(self, device_id: str) -> Optional[str]:
        """Finds active broker URL for a given 9-digit device ID on the LAN."""
        clean = device_id.replace(" ", "")
        now = time.time()
        with self._lock:
            peer = self._peers.get(clean)
            if peer and (now - peer.get("last_seen", 0) <= PEER_TIMEOUT):
                return peer.get("broker_url")
        return None


# Global singleton listener
_global_listener: Optional[LANBeaconListener] = None

def get_discovery_listener() -> LANBeaconListener:
    global _global_listener
    if _global_listener is None:
        _global_listener = LANBeaconListener()
        _global_listener.start()
    return _global_listener
