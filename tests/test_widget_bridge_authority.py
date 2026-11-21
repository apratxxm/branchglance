from __future__ import annotations

from types import SimpleNamespace
import json
import http.client
import threading

from token_tracker import server, widget


TRUSTED = "http://127.0.0.1:8765/runway.html"


def _bound_authority(paths=("/runway.html",)):
    authority = widget.BridgeAuthority({"/runway.html"})
    window = object()
    assert authority.install(window) is False
    authority.enable(8765)
    assert authority.install(window)
    return authority, window


def test_native_receipt_requires_bound_window_source_generation_and_public_operation():
    authority = widget.BridgeAuthority({"/runway.html"})
    window = object()
    assert authority.install(window) is False
    authority.enable(8765)
    assert authority.receive(window, "queue_message", TRUSTED, TRUSTED, {"queue_message"}) is None
    assert authority.install(window)
    assert authority.receive(object(), "queue_message", TRUSTED, TRUSTED, {"queue_message"}) is None
    assert authority.receive(window, "queue_message", "http://127.0.0.1:8765/evil.html", TRUSTED,
                             {"queue_message"}) is None
    assert authority.receive(window, "queue_message", TRUSTED,
                             "http://127.0.0.1:8766/runway.html", {"queue_message"}) is None
    assert authority.receive(window, "_queue_message", TRUSTED, TRUSTED, {"_queue_message"}) is None
    assert authority.receive(window, "queue_message.nested", TRUSTED, TRUSTED,
                             {"queue_message.nested"}) is None
    receipt = authority.receive(window, "queue_message", TRUSTED, TRUSTED, {"queue_message"})
    assert receipt and authority.receipt_is_current(receipt, window, "queue_message")
    authority.navigation_started()  # same-URL reload invalidates the received native receipt
    assert not authority.receipt_is_current(receipt, window, "queue_message")
    authority.revoke()
    assert authority.receive(window, "queue_message", TRUSTED, TRUSTED, {"queue_message"}) is None


def test_bridge_operation_allowlists_match_the_only_public_api_methods():
    main = widget.WindowApi(None, None, None)
    settings = widget.SettingsWindowApi(lambda *_args: {"ok": True})
    main_public = {name for name in dir(main) if not name.startswith("_") and callable(getattr(main, name))}
    settings_public = {name for name in dir(settings) if not name.startswith("_") and callable(getattr(settings, name))}
    assert main_public == widget._MAIN_BRIDGE_OPERATIONS
    assert settings_public == widget._SETTINGS_BRIDGE_OPERATIONS == {"save_ledger_csv"}


def test_widget_queue_bridge_checks_document_and_calls_sender_once(monkeypatch):
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    app.quitting = False
    app._bridge_authority, app.window = _bound_authority()
    sent = []
    policy_allowed = [False]
    collected = [False]
    monkeypatch.setattr(server, "_codex_queue_allowed", lambda: policy_allowed[0])
    monkeypatch.setattr(server, "_is_collected_codex_thread", lambda _thread: collected[0])
    monkeypatch.setattr(server, "queue_codex_message", lambda thread, message: sent.append((thread, message)))
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    receipt = app._bridge_authority.receive(app.window, "queue_message", TRUSTED, TRUSTED,
                                            widget._MAIN_BRIDGE_OPERATIONS)
    assert app.queue_message("bad", "hello", receipt, app.window)["ok"] is False
    assert sent == []
    assert app.queue_message(thread, "hello", receipt, app.window)["error"] == "Explicit Codex SEND consent is required."
    policy_allowed[0] = True
    assert app.queue_message(thread, "hello", receipt, app.window)["error"] == "unknown Codex thread"
    assert sent == []  # a syntactically valid but uncollected ID has no delivery side effect
    collected[0] = True
    assert app.queue_message(thread, "hello", receipt, app.window)["ok"] is True
    assert sent == [(thread, "hello")]
    app._bridge_authority.navigation_started()
    assert app.queue_message(thread, "stale", receipt, app.window)["ok"] is False
    assert sent == [(thread, "hello")]


