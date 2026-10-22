import sys
import os
import time
import asyncio
import logging
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Setup logging to ProgramData
log_dir = Path(os.environ.get("ProgramData", "C:\\ProgramData")) / "RemoteDesktop"
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(log_dir / "service.log"),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

from src.host_daemon import HostDaemon
from src.signaling_broker import SignalingBroker

class WindowsServiceManager:
    """Manages the host service execution in background with integrated broker."""

    def __init__(self):
        self.daemon = HostDaemon()
        self.broker = None

    async def _async_run(self):
        try:
            self.broker = SignalingBroker(host="0.0.0.0", port=8765)
            await self.broker.start()
            logging.info("Integrated Signaling Broker started on port 8765")
        except Exception as e:
            logging.warning(f"Signaling broker already running or port busy: {e}")

        await self.daemon.start()

    def run(self):
        logging.info("RemoteDesktop Windows Service starting...")
        self.daemon.prevent_sleep(True)
        try:
            asyncio.run(self._async_run())
            while self.daemon.running:
                time.sleep(1)
        except Exception as e:
            logging.error(f"Service error: {e}")
        finally:
            self.daemon.stop()
            if self.broker:
                try:
                    asyncio.run(self.broker.stop())
                except Exception:
                    pass
            logging.info("RemoteDesktop Windows Service stopped.")

if __name__ == "__main__":
    svc = WindowsServiceManager()
    svc.run()
