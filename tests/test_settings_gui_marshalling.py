"""Settings creation through the real GUI-thread marshalling (BeginInvoke) path.

The bridge thread reserves the Settings child, then posts native creation to the
GUI thread. These tests drive the real _open_reset_actions_from_bridge with a
single-thread executor standing in for the WinForms message loop.
"""
from __future__ import annotations

import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

from token_tracker import server, widget

MAIN = "http://127.0.0.1:8765/runway.html"


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        for handler in list(self.handlers):
            handler()


class _Model:
    def __init__(self, uid, title, url, **_kwargs):
        self.uid = uid
        self.events = SimpleNamespace(before_show=_Event(), closing=_Event(), closed=_Event())
        self.native = None
        self.destroyed = 0

    def _initialize(self, *, gui, server=None):
        return True

    def destroy(self):
        self.destroyed += 1


class _GuiThread:
    """Single-thread executor standing in for the WinForms GUI thread; records escaped exceptions."""

    def __init__(self):
        self.queue = []
        self.condition = threading.Condition()
        self.gate = threading.Event()
        self.gate.set()
        self.escaped = []
        self.posted = 0
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while True:
            with self.condition:
                while not self.queue:
                    self.condition.wait()
                callback = self.queue.pop(0)
            self.gate.wait()
            try:
                callback()
            except BaseException as error:  # what the message loop would surface as a dialog
                self.escaped.append(error)

    def post(self, callback):
        with self.condition:
            self.queue.append(callback)
            self.posted += 1
            self.condition.notify()

    def wait_for_queued(self):
        # The loop dequeues before waiting on the gate, so count posts rather than queue length.
        for _ in range(500):
            if self.posted:
                return True
            time.sleep(0.01)
        return False


class _MainNative:
    def __init__(self, gui_thread):
        self.gui_thread = gui_thread

    @property
    def InvokeRequired(self):
        return threading.current_thread() is not self.gui_thread.thread

    def BeginInvoke(self, callback):
        self.gui_thread.post(callback)


class _Gui:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def create_window(self, window):
        self.calls.append(window)
        if self.fail:
            raise RuntimeError("native creation failed")
        window.native = object()
        window.events.before_show.fire()


@pytest.fixture
def harness(monkeypatch):
    system = types.ModuleType("System")

    class _Func:
        def __getitem__(self, _type):
            return lambda function: function

    system.Func = _Func()
    system.Object = object
    monkeypatch.setitem(sys.modules, "System", system)
    monkeypatch.setattr(widget.webview, "Window", _Model)
    monkeypatch.setattr(widget.webview, "windows", [])

    def build(gui):
        monkeypatch.setattr(widget.webview, "guilib", gui)
        app = widget.WidgetApp.__new__(widget.WidgetApp)
        app._lifecycle = threading.RLock()
        app._reset_actions_lock = threading.Lock()
        app._shutdown_requested = threading.Event()
        app.quitting = False
        app.reset_actions_window = None
        app._reset_actions_token = None
        app._settings_bridge_authority = None
        app._settings_child_reservation = None
        app.server = SimpleNamespace(server_address=("127.0.0.1", 8765))
        gui_thread = _GuiThread()
        app.window = SimpleNamespace(native=_MainNative(gui_thread))
        authority = widget.BridgeAuthority({"/runway.html"})
        authority.enable(8765)
        authority.install(app.window, object())
        authority.activate(app.window, authority._adapter, MAIN)
        authority.begin_navigation(MAIN, 1)
        authority.complete_navigation(1, True, MAIN)
        app._bridge_authority = authority
        app._install_native_authority = lambda window, lease, **_k: lease.install(window, window.native)
        return app, gui_thread

    yield build
    server._SETTINGS_TOKEN = None


def _receipt(app):
    return app._bridge_authority.receive(app.window, "open_reset_actions", MAIN, MAIN,
                                         widget._MAIN_BRIDGE_OPERATIONS)


def _open_in_thread(app, receipt):
    results = []
    thread = threading.Thread(target=lambda: results.append(app._open_reset_actions_from_bridge(receipt, app.window)))
    thread.start()
    return thread, results


def test_reload_while_native_creation_is_queued_creates_nothing(harness):
    gui = _Gui()
    app, gui_thread = harness(gui)
    gui_thread.gate.clear()
    thread, results = _open_in_thread(app, _receipt(app))
    assert gui_thread.wait_for_queued()
    app._bridge_authority.begin_navigation(MAIN, 2)
    app._bridge_authority.complete_navigation(2, True, MAIN)
    gui_thread.gate.set()
    thread.join(10)
    assert results == [False]
    assert gui.calls == []
    assert app.reset_actions_window is None
    assert app._reset_actions_token is None and server._SETTINGS_TOKEN is None
    assert widget.webview.windows == []
    assert gui_thread.escaped == []


def test_second_manage_while_creation_is_queued_reuses_the_reservation(harness):
    gui = _Gui()
    app, gui_thread = harness(gui)
    gui_thread.gate.clear()
    thread, results = _open_in_thread(app, _receipt(app))
    assert gui_thread.wait_for_queued()
    token = app._reset_actions_token
    assert app._open_reset_actions_from_bridge(_receipt(app), app.window) is True
    assert app._reset_actions_token == token
    gui_thread.gate.set()
    thread.join(10)
    assert results == [True]
    assert len(gui.calls) == 1
    assert app.reset_actions_window is gui.calls[0]
    assert gui_thread.escaped == []


def test_native_creation_failure_stays_off_the_gui_message_loop(harness):
    gui = _Gui(fail=True)
    app, gui_thread = harness(gui)
    thread, results = _open_in_thread(app, _receipt(app))
    thread.join(10)
    assert not thread.is_alive()
    assert results and results[0] in (False, None)
    assert gui_thread.escaped == []
    assert app.reset_actions_window is None
    assert app._reset_actions_token is None and server._SETTINGS_TOKEN is None
    assert widget.webview.windows == []