def test_widget_open_bridge_refuses_invalid_or_uncollected_threads(monkeypatch):
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    app.quitting = False
    app._bridge_authority, app.window = _bound_authority()
    opened = []
    show_allowed = [True]
    collected = [False]
    monkeypatch.setattr(server, "_codex_allows", lambda _action: show_allowed[0])
    monkeypatch.setattr(server, "_is_collected_codex_thread", lambda _value: collected[0])
    monkeypatch.setattr(widget.os, "startfile", lambda url: opened.append(url), raising=False)
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    receipt = app._bridge_authority.receive(app.window, "open_thread", TRUSTED, TRUSTED,
                                            widget._MAIN_BRIDGE_OPERATIONS)
    assert app.open_thread("not-a-thread", receipt, app.window)["ok"] is False
    show_allowed[0] = False
    assert app.open_thread(thread, receipt, app.window)["error"] == "Codex is not enabled."
    show_allowed[0] = True
    assert app.open_thread(thread, receipt, app.window)["error"] == "unknown Codex thread"
    collected[0] = True
    assert app.open_thread(thread, receipt, app.window) == {"ok": True}
    assert opened == [f"codex://threads/{thread}"]
    assert len(opened) == 1


def test_native_edge_adapter_is_guarded_before_navigation_and_receipts_are_allowlisted(monkeypatch):
    class NativeEvent:
        def __init__(self): self.handlers = []
        def __iadd__(self, handler): self.handlers.append(handler); return self
        def __isub__(self, handler): self.handlers.remove(handler); return self
        def fire(self, *args):
            for handler in list(self.handlers): handler(*args)

    class Control:
        def __init__(self):
            self.WebMessageReceived = NativeEvent()
            self.NavigationStarting = NativeEvent()
            self.NavigationCompleted = NativeEvent()
            self.CoreWebView2InitializationCompleted = NativeEvent()
            self.CoreWebView2 = type("Core", (), {"Source": TRUSTED, "NewWindowRequested": NativeEvent()})()

    class Adapter:
        def __init__(self, model):
            self.pywebview_window = model
            self.webview = Control()
            self.loaded_url = False
            self.on_script_notify = self.original_notify
            self.on_webview_ready = self.original_ready
            self.on_new_window_request = lambda _sender, _args: None
            self.webview.WebMessageReceived += self.on_script_notify
            self.webview.NavigationStarting += lambda *_args: None
            self.webview.CoreWebView2InitializationCompleted += self.on_webview_ready

        def original_notify(self, *_args):
            raise AssertionError("the unguarded receiver must be detached")

        def original_ready(self, sender, args):
            sender.CoreWebView2.NewWindowRequested += self.on_new_window_request
            self.loaded_url = True

    adapter_holder = {}
    model = SimpleNamespace()
    adapter = Adapter(model)
    adapter_holder["adapter"] = adapter
    model.native = SimpleNamespace(browser=adapter)
    api = SimpleNamespace()
    model._js_api = api
    authority = widget.BridgeAuthority({"/runway.html"})
    authority.enable(8765)
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    calls = []
    app._dispatch_native_bridge_call = lambda *args: calls.append(args)
    app._revoke_settings_authority = lambda _authority: None
    assert app._install_native_authority(model, authority) is True
    assert authority._admitted
    assert adapter.on_script_notify is not adapter.original_notify
    assert len(adapter.webview.WebMessageReceived.handlers) == 1
    assert adapter.webview.WebMessageReceived.handlers[0].__name__ == "guarded_notify"
    assert adapter.webview.NavigationStarting.handlers[-1] is not None

    adapter.webview.CoreWebView2InitializationCompleted.fire(
        adapter.webview, SimpleNamespace(IsSuccess=True))
    assert adapter.loaded_url
    assert adapter.on_new_window_request not in adapter.webview.CoreWebView2.NewWindowRequested.handlers
    assert len(adapter.webview.CoreWebView2.NewWindowRequested.handlers) == 1

    value_id = "1234567890123456"
    assert authority.receive(model, "queue_message", TRUSTED, TRUSTED,
                             widget._MAIN_BRIDGE_OPERATIONS) is not None
    wire = json.dumps(["queue_message", json.dumps(["01234567-89ab-cdef-0123-456789abcdef", "hello"]), value_id])
    args = SimpleNamespace(Source=TRUSTED, get_WebMessageAsJson=lambda: wire)
    receive_attempts = []
    real_receive = authority.receive
    monkeypatch.setattr(authority, "receive", lambda *values: (receive_attempts.append(values), real_receive(*values))[1])
    adapter.webview.WebMessageReceived.fire(adapter.webview, args)
    assert receive_attempts, "the native adapter parses and validates the WebMessageReceived envelope"
    assert len(calls) == 1
    receipt = calls[0][-1]
    assert receipt.window is model and receipt.source_url == TRUSTED and receipt.top_url == TRUSTED
    assert receipt.operation == "queue_message"

    opened = []
    monkeypatch.setattr("webbrowser.open", lambda value: opened.append(value))
    nav = SimpleNamespace(Uri="https://evil.example/redirect", IsUserInitiated=False, Cancel=False)
    adapter.webview.NavigationStarting.fire(adapter.webview, nav)
    assert nav.Cancel and opened == [], "script navigation is cancelled without a desktop launch"
    nav = SimpleNamespace(Uri="https://example.com/docs", IsUserInitiated=True, Cancel=False)
    adapter.webview.NavigationStarting.fire(adapter.webview, nav)
    assert nav.Cancel and opened == ["https://example.com/docs"]
    popup = SimpleNamespace(IsUserInitiated=True, get_Uri=lambda: "codex://threads/01234567-89ab-cdef-0123-456789abcdef",
                            set_Handled=lambda value: setattr(popup, "handled", value))
    adapter.webview.CoreWebView2.NewWindowRequested.fire(adapter.webview, popup)
    assert popup.handled and len(opened) == 1, "Codex links stay behind open_thread"

    for operation in ("_private", "queue_message.nested", "not_public"):
        bad = json.dumps([operation, json.dumps([]), value_id])
        adapter.webview.WebMessageReceived.fire(adapter.webview,
                                                SimpleNamespace(Source=TRUSTED, get_WebMessageAsJson=lambda b=bad: b))
    assert len(calls) == 1, "private, nested and non-public names never reach dispatch"


