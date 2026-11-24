from __future__ import annotations

from types import SimpleNamespace
import json
import http.client
import subprocess
import threading
import time
from dataclasses import replace
from types import MappingProxyType

from token_tracker import provider_policy, server, widget


TRUSTED = "http://127.0.0.1:8765/runway.html"


def _bound_authority(paths=("/runway.html",)):
    authority = widget.BridgeAuthority({"/runway.html"})
    window = object()
    assert authority.install(window) is False
    authority.enable(8765)
    assert authority.install(window)
    assert authority.activate(window, None, TRUSTED)
    assert authority.begin_navigation(TRUSTED, 1) is True
    assert authority.complete_navigation(1, True, TRUSTED)
    return authority, window


def test_native_receipt_requires_bound_window_source_generation_and_public_operation():
    authority = widget.BridgeAuthority({"/runway.html"})
    window = object()
    assert authority.install(window) is False
    authority.enable(8765)
    assert authority.receive(window, "queue_message", TRUSTED, TRUSTED, {"queue_message"}) is None
    assert authority.install(window)
    assert authority.activate(window, None, TRUSTED)
    assert authority.begin_navigation(TRUSTED, 1) is True
    assert authority.complete_navigation(1, True, TRUSTED)
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
    assert authority.begin_navigation(TRUSTED, 2) is True
    assert authority.receive(window, "queue_message", TRUSTED, TRUSTED, {"queue_message"}) is None
    assert authority.complete_navigation(2, True, TRUSTED)
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


def test_busy_native_bridge_receipt_returns_retryable_failure_only_to_current_document():
    authority, window = _bound_authority()
    entered, release, replied = threading.Event(), threading.Event(), threading.Event()
    holder = threading.Thread(target=lambda: _hold_authority(authority, entered, release), daemon=True)
    holder.start()
    assert entered.wait(2)
    receipt, refusal, generation = authority.receive_status(
        window, "queue_message", TRUSTED, TRUSTED, widget._MAIN_BRIDGE_OPERATIONS)
    assert receipt is None and refusal == "contended"
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    responses = []

    def schedule(_window, operation, callback_id, result):
        responses.append((operation, callback_id, result))
        replied.set()

    app._schedule_native_reply = schedule
    app._dispatch_retryable_bridge_refusal(
        window, authority, "queue_message", ["thread", "message"], "1e-7",
        TRUSTED, TRUSTED, generation, widget._MAIN_BRIDGE_OPERATIONS)
    release.set()
    assert replied.wait(2)
    holder.join(2)
    assert responses == [("queue_message", "1e-7",
                          {"ok": False, "status": "retryable", "error": "The widget is busy. Try again."})]
    script = widget.WidgetApp._native_bridge_callback_script("queue_message", "1e-7", responses[0][2])
    assert '["queue_message"]?.["1e-7"]' in script, "callback identifiers use JSON-safe property access"
    assert "function\")cb" in script

    responses.clear();replied.clear()
    assert authority.begin_navigation(TRUSTED, 2) is True
    receipt, refusal, generation = authority.receive_status(
        window, "queue_message", TRUSTED, TRUSTED, widget._MAIN_BRIDGE_OPERATIONS)
    assert receipt is None and refusal == "suspended"
    app._dispatch_retryable_bridge_refusal(
        window, authority, "queue_message", ["thread", "message"], "1e-8",
        TRUSTED, TRUSTED, generation, widget._MAIN_BRIDGE_OPERATIONS)
    time.sleep(0.05)
    assert authority.cancel_navigation(2)
    assert replied.wait(2)
    assert responses == [("queue_message", "1e-8",
                          {"ok": False, "status": "retryable", "error": "The widget is busy. Try again."})]


def _hold_authority(authority, entered, release):
    with authority._lock:
        entered.set()
        release.wait(2)


def test_widget_queue_bridge_checks_document_and_calls_sender_once(monkeypatch):
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    app.quitting = False
    app._bridge_authority, app.window = _bound_authority()
    sent = []
    policy_allowed = [False]
    collected = [False]
    unknown_outcome = [False]
    monkeypatch.setattr(server, "_codex_queue_allowed", lambda: policy_allowed[0])
    monkeypatch.setattr(server, "_is_collected_codex_thread", lambda _thread: collected[0])
    def send(thread, message):
        sent.append((thread, message))
        if unknown_outcome[0]:
            raise subprocess.TimeoutExpired("codex queue", 15)
    monkeypatch.setattr(server, "queue_codex_message", send)
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
    unknown_outcome[0] = True
    retry_receipt = app._bridge_authority.receive(app.window, "queue_message", TRUSTED, TRUSTED,
                                                  widget._MAIN_BRIDGE_OPERATIONS)
    result = app.queue_message(thread, "hello again", retry_receipt, app.window)
    assert result["status"] == "unknown", "a timeout after sender dispatch remains an unknown outcome"
    assert sent[-1] == (thread, "hello again")
    assert app._bridge_authority.begin_navigation(TRUSTED, 1) is True
    assert app._bridge_authority.complete_navigation(1, True, TRUSTED)
    assert app.queue_message(thread, "stale", receipt, app.window)["ok"] is False
    assert sent == [(thread, "hello"), (thread, "hello again")]


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


