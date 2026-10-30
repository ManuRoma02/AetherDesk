"""In-memory regressions for a responsive, coordinated ClientViewer shutdown."""

from unittest.mock import patch

from src.client_viewer import ClientViewerGUI


class FakeRoot:
    def __init__(self):
        self.callbacks = []
        self.destroyed = False

    def after(self, delay_ms, callback):
        self.callbacks.append((delay_ms, callback))

    def destroy(self):
        self.destroyed = True


class FakeThread:
    def __init__(self, *, target, daemon):
        self.target = target
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True


def test_main_window_close_starts_background_teardown_and_polls_completion():
    gui = object.__new__(ClientViewerGUI)
    gui.root = FakeRoot()
    gui._closing = False
    gui.host_daemon = None
    gui.host_thread = None
    gui.client = None
    gui.loop = None
    gui.network_thread = None
    thread = FakeThread(target=None, daemon=False)

    with patch("src.client_viewer.threading.Thread", return_value=thread) as make_thread:
        gui._on_main_window_close()
        gui._on_main_window_close()  # repeated WM close events must not start duplicate cleanup

    assert gui.root.destroyed is False
    assert thread.started is True
    assert make_thread.call_args.kwargs["daemon"] is True
    assert make_thread.call_count == 1
    assert len(gui.root.callbacks) == 1
    assert gui.root.callbacks[0][0] == 50

    thread.target = make_thread.call_args.kwargs["target"]
    thread.target()
    assert gui._close_complete.is_set()
    gui.root.callbacks.pop()[1]()
    assert gui.root.destroyed is True
