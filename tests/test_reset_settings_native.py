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
    def install_test_authority(window, authority, **_kwargs):
        adapter = getattr(window, "native_adapter", None)
        if adapter is None:
            adapter = window.native_adapter = object()
        if not authority.already_installed(window, adapter):
            authority.install(window, adapter)
        if not authority._admitted:
            authority.activate(window, adapter, "http://127.0.0.1:8765/reset_actions.html")
            authority.begin_navigation("http://127.0.0.1:8765/reset_actions.html", 1)
            authority.complete_navigation(1, True, "http://127.0.0.1:8765/reset_actions.html")
        return True

    monkeypatch.setattr(app, "_install_native_authority", install_test_authority)
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
                                               authority, lambda: None, lambda _child: None,
                                               lambda _child: True, "fragment-token")
    assert child is observations[0][0]
    assert observations == [(child, False)], "native guards bind before navigation; admission waits for the trusted document"


def test_manage_settings_reuses_a_registered_child_while_its_document_loads(monkeypatch):
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    app._lifecycle = threading.RLock()
    app._reset_actions_lock = threading.Lock()
    app._shutdown_requested = threading.Event()
    app.quitting = False
    app.reset_actions_window = None
    app._reset_actions_token = None
    app._settings_bridge_authority = None
    app.server = types.SimpleNamespace(server_address=("127.0.0.1", 8765))
    app._bridge_authority = widget.BridgeAuthority({"/runway.html"})
    app._bridge_authority.enable(8765)
    app.window = object()
    app._bridge_authority.install(app.window, object())
    main_url = "http://127.0.0.1:8765/runway.html"
    app._bridge_authority.activate(app.window, app._bridge_authority._adapter, main_url)
    app._bridge_authority.begin_navigation(main_url, 1)
    app._bridge_authority.complete_navigation(1, True, main_url)
    first_receipt = app._bridge_authority.receive(app.window, "open_reset_actions", main_url, main_url,
                                                   widget._MAIN_BRIDGE_OPERATIONS)
    created = []
    child_registered, release_create = threading.Event(), threading.Event()

    def create_child(_api, authority, revoke, clear, register_child, _token):
        child = Child()
        adapter = object()
        authority.install(child, adapter)
        child.native_adapter = adapter
        child.events.closing += revoke
        child.events.closed += lambda: clear(child)
        created.append(child)
        assert register_child(child)
        child_registered.set()
        assert release_create.wait(3)
        return child  # Simulate CoreWebView2 initialization still pending.

    monkeypatch.setattr(app, "_create_settings_native_window", create_child)
    first_result = []
    opener = threading.Thread(target=lambda: first_result.append(
        app._open_reset_actions_from_bridge(first_receipt, app.window)))
    opener.start()
    assert child_registered.wait(3)
    pending_child = created[0]
    authority = app._settings_bridge_authority
    registered_token = app._reset_actions_token
    assert authority.guards_installed(pending_child) and not authority.document_ready(pending_child)
    second_receipt = app._bridge_authority.receive(app.window, "open_reset_actions", main_url, main_url,
                                                    widget._MAIN_BRIDGE_OPERATIONS)
    assert app._open_reset_actions_from_bridge(second_receipt, app.window) is True
    assert created == [pending_child], "repeated Manage clicks reuse the pending child and capability"
    release_create.set(); opener.join(3)
    assert not opener.is_alive() and first_result == [True]
    assert pending_child.calls == []
    pending_child.events.closing.fire()
    assert not widget.server_module._settings_token_matches(registered_token)
    pending_child.events.closed.fire()


def test_old_settings_cleanup_cannot_clear_a_replacement_child_capability():
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    app._lifecycle = threading.RLock()
    app._reset_actions_lock = threading.Lock()
    old_authority = widget.BridgeAuthority({"/reset_actions.html"})
    new_authority = widget.BridgeAuthority({"/reset_actions.html"})
    app._settings_bridge_authority = new_authority
    app._reset_actions_token = "new-settings-token"
    old_authority.settings_token = "old-settings-token"
    new_authority.settings_token = "new-settings-token"
    widget.server_module.set_settings_token("new-settings-token")
    try:
        app._revoke_settings_authority(old_authority)
        assert old_authority._shutting_down
        assert widget.server_module._settings_token_matches("new-settings-token")
        assert app._reset_actions_token == "new-settings-token"
    finally:
        widget.server_module.set_settings_token(None)


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


