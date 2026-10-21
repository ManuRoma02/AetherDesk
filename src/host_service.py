import sys
import os
import time
import asyncio
import logging
from pathlib import Path

# Setup logging to ProgramData
log_dir = Path(os.environ.get("ProgramData", "C:\\ProgramData")) / "RemoteDesktop"
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(log_dir / "service.log"),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

from src.host_daemon import HostDaemon

class WindowsServiceManager:
    """Manages the host service execution in background."""

    def __init__(self):
        self.daemon = HostDaemon()

    def run(self):
        logging.info("RemoteDesktop Windows Service starting...")
        self.daemon.prevent_sleep(True)
        try:
            asyncio.run(self.daemon.start())
            while self.daemon.running:
                time.sleep(1)
        except Exception as e:
            logging.error(f"Service error: {e}")
        finally:
            self.daemon.stop()
            logging.info("RemoteDesktop Windows Service stopped.")

if __name__ == "__main__":
    svc = WindowsServiceManager()
    svc.run()
