from __future__ import annotations

import threading
import types

from token_tracker import widget


class Events:
    def __init__(self):
        self.closed = Callback()


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


def test_window_api_callback_is_optional_and_additive():
    api = widget.WindowApi(None, None, None)
    assert api.open_reset_actions() is False
    calls = []
    api = widget.WindowApi(None, None, None, on_reset_actions=lambda: calls.append("open") or True)
    assert api.open_reset_actions() is True and calls == ["open"]


def test_native_settings_window_reuses_and_clears_reference(monkeypatch):
    app = widget.WidgetApp()
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
    assert len(calls) == 1
    assert calls[0][1]["width"] == 440 and calls[0][1]["height"] == 540
    assert calls[0][1]["min_size"] == (360, 360) and calls[0][1]["resizable"] is True
    assert "js_api" not in calls[0][1]
    first_url = calls[0][0][1]
    from urllib.parse import urlsplit, parse_qs
    parsed = urlsplit(first_url)
    assert parsed.query == "" and parsed.path == "/reset_actions.html"
    first_token = parse_qs(parsed.fragment).get("t", [None])[0]
    assert first_token and first_token == tokens[0] and len(first_token) >= 40
    assert widget.server_module._settings_token_matches(first_token)
    assert app.open_reset_actions() is True
    assert len(calls) == 1 and children[0].calls == ["show", "restore", "focus"]
    children[0].events.closed.fire()
    assert app.reset_actions_window is None
    assert not widget.server_module._settings_token_matches(first_token)
    assert app.open_reset_actions() is True
    assert len(calls) == 2
    second_token = parse_qs(urlsplit(calls[1][0][1]).fragment).get("t", [None])[0]
    assert second_token and second_token != first_token
    assert not widget.server_module._settings_token_matches(first_token)
    assert widget.server_module._settings_token_matches(second_token)
    children[0].events.closed.fire()  # a delayed old close cannot revoke the new window
    assert widget.server_module._settings_token_matches(second_token)
    assert app.reset_actions_window is children[1]
    children[1].events.closed.fire()
    assert not widget.server_module._settings_token_matches(second_token)


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
    original_set_token = widget.server_module.set_settings_token

    def set_token(token):
        order.append("token-clear" if token is None else "token-set")
        original_set_token(token)

    monkeypatch.setattr(widget.server_module, "set_settings_token", set_token)
    original_set_token("active-token")
    app.server = types.SimpleNamespace(shutdown=lambda: order.append("server"), server_close=lambda: None)
    for name in ("_cancel_active_compact_drag", "_cancel_notch_transition", "save_bounds",
                 "_stop_inspection_hotkey", "_remove_control_nonce"):
        monkeypatch.setattr(app, name, lambda *a, **k: None, raising=False)
    app.quit()
    assert order[:3] == ["token-clear", "settings", "server"] and app.reset_actions_window is None
    assert not widget.server_module._settings_token_matches("active-token")