def test_shutdown_intent_and_token_revocation_win_against_pending_settings_registration(monkeypatch):
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    app._lifecycle = threading.RLock()
    app._reset_actions_lock = threading.Lock()
    app._shutdown_requested = threading.Event()
    app.quitting = False
    app._reset_actions_token = None
    app._settings_bridge_authority = None
    app.reset_actions_window = None
    app.server = types.SimpleNamespace(server_address=("127.0.0.1", 8765))
    app._bridge_authority = widget.BridgeAuthority({"/runway.html"})
    app._bridge_authority.enable(8765)
    app.window = object()
    app._bridge_authority.install(app.window, object())
    app._bridge_authority.activate(app.window, app._bridge_authority._adapter,
                                   "http://127.0.0.1:8765/runway.html")
    app._bridge_authority.begin_navigation("http://127.0.0.1:8765/runway.html", 1)
    app._bridge_authority.complete_navigation(1, True, "http://127.0.0.1:8765/runway.html")
    receipt = app._bridge_authority.receive(app.window, "open_reset_actions",
                                            "http://127.0.0.1:8765/runway.html",
                                            "http://127.0.0.1:8765/runway.html",
                                            widget._MAIN_BRIDGE_OPERATIONS)
    registered, release = threading.Event(), threading.Event()
    child = Child()

    def create_child(_api, authority, revoke, clear, register_child, _token):
        authority.install(child, child)
        authority.activate(child, child, "http://127.0.0.1:8765/reset_actions.html")
        authority.begin_navigation("http://127.0.0.1:8765/reset_actions.html", 1)
        authority.complete_navigation(1, True, "http://127.0.0.1:8765/reset_actions.html")
        child.events.closing += revoke
        child.events.closed += lambda: clear(child)
        assert register_child(child)
        registered.set()
        assert release.wait(3)
        return child

    monkeypatch.setattr(app, "_create_settings_native_window", create_child)
    outcome = []
    opener = threading.Thread(target=lambda: outcome.append(
        app._open_reset_actions_from_bridge(receipt, app.window)))
    opener.start()
    assert registered.wait(3)
    registered_token = app._reset_actions_token
    assert registered_token and widget.server_module._settings_token_matches(registered_token)
    app._begin_shutdown()
    assert app._shutdown_requested.is_set() and app.quitting
    assert not widget.server_module._settings_token_matches(registered_token)
    assert app.open_reset_actions() is False, "shutdown intent blocks later Settings capability registration"
    release.set()
    opener.join(3)
    assert not opener.is_alive()
    assert outcome == [False] and app.reset_actions_window is None
    assert not authority_active(app)


def test_rejected_settings_child_is_detached_before_synchronous_gui_destroy():
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    app._lifecycle = threading.RLock()
    app._reset_actions_lock = threading.Lock()
    app._shutdown_requested = threading.Event()
    app.quitting = False
    app.reset_actions_window = None
    app._reset_actions_token = None
    app._settings_bridge_authority = None
    app._bridge_authority = None
    app.server = types.SimpleNamespace(server_address=("127.0.0.1", 8765))
    child = Child()
    close_finished = threading.Event()

    def destroy_synchronously_through_gui():
        def close_on_ui_thread():
            child.events.closing.fire()
            child.events.closed.fire()
            close_finished.set()
        native_thread = threading.Thread(target=close_on_ui_thread)
        native_thread.start()
        assert close_finished.wait(2), "native FormClosing must not wait on a lifecycle lock held by destroy"
        native_thread.join(2)

    child.destroy = destroy_synchronously_through_gui

    def create_then_quit(_api, authority, revoke, clear, register_child, _token):
        authority.install(child, object())
        child.events.closing += revoke
        child.events.closed += lambda: clear(child)
        assert register_child(child)
        app._begin_shutdown()
        return child

    app._create_settings_native_window = create_then_quit
    assert app.open_reset_actions() is False
    assert close_finished.is_set()
    assert app.reset_actions_window is None and app._settings_child_reservation is None
    assert widget.server_module._SETTINGS_TOKEN is None


def test_discarded_settings_child_is_destroyed_outside_lifecycle_lock():
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    app._lifecycle = threading.RLock()
    app._reset_actions_lock = threading.Lock()
    app._shutdown_requested = threading.Event()
    app.quitting = False
    app.reset_actions_window = None
    app._reset_actions_token = None
    app._settings_bridge_authority = None
    app.server = types.SimpleNamespace(server_address=("127.0.0.1", 8765))
    child = Child()
    closed = threading.Event()

    def destroy_synchronously_through_gui():
        def native_close():
            child.events.closing.fire()
            child.events.closed.fire()
            closed.set()
        native_thread = threading.Thread(target=native_close)
        native_thread.start()
        assert closed.wait(2), "native close callback must not wait on a lifecycle lock held by destroy"
        native_thread.join(2)

    child.destroy = destroy_synchronously_through_gui

    def create_then_quit(_api, authority, revoke, clear, register_child, _token):
        adapter = object()
        authority.install(child, adapter)
        child.events.closing += revoke
        child.events.closed += lambda: clear(child)
        assert register_child(child)
        app._begin_shutdown()
        return child

    app._create_settings_native_window = create_then_quit
    assert app.open_reset_actions() is False
    assert closed.is_set()
    assert app.reset_actions_window is None
    assert widget.server_module._SETTINGS_TOKEN is None


def authority_active(app):
    authority = app._settings_bridge_authority
    return bool(authority and authority._admitted and not authority._shutting_down)
