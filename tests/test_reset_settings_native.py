from __future__ import annotations

import threading
import types

from token_tracker import widget


class Events:
    def __init__(self):
        self.closed = Callback()
        self.closing = Callback()
        self.loaded = Callback()
        self.before_show = Callback()


class Callback:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        for handler in self.handlers:
            handler()


class Child:
    def __init__(self):
        self.events = Events()
        self.calls = []

    def show(self): self.calls.append("show")
    def restore(self): self.calls.append("restore")
    def focus(self): self.calls.append("focus")


def test_window_api_privileged_settings_call_requires_native_receipt_dispatch():
    api = widget.WindowApi(None, None, None)
    assert api.open_reset_actions() is False
    calls = []
    api = widget.WindowApi(None, None, None, on_reset_actions=lambda: calls.append("open") or True)
    assert api.open_reset_actions() is False and calls == [], "direct API invocation has no native receipt"


def test_native_settings_window_reuses_and_clears_reference(monkeypatch):
    app = widget.WidgetApp()
    app.server = types.SimpleNamespace(server_address=("127.0.0.1", 8765))
    monkeypatch.setattr(widget.webview, "guilib", None, raising=False)
    monkeypatch.setattr(app, "_install_native_authority",
                        lambda window, authority, **_kwargs: authority.install(window))
    children = [Child(), Child()]
    calls = []
    tokens = []
    original_set_token = widget.server_module.set_settings_token

    def set_token(token):
        tokens.append(token)
        original_set_token(token)

    monkeypatch.setattr(widget.server_module, "set_settings_token", set_token)

    def create(*args, **kwargs):
        calls.append((args, kwargs))
        return children[len(calls) - 1]

    monkeypatch.setattr(widget.webview, "create_window", create)
    assert app.open_reset_actions() is True
    children[0].events.before_show.fire()
    assert len(calls) == 1
    assert calls[0][1]["width"] == 440 and calls[0][1]["height"] == 540
    assert calls[0][1]["min_size"] == (360, 360) and calls[0][1]["resizable"] is True
    # The Settings window gets only the CSV-save bridge, never the widget's WindowApi.
    bridge = calls[0][1]["js_api"]
    assert type(bridge) is widget.SettingsWindowApi
    assert {name for name in dir(bridge) if not name.startswith("_")} == {"save_ledger_csv"}
    first_url = calls[0][0][1]
    from urllib.parse import urlsplit, parse_qs
    parsed = urlsplit(first_url)
    assert parsed.query == "" and parsed.path == "/reset_actions.html"
    first_token = parse_qs(parsed.fragment).get("t", [None])[0]
    assert first_token and first_token == tokens[0] and len(first_token) >= 40
    assert widget.server_module._settings_token_matches(first_token)
    assert app.open_reset_actions() is True
    assert len(calls) == 1 and children[0].calls == ["show", "restore", "focus"]
    children[0].events.closing.fire()
    assert not widget.server_module._settings_token_matches(first_token), "closing revokes before closed cleanup"
    children[0].events.closed.fire()
    assert app.reset_actions_window is None
    assert app.open_reset_actions() is True
    assert len(calls) == 2
    children[1].events.before_show.fire()
    second_token = parse_qs(urlsplit(calls[1][0][1]).fragment).get("t", [None])[0]
    assert second_token and second_token != first_token
    assert not widget.server_module._settings_token_matches(first_token)
    assert widget.server_module._settings_token_matches(second_token)
    children[0].events.closing.fire()  # a delayed old close cannot revoke the new window
    children[0].events.closed.fire()
    assert widget.server_module._settings_token_matches(second_token)
    assert app.reset_actions_window is children[1]
    children[1].events.closing.fire()
    children[1].events.closed.fire()
    assert not widget.server_module._settings_token_matches(second_token)


def test_settings_native_guards_are_registered_before_pinned_gui_creates_the_window(monkeypatch):
    authority = widget.BridgeAuthority({"/reset_actions.html"})
    authority.enable(8765)
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    observations = []

    class Model:
        def __init__(self, uid, title, url, **kwargs):
            self.uid, self.title, self.url = uid, title, url
            self._js_api = kwargs["js_api"]
            self.events = Events()
            self.native = None

        def _initialize(self, *, gui, server=None):
            return True

    class Native:
        def __init__(self, window):
            self.window = window

    class Gui:
        renderer = "edgechromium"

        def create_window(self, window):
            assert window.events.before_show.handlers, "guard is bound before BrowserForm construction"
            assert window.events.closing.handlers, "close revocation is bound before FormClosing can fire"
            window.native = Native(window)
            window.events.before_show.fire()
            observations.append((window, authority._admitted))

    gui = Gui()
    windows = []
    monkeypatch.setattr(widget.webview, "Window", Model)
    monkeypatch.setattr(widget.webview, "windows", windows, raising=False)
    monkeypatch.setattr(widget.webview, "guilib", gui, raising=False)
    monkeypatch.setattr(app, "_install_native_authority",
                        lambda window, lease, **_kwargs: lease.install(window, window.native))
    child = app._create_settings_native_window(widget.SettingsWindowApi(lambda *_args: {}),
                                               authority, lambda: None, "fragment-token")
    assert child is observations[0][0]
    assert observations == [(child, True)]


def test_quit_destroys_settings_window_before_releasing_the_server(monkeypatch):
    order = []
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    for name, value in {"_shutdown_requested": threading.Event(), "_lifecycle": threading.Lock(),
                        "_notch_lock": threading.RLock(), "_reset_actions_lock": threading.Lock(),
                        "tray": None, "window": None,
                        "control": types.SimpleNamespace(close=lambda: order.append("control")),
                        "limit_reset": types.SimpleNamespace(stop=lambda: None)}.items():
        setattr(app, name, value)
    app.reset_actions_window = types.SimpleNamespace(destroy=lambda: order.append("settings"))
    app._reset_actions_token = "active-token"
    app._settings_bridge_authority = None
    original_clear_token = widget.server_module.clear_settings_token

    def clear_token(expected=None, on_cleared=None):
        order.append("token-clear")
        return original_clear_token(expected, on_cleared)

    monkeypatch.setattr(widget.server_module, "clear_settings_token", clear_token)
    widget.server_module.set_settings_token("active-token")
    app.server = types.SimpleNamespace(shutdown=lambda: order.append("server"), server_close=lambda: None)
    for name in ("_cancel_active_compact_drag", "_cancel_notch_transition", "save_bounds",
                 "_stop_inspection_hotkey", "_remove_control_nonce"):
        monkeypatch.setattr(app, name, lambda *a, **k: None, raising=False)
    app.quit()
    assert order[:3] == ["token-clear", "settings", "server"] and app.reset_actions_window is None
    assert not widget.server_module._settings_token_matches("active-token")