def test_host_only_parent_never_admits_open_or_queue(monkeypatch):
    from types import SimpleNamespace
    parent = "01234567-89ab-cdef-0123-456789abcdef"
    child = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    host_edges = {f"Claude:{child}": f"Codex:{parent}"}
    overlay = {"instance": "test", "generation": 1, "seq": 1, "edges": host_edges, "titles": {}}
    monkeypatch.setattr(server, "CACHE", SimpleNamespace(value={"providers": [], "hostOverlay": overlay}))
    monkeypatch.setattr(server, "LIVE_CACHE", SimpleNamespace(value={"chats": [], "hostOverlay": overlay}))
    monkeypatch.setattr(server, "_codex_allows", lambda action: True)
    monkeypatch.setattr(server, "_codex_queue_allowed", lambda: True)
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    app.quitting = False
    app._bridge_authority, app.window = _bound_authority()
    for operation in ("open_thread", "queue_message"):
        receipt = app._bridge_authority.receive(app.window, operation, TRUSTED, TRUSTED,
                                                widget._MAIN_BRIDGE_OPERATIONS)
        result = (app.open_thread(parent, receipt, app.window) if operation == "open_thread"
                  else app.queue_message(parent, "hello", receipt, app.window))
        assert result["error"] == "unknown Codex thread"


def test_bridge_policy_matches_parent_display_and_collection_denials(monkeypatch):
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    cached_ids = frozenset({thread})
    monkeypatch.setattr(server, "_collected_codex_thread_ids", lambda: cached_ids)
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    app.quitting = False
    app._bridge_authority, app.window = _bound_authority()
    queued, opened = [], []
    monkeypatch.setattr(server, "queue_codex_message", lambda *args: queued.append(args))
    monkeypatch.setattr(widget.os, "startfile", lambda url: opened.append(url), raising=False)

    policies = dict(provider_policy.POLICIES)
    policies["Codex"] = replace(policies["Codex"], collect=True, show=False, send=True,
                                send_origin="provider_env")
    monkeypatch.setattr(provider_policy, "POLICIES", MappingProxyType(policies))
    queue_receipt = app._bridge_authority.receive(app.window, "queue_message", TRUSTED, TRUSTED,
                                                  widget._MAIN_BRIDGE_OPERATIONS)
    assert server._codex_queue_allowed() is False  # explicit SEND cannot override display off
    assert app.queue_message(thread, "hello", queue_receipt, app.window)["ok"] is False
    assert queued == []

    policies["Codex"] = replace(policies["Codex"], collect=False, show=True,
                                send=False, send_origin="provider_denied")
    monkeypatch.setattr(provider_policy, "POLICIES", MappingProxyType(policies))
    open_receipt = app._bridge_authority.receive(app.window, "open_thread", TRUSTED, TRUSTED,
                                                 widget._MAIN_BRIDGE_OPERATIONS)
    assert server._codex_allows("show") is False  # display=true is ineffective when collection is off
    assert app.open_thread(thread, open_receipt, app.window)["ok"] is False
    assert opened == []


def test_manage_settings_rechecks_native_receipt_after_waiting_for_lifecycle_gate(monkeypatch):
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    app._lifecycle = threading.RLock()
    app._shutdown_requested = threading.Event()
    app.quitting = False
    app.reset_actions_window = None
    app._reset_actions_token = None
    app._settings_bridge_authority = None
    app._reset_actions_lock = threading.Lock()
    app._bridge_authority, app.window = _bound_authority()
    receipt = app._bridge_authority.receive(app.window, "open_reset_actions", TRUSTED, TRUSTED,
                                             widget._MAIN_BRIDGE_OPERATIONS)
    create_calls = []
    monkeypatch.setattr(app, "_create_settings_native_window", lambda *args: create_calls.append(args))
    server.set_settings_token(None)
    entered, release = threading.Event(), threading.Event()

    def hold_lifecycle():
        with app._lifecycle:
            entered.set()
            assert release.wait(3)

    holder = threading.Thread(target=hold_lifecycle)
    holder.start(); assert entered.wait(2)
    result = []
    opener = threading.Thread(target=lambda: result.append(
        app._open_reset_actions_from_bridge(receipt, app.window)))
    opener.start()
    app._bridge_authority.begin_navigation(TRUSTED, 2)
    assert app._bridge_authority.complete_navigation(2, True, TRUSTED)
    release.set(); holder.join(2); opener.join(2)
    assert not opener.is_alive()
    assert result == [False] and create_calls == []
    assert server._SETTINGS_TOKEN is None, "a stale receipt never registers a Settings capability"


