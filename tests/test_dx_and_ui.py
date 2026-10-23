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

def test_standalone_binary_health_check():
    """Verify that the compiled standalone AetherDesk.exe executes cleanly without missing module crashes."""
    import os
    exe_path = os.path.join("dist", "AetherDesk.exe")
    if not os.path.exists(exe_path):
        pytest.skip("Standalone binary not found in dist/AetherDesk.exe - skipping binary test.")

    # 1. Test --version
    res_ver = subprocess.run([exe_path, "--version"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=5)
    assert res_ver.returncode == 0, f"Binary --version failed: {res_ver.stderr}"
    assert "AetherDesk" in res_ver.stdout
    assert "ModuleNotFoundError" not in res_ver.stderr
    assert "Traceback" not in res_ver.stderr

    # 2. Test --help
    res_help = subprocess.run([exe_path, "--help"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=5)
    assert res_help.returncode == 0, f"Binary --help failed: {res_help.stderr}"
    assert "--connect" in res_help.stdout
    assert "ModuleNotFoundError" not in res_help.stderr

def test_select_saved_device_logic():
    mock_gui = MagicMock()
    dev = {
        "id": "123456789",
        "name": "Office-PC",
        "password": "secret_pwd",
        "broker_url": "ws://192.168.1.100:8765"
    }
    ClientViewerGUI._select_saved_device(mock_gui, dev)
    mock_gui.combo_id.set.assert_called_with("123456789")
    mock_gui.entry_pwd.insert.assert_called_with(0, "secret_pwd")
    mock_gui.entry_broker.insert.assert_called_with(0, "ws://192.168.1.100:8765")
    mock_gui._on_status_update.assert_called_once()

def test_app_ota_availability(monkeypatch):
    """Verify OTA update checking and UI buttons are available from the GUI."""
    from src.ota import check_github_update
    has_newer, info = check_github_update()
    assert isinstance(has_newer, bool)
    assert isinstance(info, dict)
    assert "tag" in info
    assert "download_url" in info

    # Verify ClientViewerGUI exposes OTA UI controls
    mock_viewer = MagicMock()
    mock_viewer.ota_downloading = False
    mock_viewer.git_update_info = None
    mock_viewer._check_git_updates_background = MagicMock()

    ClientViewerGUI._on_git_update_click(mock_viewer)
    # When clicked without cached update, it triggers background check
    # (via threading or direct call)

def test_client_viewer_monitors_received():
    """Verify handling of incoming physical monitor list from host."""
    mock_gui = MagicMock()
    mock_gui.remote_monitors = []
    mock_gui.btn_mdual = MagicMock()
    
    mons = [
        {"id": 0, "name": "\\\\.\\DISPLAY1", "rect": [0, 0, 1920, 1080], "is_primary": True},
        {"id": 1, "name": "\\\\.\\DISPLAY2", "rect": [1920, 0, 1920, 1080], "is_primary": False},
        {"id": -1, "name": "Tutti i Monitor", "rect": [0, 0, 3840, 1080], "is_primary": False}
    ]
    
    # Simulate _on_monitors_received
    ClientViewerGUI._on_monitors_received(mock_gui, mons)
    assert len(mock_gui.remote_monitors) == 2
    assert mock_gui.remote_monitors[0]["id"] == 0
    assert mock_gui.remote_monitors[1]["id"] == 1

def test_client_viewer_dual_window_toggle():
    """Verify toggling dual window mode on and off."""
    mock_gui = MagicMock()
    mock_gui.dual_window_mode = False
    mock_gui.viewer_window_m2 = None
    
    # 1. Toggle ON: calls _open_dual_window
    ClientViewerGUI._toggle_dual_window(mock_gui)
    mock_gui._open_dual_window.assert_called_once()
    
    # 2. Toggle OFF: calls _close_dual_window
    mock_gui.dual_window_mode = True
    ClientViewerGUI._toggle_dual_window(mock_gui)
    mock_gui._close_dual_window.assert_called_once()

def test_client_viewer_calc_norm_coords_dual():
    """Verify coordinate transformation from 2 independent windows to virtual desktop space."""
    mock_gui = MagicMock()
    mock_gui.dual_window_mode = True
    mock_gui.viewer_window_m2 = MagicMock()
    
    mock_gui.canvas = MagicMock()
    mock_gui.canvas.winfo_width.return_value = 1920
    mock_gui.canvas.winfo_height.return_value = 1080
    mock_gui.current_img_dims = (1920, 1080)
    mock_gui._crop_m1 = (0, 0, 1920, 1080)
    
    mock_gui.canvas_m2 = MagicMock()
    mock_gui.canvas_m2.winfo_width.return_value = 1920
    mock_gui.canvas_m2.winfo_height.return_value = 1080
    mock_gui.current_img_dims_m2 = (1920, 1080)
    mock_gui._crop_m2 = (1920, 0, 3840, 1080)
    
    mock_gui._last_full_dims = (3840, 1080)
    
    # Event in Screen 1 (center: 960, 540)
    ev1 = MagicMock()
    ev1.x = 960
    ev1.y = 540
    norm_x1, norm_y1 = ClientViewerGUI._calc_norm_coords(mock_gui, ev1, mon_idx=0)
    assert abs(norm_x1 - 0.25) < 0.001
    assert abs(norm_y1 - 0.5) < 0.001
    
    # Event in Screen 2 (center: 960, 540) -> virt_x = 1920 + 960 = 2880 -> 2880 / 3840 = 0.75
    ev2 = MagicMock()
    ev2.x = 960
    ev2.y = 540
    norm_x2, norm_y2 = ClientViewerGUI._calc_norm_coords(mock_gui, ev2, mon_idx=1)
    assert abs(norm_x2 - 0.75) < 0.001
    assert abs(norm_y2 - 0.5) < 0.001