def test_settings_capability_is_absent_from_snapshots_and_both_active_debug_reports(monkeypatch):
    from token_tracker import debug_report

    sentinel = "settings-capability-sentinel-for-privacy-tests"
    server.set_settings_token(sentinel)
    monkeypatch.setattr(server.CACHE, "get", lambda **_kwargs: {"providers": [], "snapshotCache": {"ready": True}})
    try:
        snapshot = server._insights_snapshot()
        report = debug_report.build_report(snapshot, running=True, port=8765)
        outputs = (json.dumps(snapshot), debug_report.serialize(report),
                   json.dumps(debug_report.report_to_dict(report)))
        assert all(sentinel not in output for output in outputs)
    finally:
        server.set_settings_token(None)


def test_settings_capability_is_not_in_authenticated_request_urls_logs_or_active_reports(monkeypatch, capsys):
    sentinel = "settings-capability-sentinel-for-privacy-tests"
    server.set_settings_token(sentinel)
    monkeypatch.setattr(server.CACHE, "get", lambda **_kwargs: {"providers": [], "snapshotCache": {"ready": True}})
    monkeypatch.setattr(server.Handler, "reset_service", SimpleNamespace())
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
        host = f"127.0.0.1:{httpd.server_port}"
        origin = f"http://{host}"
        for path in ("/api/debug-report", "/api/debug-report.json"):
            conn.request("GET", path, headers={"Host": host, "Origin": origin})
            response = conn.getresponse()
            body = response.read().decode()
            assert response.status == 200
            assert sentinel not in body
        conn.request("POST", "/api/reset-action", body='{"mode":"invalid","threadIds":[]}', headers={
            "Host": host, "Origin": origin, "Content-Type": "application/json",
            "X-Branchglance-Settings-Token": sentinel,
        })
        response = conn.getresponse()
        assert response.status == 400
        assert sentinel not in response.read().decode()
        conn.close()
    finally:
        httpd.shutdown(); httpd.server_close(); worker.join(timeout=3)
        server.set_settings_token(None)
    logs = capsys.readouterr()
    assert sentinel not in logs.out + logs.err