def test_reload_between_settings_reservation_and_native_creation_discards_the_reservation():
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    app._lifecycle = threading.RLock()
    app._reset_actions_lock = threading.Lock()
    app._shutdown_requested = threading.Event()
    app.quitting = False
    app.reset_actions_window = None
    app._reset_actions_token = None
    app._settings_bridge_authority = None
    app.server = SimpleNamespace(server_address=("127.0.0.1", 8765))
    app._bridge_authority, app.window = _bound_authority()
    receipt = app._bridge_authority.receive(app.window, "open_reset_actions", TRUSTED, TRUSTED,
                                             widget._MAIN_BRIDGE_OPERATIONS)
    reserved, continue_create = threading.Event(), threading.Event()
    child = SimpleNamespace(native_adapter=object())
    native_creations = []

    def create_settings(_api, authority, _revoke, _clear, register_child, _token):
        if not register_child(child):
            return None
        reserved.set()
        assert continue_create.wait(3)
        if not register_child(child, lambda: native_creations.append(child)):
            return None
        return child

    app._create_settings_native_window = create_settings
    result = []
    opener = threading.Thread(target=lambda: result.append(
        app._open_reset_actions_from_bridge(receipt, app.window)))
    opener.start()
    assert reserved.wait(3)
    assert app._settings_child_reservation is not None, "the single pending child owns the reservation"
    assert app._bridge_authority.begin_navigation(TRUSTED, 2) is True
    assert app._bridge_authority.complete_navigation(2, True, TRUSTED)
    continue_create.set()
    opener.join(3)
    assert not opener.is_alive()
    assert result == [False] and native_creations == []
    assert app._settings_child_reservation is None and app.reset_actions_window is None
    assert server._SETTINGS_TOKEN is None


