from __future__ import annotations

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
    child = Child()
    calls = []
    def create(*args, **kwargs):
        calls.append((args, kwargs))
        return child
    monkeypatch.setattr(widget.webview, "create_window", create)
    assert app.open_reset_actions() is True
    assert len(calls) == 1
    assert calls[0][1]["width"] == 440 and calls[0][1]["height"] == 540
    assert calls[0][1]["min_size"] == (360, 360) and calls[0][1]["resizable"] is True
    assert app.open_reset_actions() is True
    assert len(calls) == 1 and child.calls == ["show", "restore", "focus"]
    child.events.closed.fire()
    assert app.reset_actions_window is None
