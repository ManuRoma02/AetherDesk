import sys
import subprocess
import pytest
from unittest.mock import MagicMock
from src.client_viewer import ClientViewerGUI

def test_cli_client_viewer_version():
    res = subprocess.run([sys.executable, "src/client_viewer.py", "--version"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "AetherDesk Client v" in res.stdout

def test_cli_client_viewer_help():
    res = subprocess.run([sys.executable, "src/client_viewer.py", "--help"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "--connect" in res.stdout
    assert "--password" in res.stdout
    assert "--broker" in res.stdout

def test_cli_host_daemon_version():
    res = subprocess.run([sys.executable, "src/host_daemon.py", "--version"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "AetherDesk Host Daemon v" in res.stdout

def test_cli_host_daemon_help():
    res = subprocess.run([sys.executable, "src/host_daemon.py", "--help"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "--broker" in res.stdout
    assert "--status" in res.stdout

def test_cli_host_daemon_status():
    res = subprocess.run([sys.executable, "src/host_daemon.py", "--status"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "AetherDesk Host Status:" in res.stdout
    assert "Device ID:" in res.stdout

def test_cli_signaling_broker_version():
    res = subprocess.run([sys.executable, "src/signaling_broker.py", "--version"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "AetherDesk Broker v" in res.stdout

def test_cli_signaling_broker_help():
    res = subprocess.run([sys.executable, "src/signaling_broker.py", "--help"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "--host" in res.stdout
    assert "--port" in res.stdout

def test_ui_center_window():
    mock_win = MagicMock()
    mock_win.winfo_screenwidth.return_value = 1920
    mock_win.winfo_screenheight.return_value = 1080

    ClientViewerGUI._center_window(mock_win, 490, 630)
    mock_win.geometry.assert_called_with("490x630+715+225")

def test_escape_fullscreen_exit():
    # Mock GUI to test _on_escape_press logic
    mock_gui = MagicMock()
    mock_gui.viewer_window.attributes.return_value = True # Is fullscreen
    mock_event = MagicMock()

    res = ClientViewerGUI._on_escape_press(mock_gui, mock_event)
    assert res == "break"
    mock_gui._toggle_fullscreen.assert_called_once()