def test_known_queue_refusal_waits_past_three_seconds_for_current_document():
    authority, window = _bound_authority()
    lock_held = threading.Event()
    reply_received = threading.Event()
    responses = []

    def hold_for_probe():
        with authority._lock:
            lock_held.set()
            time.sleep(3.35)

    holder = threading.Thread(target=hold_for_probe, daemon=True)
    holder.start()
    assert lock_held.wait(2)
    receipt, refusal, epoch = authority.receive_status(
        window, "queue_message", TRUSTED, TRUSTED, widget._MAIN_BRIDGE_OPERATIONS)
    assert receipt is None and refusal == "contended"
    app = widget.WidgetApp.__new__(widget.WidgetApp)

    def schedule(_window, operation, callback_id, result):
        responses.append((operation, callback_id, result))
        reply_received.set()

    app._schedule_native_reply = schedule
    app._dispatch_retryable_bridge_refusal(
        window, authority, "queue_message", ["thread", "message"], "3e-35",
        TRUSTED, TRUSTED, epoch, widget._MAIN_BRIDGE_OPERATIONS)
    assert reply_received.wait(8)
    holder.join(2)
    assert responses == [("queue_message", "3e-35",
                          {"ok": False, "status": "retryable", "error": "The widget is busy. Try again."})]


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
            self.load_url = self.initial_load
            self.webview.WebMessageReceived += self.on_script_notify
            self.webview.NavigationStarting += lambda *_args: None
            self.webview.CoreWebView2InitializationCompleted += self.on_webview_ready

        def original_notify(self, *_args):
            raise AssertionError("the unguarded receiver must be detached")

        def original_ready(self, sender, args):
            sender.CoreWebView2.NewWindowRequested += self.on_new_window_request
            self.load_url(TRUSTED)
            self.loaded_url = True

        def initial_load(self, target):
            assert self.webview.CoreWebView2.NewWindowRequested.handlers == [self.on_new_window_request]
            assert self.on_new_window_request.__name__ == "new_window_requested"
            assert self.webview.WebMessageReceived.handlers[0].__name__ == "guarded_notify"
            assert self.webview.NavigationStarting.handlers[-1] is not None
            assert authority._admitted
            nav = SimpleNamespace(Uri=target, NavigationId=1, IsUserInitiated=False, IsRedirected=False, Cancel=False)
            self.webview.NavigationStarting.fire(self.webview, nav)
            assert nav.Cancel is False
            self.webview.CoreWebView2.Source = target
            self.webview.NavigationCompleted.fire(self.webview,
                                                   SimpleNamespace(NavigationId=1, IsSuccess=True))

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
    assert not authority._admitted, "receipt admission waits until the guarded initializer is ready"
    assert adapter.on_script_notify is not adapter.original_notify
    assert len(adapter.webview.WebMessageReceived.handlers) == 1
    assert adapter.webview.WebMessageReceived.handlers[0].__name__ == "guarded_notify"
    adapter.webview.CoreWebView2InitializationCompleted.fire(
        adapter.webview, SimpleNamespace(IsSuccess=True))
    assert adapter.loaded_url
    assert authority.document_ready(model)
    assert adapter.webview.CoreWebView2.NewWindowRequested.handlers == [adapter.on_new_window_request]

    value_id = "1234567890123456"
    assert authority.receive(model, "queue_message", TRUSTED, TRUSTED,
                             widget._MAIN_BRIDGE_OPERATIONS) is not None
    wire = json.dumps(["queue_message", json.dumps(["01234567-89ab-cdef-0123-456789abcdef", "hello"]), value_id])
    args = SimpleNamespace(Source=TRUSTED, get_WebMessageAsJson=lambda: wire)
    receive_attempts = []
    real_receive = authority.receive_status
    monkeypatch.setattr(authority, "receive_status", lambda *values: (receive_attempts.append(values), real_receive(*values))[1])
    adapter.webview.WebMessageReceived.fire(adapter.webview, args)
    assert receive_attempts, "the native adapter parses and validates the WebMessageReceived envelope"
    assert len(calls) == 1
    receipt = calls[0][-1]
    assert receipt.window is model and receipt.source_url == TRUSTED and receipt.top_url == TRUSTED
    assert receipt.operation == "queue_message"

    opened = []
    monkeypatch.setattr("webbrowser.open", lambda value: opened.append(value))
    entered, release = threading.Event(), threading.Event()
    holder = threading.Thread(target=lambda: _hold_authority(authority, entered, release), daemon=True)
    holder.start(); assert entered.wait(2)
    busy_nav = SimpleNamespace(Uri=TRUSTED + "#t=capability-sentinel", NavigationId=2,
                               IsUserInitiated=True, IsRedirected=False, Cancel=False)
    adapter.webview.NavigationStarting.fire(adapter.webview, busy_nav)
    assert busy_nav.Cancel and opened == [], "authority contention cancels a local Settings URL without browser handoff"
    release.set(); holder.join(2)

    nav = SimpleNamespace(Uri="https://evil.example/redirect", IsUserInitiated=False, Cancel=False)
    adapter.webview.NavigationStarting.fire(adapter.webview, nav)
    assert nav.Cancel and opened == [], "script navigation is cancelled without a desktop launch"
    nav = SimpleNamespace(Uri="https://example.com/docs", IsUserInitiated=True, Cancel=False)
    adapter.webview.NavigationStarting.fire(adapter.webview, nav)
    assert nav.Cancel and opened == [], "arbitrary host-initiated external URLs are not handed off"
    explicit_url = "https://github.com/apratxxm/branchglance/issues/new?template=bug_report.yml&os=Windows"
    nav = SimpleNamespace(Uri=explicit_url, IsUserInitiated=True, IsRedirected=False, Cancel=False, NavigationId=3)
    adapter.webview.NavigationStarting.fire(adapter.webview, nav)
    assert nav.Cancel and opened == [explicit_url]
    popup = SimpleNamespace(IsUserInitiated=True, get_Uri=lambda: "codex://threads/01234567-89ab-cdef-0123-456789abcdef",
                            set_Handled=lambda value: setattr(popup, "handled", value))
    adapter.webview.CoreWebView2.NewWindowRequested.fire(adapter.webview.CoreWebView2, popup)
    assert popup.handled and len(opened) == 1, "Codex links stay behind open_thread"
    entered, release = threading.Event(), threading.Event()
    holder = threading.Thread(target=lambda: _hold_authority(authority, entered, release), daemon=True)
    holder.start(); assert entered.wait(2)
    popup = SimpleNamespace(IsUserInitiated=True, get_Uri=lambda: explicit_url,
                            set_Handled=lambda value: setattr(popup, "handled", value))
    adapter.webview.CoreWebView2.NewWindowRequested.fire(adapter.webview.CoreWebView2, popup)
    assert popup.handled and opened == [explicit_url], "popup contention never launches the browser"
    release.set(); holder.join(2)
    popup = SimpleNamespace(IsUserInitiated=True, get_Uri=lambda: explicit_url,
                            set_Handled=lambda value: setattr(popup, "handled", value))
    adapter.webview.CoreWebView2.NewWindowRequested.fire(adapter.webview.CoreWebView2, popup)
    assert popup.handled and opened == [explicit_url, explicit_url]

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
