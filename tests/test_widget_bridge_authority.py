from __future__ import annotations

from types import SimpleNamespace
import json
import http.client
import threading

from token_tracker import server, widget


TRUSTED = "http://127.0.0.1:8765/runway.html"


def test_bridge_authority_requires_bound_listener_trusted_document_and_live_generation():
    authority = widget.BridgeAuthority({"/runway.html"})
    assert not authority.admits(TRUSTED, TRUSTED, 0)  # before bind
    authority.enable(8765)
    assert authority.admits(TRUSTED, TRUSTED, 0)
    assert not authority.admits("http://127.0.0.1:8765/evil.html", TRUSTED, 0)
    assert not authority.admits("http://127.0.0.1:8765/runway.html?embedded=1", TRUSTED, 0)
    assert not authority.admits(TRUSTED, "http://127.0.0.1:8766/runway.html", 0)
    authority.navigation_started()
    assert not authority.admits(TRUSTED, TRUSTED, 0)  # same URL reload invalidates old calls
    assert authority.admits(TRUSTED, TRUSTED, 1)
    authority.revoke()
    assert not authority.admits(TRUSTED, TRUSTED, 1)


def test_widget_queue_bridge_checks_document_and_calls_sender_once(monkeypatch):
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    app.quitting = False
    app._bridge_authority = widget.BridgeAuthority({"/runway.html"})
    app._bridge_authority.enable(8765)
    app.window = SimpleNamespace(get_current_url=lambda: TRUSTED)
    sent = []
    monkeypatch.setattr(server, "_codex_queue_allowed", lambda: True)
    monkeypatch.setattr(server, "_is_collected_codex_thread", lambda _thread: True)
    monkeypatch.setattr(server, "queue_codex_message", lambda thread, message: sent.append((thread, message)))
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    assert app.queue_message(thread, "hello", 0)["ok"] is True
    assert sent == [(thread, "hello")]
    app._bridge_authority.navigation_started()
    assert app.queue_message(thread, "stale", 0)["ok"] is False
    assert sent == [(thread, "hello")]


def test_widget_open_bridge_refuses_invalid_or_uncollected_threads(monkeypatch):
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    app.quitting = False
    app._bridge_authority = widget.BridgeAuthority({"/runway.html"})
    app._bridge_authority.enable(8765)
    app.window = SimpleNamespace(get_current_url=lambda: TRUSTED)
    opened = []
    monkeypatch.setattr(server, "_codex_allows", lambda _action: True)
    monkeypatch.setattr(server, "_is_collected_codex_thread", lambda value: value.endswith("abcdef"))
    monkeypatch.setattr(widget.os, "startfile", lambda url: opened.append(url), raising=False)
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    assert app.open_thread(thread, 0) == {"ok": True}
    assert opened == [f"codex://threads/{thread}"]
    assert app.open_thread("not-a-thread", 0)["ok"] is False
    assert len(opened) == 1


def test_settings_capability_is_absent_from_snapshots_and_both_debug_reports(monkeypatch):
    from token_tracker import debug_report

    sentinel = "settings-capability-sentinel-for-privacy-tests"
    server.set_settings_token(sentinel)
    monkeypatch.setattr(server.CACHE, "get", lambda **_kwargs: {"providers": [], "snapshotCache": {"ready": True}})
    try:
        snapshot = server._insights_snapshot()
        report = debug_report.build_report(snapshot, running=False, port=8765)
        outputs = (json.dumps(snapshot), debug_report.serialize(report),
                   json.dumps(debug_report.report_to_dict(report)))
        assert all(sentinel not in output for output in outputs)
    finally:
        server.set_settings_token(None)


def test_settings_fragment_capability_is_not_in_http_urls_or_request_logs(capsys):
    sentinel = "settings-capability-sentinel-for-privacy-tests"
    server.set_settings_token(sentinel)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
        conn.request("GET", "/reset_actions.html", headers={"Host": f"127.0.0.1:{httpd.server_port}"})
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        conn.close()
    finally:
        httpd.shutdown(); httpd.server_close(); worker.join(timeout=3)
        server.set_settings_token(None)
    logs = capsys.readouterr()
    assert sentinel not in logs.out + logs.err
