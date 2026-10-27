"""
AetherDesk Windows Host Service Manager.

Architectural Boundary & Session 0 Isolation:
- Headless Service Daemon: Runs HostDaemon and its embedded SignalingBroker on the service thread.
- Session 0 Boundary: A process in Session 0 (SYSTEM) has no interactive desktop surface or SendInput
  routing into the active user session (Session 1+). Remote screen capture (DXGI Desktop Duplication)
  and remote input injection require an interactive Session Agent process spawned into the user session
  via WTSQueryUserToken / CreateProcessAsUserW.
- Until a multi-process Session Agent bridge is implemented, running under Session 0 is limited to
  signaling/broker operations; full desktop streaming requires running within the active user session.
"""

import sys
import os
import time
import asyncio
import logging
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Setup logging to ProgramData
prog_data = Path(os.environ.get("ProgramData", "C:\\ProgramData"))
legacy_log = prog_data / "RemoteDesktop"
primary_log = prog_data / "AetherDesk"
log_dir = legacy_log if legacy_log.exists() and not primary_log.exists() else primary_log
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(log_dir / "service.log"),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

from src.host_daemon import HostDaemon

def check_session_0_boundary():
    """
    Check if running in Windows Session 0 (SYSTEM non-interactive service session).
    Must be called at entry BEFORE instantiating HostDaemon or WindowsServiceManager
    so that InputWorker and interactive input/capture systems are never initialized in Session 0.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            session_id = ctypes.c_uint32()
            if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)):
                if session_id.value == 0:
                    err_msg = (
                        "CRITICAL ERROR: AetherDesk Host Service cannot run in Windows Session 0 (SYSTEM). "
                        "Windows Session 0 lacks access to the interactive user desktop and input subsystem. "
                        "A multi-process Session Agent (CreateProcessAsUserW) is required for remote desktop "
                        "capture and control from a Windows Service, but is not implemented in this repository. "
                        "Aborting service startup."
                    )
                    logging.critical(err_msg)
                    sys.stderr.write(f"\n{err_msg}\n")
                    sys.exit(1)
        except Exception as e:
            if isinstance(e, SystemExit):
                raise

class WindowsServiceManager:
    """Manages the host service execution in background with integrated broker."""

    def __init__(self):
        check_session_0_boundary()
        self.daemon = HostDaemon()

    async def _async_run(self):
        await self.daemon.start()

    def run(self):
        logging.info("AetherDesk Host Service starting...")
        self.daemon.prevent_sleep(True)
        try:
            asyncio.run(self._async_run())
        except Exception as e:
            logging.error(f"Service error: {e}")
        finally:
            self.daemon.stop()
            logging.info("AetherDesk Host Service stopped.")

if __name__ == "__main__":
    check_session_0_boundary()
    svc = WindowsServiceManager()
    svc.run()
