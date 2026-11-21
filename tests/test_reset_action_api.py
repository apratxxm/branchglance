from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

from token_tracker import server
from token_tracker.limit_reset import LimitResetService, LimitSample
from token_tracker import provider_policy
from dataclasses import replace

ONE = "11111111-1111-4111-8111-111111111111"
TWO = "22222222-2222-4222-8222-222222222222"
SETTINGS_TOKEN = "reset-action-test-capability"


class Service:
    def __init__(self):
        self.action = {"mode": "off", "threadIds": [], "armed": False, "oneShot": True, "lastRun": None}
        self.configure_calls = []
        self.error = None

    def get_reset_action(self):
        return self.action

    def configure_reset_action(self, mode, thread_ids):
        self.configure_calls.append((mode, thread_ids))
        if self.error:
            raise self.error
        if mode == "continue" and not thread_ids:
            raise ValueError("select at least one chat")
        self.action = {"mode": mode, "threadIds": thread_ids, "armed": mode == "continue", "oneShot": True, "lastRun": None}
        return self.action


@pytest.fixture
def api(monkeypatch):
    policies = dict(provider_policy.POLICIES)
    policies["Codex"] = replace(policies["Codex"], send=True, send_origin="provider_env")
    monkeypatch.setattr(provider_policy, "POLICIES", policies)
    service = Service()
    server.set_settings_token(SETTINGS_TOKEN)
    monkeypatch.setattr(server.Handler, "reset_service", service)
    monkeypatch.setattr("token_tracker.reset_delivery.list_candidates", lambda: [{"id": ONE, "title": "<safe>", "status": "idle"}])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield service, httpd.server_port
    finally:
        httpd.shutdown(); httpd.server_close(); thread.join(timeout=2)
        server.set_settings_token(None)


def request(port, path, body=None, headers=None, raw=None, method=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    base = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}"}
    if (method or ("POST" if body is not None or raw is not None else "GET")) == "POST" and path == "/api/reset-action":
        base["X-Branchglance-Settings-Token"] = SETTINGS_TOKEN
    base.update(headers or {})
    if base.get("X-Branchglance-Settings-Token", object()) is None:
        base.pop("X-Branchglance-Settings-Token", None)
    encoded = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if encoded is not None:
        base.setdefault("Content-Type", "application/json")
        base.setdefault("Content-Length", str(len(encoded)))
    conn.request(method or ("POST" if body is not None or raw is not None else "GET"), path, body=encoded, headers=base)
    response = conn.getresponse(); data = json.loads(response.read()); conn.close()
    return response.status, data


@contextmanager
def serve_service(service):
    previous = server.Handler.reset_service
    server.set_settings_token(SETTINGS_TOKEN)
    server.Handler.reset_service = service
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_port
    finally:
        httpd.shutdown(); httpd.server_close(); thread.join(timeout=2)
        server.Handler.reset_service = previous
        server.set_settings_token(None)


def test_get_returns_action_and_candidates_and_candidate_failure_preserves_action(api, monkeypatch):
    service, port = api
    status, data = request(port, "/api/reset-action")
    assert status == 200 and data["action"] == service.action and data["threads"][0]["id"] == ONE
    monkeypatch.setattr("token_tracker.reset_delivery.list_candidates", lambda: (_ for _ in ()).throw(OSError()))
    status, failed = request(port, "/api/reset-action")
    assert status == 200 and failed["action"] == service.action and failed["threadsAvailable"] is False


@pytest.mark.parametrize("body", [
    {"mode": "greet", "threadIds": [], "prompt": "x"}, {"mode": "other", "threadIds": []},
    {"mode": "continue", "threadIds": ["bad"]}, {"mode": "continue", "threadIds": [ONE] * 21},
    {"mode": "continue", "threadIds": []}, {"mode": [], "threadIds": []},
    {"mode": "off", "threadIds": [ONE]},
])
def test_rejects_invalid_body_without_configuring(api, body):
    service, port = api
    assert request(port, "/api/reset-action", body)[0] == 400
    assert service.configure_calls == []


def test_configure_accepts_only_supported_fields_and_never_dispatches(api):
    service, port = api
    status, data = request(port, "/api/reset-action", {"mode": "continue", "threadIds": [ONE, TWO]})
    assert status == 200 and data["action"]["armed"]
    assert service.configure_calls == [("continue", [ONE, TWO])]


@pytest.mark.parametrize("header", [None, "wrong", "short", "non-ascii"])
def test_reset_action_rejects_missing_wrong_malformed_and_retired_tokens(api, header):
    service, port = api
    value = {"X-Branchglance-Settings-Token": header}
    if header == "short":
        value["X-Branchglance-Settings-Token"] = "x"
    if header == "non-ascii":
        value["X-Branchglance-Settings-Token"] = "\N{LATIN SMALL LETTER E WITH ACUTE}"
    status, body = request(port, "/api/reset-action", {"mode": "greet", "threadIds": []}, value)
    assert status == 403 and body["error"] == server._SETTINGS_AUTH_MESSAGE
    assert service.configure_calls == []


def test_reset_action_rejects_duplicate_capability_header(api):
    service, port = api
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.putrequest("POST", "/api/reset-action")
    conn.putheader("Host", f"127.0.0.1:{port}")
    conn.putheader("Origin", f"http://127.0.0.1:{port}")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("X-Branchglance-Settings-Token", SETTINGS_TOKEN)
    conn.putheader("X-Branchglance-Settings-Token", SETTINGS_TOKEN)
    body = json.dumps({"mode": "greet", "threadIds": []}).encode()
    conn.putheader("Content-Length", str(len(body)))
    conn.endheaders(body)
    response = conn.getresponse()
    payload = json.loads(response.read())
    conn.close()
    assert response.status == 403 and payload["error"] == server._SETTINGS_AUTH_MESSAGE
    assert service.configure_calls == []


@pytest.mark.parametrize("stale", ["another-capability-kind", SETTINGS_TOKEN + "-retired"])
def test_reset_action_rejects_wrong_kind_and_retired_capabilities(api, stale):
    service, port = api
    server.set_settings_token("new-settings-capability-after-reopen")
    status, body = request(port, "/api/reset-action", {"mode": "greet", "threadIds": []},
                           {"X-Branchglance-Settings-Token": stale})
    assert status == 403 and body["error"] == server._SETTINGS_AUTH_MESSAGE
    assert service.configure_calls == []


def test_close_settings_before_mutation_boundary_revokes_pending_request(api, monkeypatch):
    service, port = api
    entered, resume = threading.Event(), threading.Event()

    def pause(_self):
        entered.set()
        assert resume.wait(3)

    monkeypatch.setattr(server.Handler, "_before_reset_action_commit", pause)
    result = []
    request_thread = threading.Thread(target=lambda: result.append(
        request(port, "/api/reset-action", {"mode": "greet", "threadIds": []})))
    request_thread.start()
    assert entered.wait(3)
    server.set_settings_token(None)
    resume.set()
    request_thread.join(3)
    assert not request_thread.is_alive()
    assert result and result[0][0] == 403
    assert service.configure_calls == []


@pytest.mark.parametrize("headers,status", [
    ({"Origin": "http://example.com"}, 403), ({"Content-Type": "text/plain"}, 415),
])
def test_origin_and_content_type_guards(api, headers, status):
    _, port = api
    assert request(port, "/api/reset-action", {"mode": "greet", "threadIds": []}, headers)[0] == status


def test_oversize_persistence_and_inflight_errors(api):
    service, port = api
    status, _ = request(port, "/api/reset-action", raw=b" " * 4097)
    assert status == 413
    service.error = RuntimeError("settings file is read-only")
    assert request(port, "/api/reset-action", {"mode": "greet", "threadIds": []})[0] == 503
    service.error = RuntimeError("action currently running")
    assert request(port, "/api/reset-action", {"mode": "greet", "threadIds": []})[0] == 409


def test_missing_service_fails_closed(api, monkeypatch):
    _, port = api
    monkeypatch.setattr(server.Handler, "reset_service", None)
    assert request(port, "/api/reset-action")[0] == 503
    assert request(port, "/api/reset-action", {"mode": "greet", "threadIds": []})[0] == 503


@pytest.mark.parametrize("path", ["/api/quota", "/api/limit-reset", "/api/reset-action"])
def test_collection_denial_precedes_provider_cache_watcher_and_chat_calls(api, monkeypatch, path):
    service, port = api
    policies = dict(provider_policy.POLICIES)
    policies["Codex"] = replace(policies["Codex"], collect=False, show=False, send=False,
                                send_origin="provider_denied")
    monkeypatch.setattr(provider_policy, "POLICIES", policies)
    calls = []
    service.status = lambda: calls.append("status")
    service.get_reset_action = lambda: calls.append("action")
    monkeypatch.setattr("token_tracker.reset_delivery.list_candidates", lambda: calls.append("candidates"))
    monkeypatch.setattr("token_tracker.limit_reset.codex_quota_http_view", lambda: calls.append("quota"))

    status, body = request(port, path)
    assert status == 403 and "error" in body
    assert calls == []


def test_hidden_codex_policy_omits_cached_quota_watcher_events_and_thread_ids(api, monkeypatch):
    service, port = api
    service.action = {"schemaVersion": 2, "mode": "continue", "threadIds": [ONE], "armed": True,
                      "lastRun": {"targets": [{"threadId": TWO, "status": "queued"}]}}
    service.status = lambda: {"providers": {"Codex": {"lastStatus": {"threadId": ONE}}},
                              "events": [{"provider": "Codex", "threadId": TWO}]}
    policies = dict(provider_policy.POLICIES)
    policies["Codex"] = replace(policies["Codex"], show=False)
    monkeypatch.setattr(provider_policy, "POLICIES", policies)
    monkeypatch.setattr("token_tracker.reset_delivery.list_candidates",
                        lambda: pytest.fail("hidden provider must not enumerate candidates"))
    monkeypatch.setattr("token_tracker.limit_reset.codex_quota_http_view",
                        lambda: {"providers": {"Codex": {"lastStatus": {"threadId": ONE}}},
                                 "events": [{"provider": "Codex", "threadId": TWO}],
                                 "quotaCache": {"ready": True}})

    quota_status, quota = request(port, "/api/quota")
    watcher_status, watcher = request(port, "/api/limit-reset")
    action_status, action = request(port, "/api/reset-action")
    assert quota_status == watcher_status == 200 and action_status == 403
    assert quota["providers"] == {} and "events" not in quota
    assert watcher["providers"] == {} and watcher["events"] == []
    assert "action" not in action
    assert "threadId" not in json.dumps(action)


def test_reset_configuration_requires_explicit_send_before_service_call(api, monkeypatch):
    service, port = api
    policies = dict(provider_policy.POLICIES)
    policies["Codex"] = replace(policies["Codex"], send=False, send_origin="default")
    monkeypatch.setattr(provider_policy, "POLICIES", policies)
    status, body = request(port, "/api/reset-action", {"mode": "greet", "threadIds": []})
    assert status == 403 and "SEND" in body["error"]
    assert service.configure_calls == []


def test_off_mode_can_be_saved_without_send_permission(api, monkeypatch):
    service, port = api
    policies = dict(provider_policy.POLICIES)
    policies["Codex"] = replace(policies["Codex"], send=False, send_origin="default")
    monkeypatch.setattr(provider_policy, "POLICIES", policies)
    status, body = request(port, "/api/reset-action", {"mode": "off", "threadIds": []})
    assert status == 200 and body["action"]["mode"] == "off" and body["action"]["armed"] is False
    assert service.configure_calls == [("off", [])]


def test_service_provider_policy_error_maps_to_generic_forbidden(api):
    service, port = api
    service.error = provider_policy.ProviderPolicyError(f"internal denial for {ONE}")
    status, body = request(port, "/api/reset-action", {"mode": "continue", "threadIds": [ONE]})
    assert status == 403
    assert body["error"] == "Codex policy does not allow this reset action."
    assert ONE not in json.dumps(body)


def test_malformed_policy_fails_closed_before_quota_cache_call(api, monkeypatch):
    _, port = api
    monkeypatch.setattr(provider_policy, "POLICIES", {"Codex": None})
    monkeypatch.setattr("token_tracker.limit_reset.codex_quota_http_view",
                        lambda: pytest.fail("malformed policy must prevent quota reads"))
    status, body = request(port, "/api/quota")
    assert status == 403 and "unavailable" in body["error"]


def test_http_queue_route_is_removed_and_never_invokes_cli(api, monkeypatch):
    _, port = api
    policies = dict(provider_policy.POLICIES)
    policies["Codex"] = replace(policies["Codex"], send=False, send_origin="default")
    monkeypatch.setattr(provider_policy, "POLICIES", policies)
    calls = []
    monkeypatch.setattr(server, "queue_codex_message", lambda *_args: calls.append("queue"))
    status, body = request(port, "/api/queue", {"thread": ONE, "message": "hello"})
    assert status == 404 and body["error"] == "not found"
    assert calls == []


def test_provider_policy_endpoint_returns_raw_public_policy(api):
    _, port = api
    status, payload = request(port, "/api/provider-policy")
    assert status == 200 and payload == provider_policy.public_policies()
    assert set(payload) == {"schemaVersion", "generation", "providers", "origin", "policyFile", "warnings"}


def test_http_configuration_persists_until_fresh_available_reset_then_dispatches_once(tmp_path, monkeypatch):
    now = [1_800_000_000.0]
    queued = []
    policies = dict(provider_policy.POLICIES)
    policies["Codex"] = replace(policies["Codex"], send=True, send_origin="provider_env")
    monkeypatch.setattr(provider_policy, "POLICIES", policies)
    monkeypatch.setattr("token_tracker.reset_delivery.list_candidates", lambda: [{"id": ONE, "title": "Selected chat", "status": "idle"}])

    def make_service():
        return LimitResetService(
            sources={}, senders={"Codex": lambda _cwd: pytest.fail("continue action must not fall back to greeting")},
            state_path=tmp_path / "durable-state.json", log_path=tmp_path / "events.jsonl", cwd=tmp_path,
            enabled=True, clock=lambda: now[0],
            list_candidates=lambda limit=100: [{"id": ONE, "title": "Selected chat", "status": "idle"}],
            inspect_target=lambda _thread_id: {
                "exists": True, "archived": False, "running": False, "waiting": False,
                "knownIdle": True, "observedAt": now[0],
            },
            queue_continue=lambda thread_id: (queued.append(thread_id) or {"status": "queued"}),
            policies=policies,
        )

    service = make_service()
    service.check_provider("Codex", LimitSample(20, 20, now[0], now[0] + 10))
    with serve_service(service) as port:
        status, saved = request(port, "/api/reset-action", {"mode": "continue", "threadIds": [ONE]})
        assert status == 200 and saved["action"]["armed"] is True
        assert queued == [], "configuring over HTTP must never queue immediately"

    # Reload the durable setup before the reset arrives.
    service = make_service()
    assert service.get_reset_action()["threadIds"] == [ONE]
    now[0] += 10
    service.check_provider("Codex", LimitSample(0, 100, now[0], now[0] + 5 * 60 * 60))
    assert queued == [], "a blocked weekly quota cannot dispatch the selected chat"
    assert service.get_reset_action()["armed"] is True

    now[0] += 1
    available_sample = LimitSample(0, 20, now[0], now[0] + 5 * 60 * 60)
    service.check_provider("Codex", available_sample)
    assert queued == [ONE]
    with serve_service(service) as port:
        status, result = request(port, "/api/reset-action")
        assert status == 200
        action = result["action"]
        assert action["mode"] == "continue" and action["armed"] is False and action["oneShot"] is True
        generation = action["lastRun"]["armGeneration"]
        assert generation
        assert action["lastRun"]["targets"] == [{"threadId": ONE, "armGeneration": generation,
                                                  "attempts": 1, "status": "queued", "reason": None}]

    # Duplicate polls and a second restart retain the terminal one-shot outcome.
    service.check_provider("Codex", available_sample)
    restarted = make_service()
    restarted.check_provider("Codex", available_sample)
    assert queued == [ONE]
    assert restarted.get_reset_action()["lastRun"]["targets"][0]["status"] == "queued"


def test_http_queue_route_is_removed_for_collected_and_unknown_threads(api, monkeypatch):
    _, port = api
    calls = []
    monkeypatch.setattr(server, "queue_codex_message", lambda *_args: calls.append("queue"))
    monkeypatch.setattr(server, "_collected_codex_thread_ids", lambda: frozenset({TWO}))
    for thread in (ONE, TWO):
        status, body = request(port, "/api/queue", {"thread": thread, "message": "hello"})
        assert status == 404 and body["error"] == "not found"
    assert calls == []


def test_http_queue_route_never_uses_cached_thread_or_send_consent(api, monkeypatch):
    policies = dict(provider_policy.POLICIES)
    policies["Codex"] = replace(policies["Codex"], collect=True, show=False,
                                send=True, send_origin="provider_env")
    monkeypatch.setattr(provider_policy, "POLICIES", policies)
    monkeypatch.setattr(server, "_collected_codex_thread_ids", lambda: frozenset({ONE}))
    calls = []
    monkeypatch.setattr(server, "queue_codex_message", lambda *_args: calls.append("queue"))
    _, port = api
    status, body = request(port, "/api/queue", {"thread": ONE, "message": "hello"})
    assert status == 404 and body["error"] == "not found" and calls == []


def test_collected_set_ignores_claude_rows_even_with_uuid_ids(monkeypatch):
    monkeypatch.setattr(server.CACHE, "_value", {"providers": [
        {"name": "Claude", "chats": [{"id": ONE}]}, {"name": "Codex", "chats": [{"id": TWO}]}]})
    monkeypatch.setattr(server.LIVE_CACHE, "value", {"chats": [
        {"id": ONE, "key": f"Claude:{ONE}", "provider": "Claude"}, {"id": ONE, "key": f"Codex:{ONE}x"}]})
    assert server._collected_codex_thread_ids() == frozenset({TWO})


def test_http_open_route_is_removed_and_never_launches_app(api, monkeypatch):
    _, port = api
    launched = []
    monkeypatch.setattr(server.os, "startfile", lambda target: launched.append(target), raising=False)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    for thread in (ONE, TWO):
        conn.request("GET", f"/api/open?thread={thread}")
        response = conn.getresponse()
        assert response.status == 404
        response.read()
        assert launched == []
    conn.close()


def test_identity_reports_product_build_pid_and_state_root(api):
    import os
    _, port = api
    status, payload = request(port, "/api/identity")
    assert status == 200
    assert payload["product"] == "Branchglance" and payload["build"] == server.BUILD
    assert payload["pid"] == os.getpid() and payload["stateRoot"]
    assert set(payload["providerPolicies"]["providers"]) >= {"Codex"}
    status, _ = request(port, "/api/identity", headers={"Host": "evil.example"})
    assert status == 403
    status, _ = request(port, "/api/identity", headers={
        "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert status == 403
    status, local = request(port, "/api/identity")
    assert status == 200 and local["product"] == "Branchglance"


@pytest.mark.parametrize("path", ["/api/limit-reset"])
def test_sensitive_get_rejects_foreign_origin(api, path):
    _, port = api
    assert request(port, path, headers={"Origin": "https://evil.example"})[0] == 403


def test_settings_export_uses_native_save_dialog_and_filtered_csv_route(monkeypatch, tmp_path):
    from token_tracker import widget

    requested = []
    body = b"provider,coverageStatus\r\nCodex,complete\r\n"

    def export(path, query):
        requested.append((path, query))
        return 200, {"Content-Type": "text/csv; charset=utf-8"}, body

    collision = tmp_path / "example.txt.csv"
    collision.write_bytes(b"keep existing file")

    class SaveWindow:
        selected = str(tmp_path / "example.txt")

        def create_file_dialog(self, dialog_type, **kwargs):
            requested.append((dialog_type, kwargs))
            return self.selected

    monkeypatch.setattr(widget.ledger_service, "http_response", export)
    app = widget.WidgetApp.__new__(widget.WidgetApp)
    app.window = SaveWindow()
    api = widget.SettingsWindowApi(app.save_ledger_csv, lambda generation: generation == 1)
    assert api.save_ledger_csv(0) == {"ok": False, "error": "Settings window is no longer trusted."}
    assert requested == [], "a stale document cannot open the native file dialog"
    assert api.save_ledger_csv(1) == {"ok": True}
    assert requested[0] == (widget.ledger_service.EXPORT_PATH, {"range": ["all"], "format": ["csv"]})
    assert requested[1][0] == widget.webview.FileDialog.SAVE
    assert requested[1][1]["save_filename"] == "branchglance-ledger-all.csv"
    assert (tmp_path / "example.txt").read_bytes() == body
    assert collision.read_bytes() == b"keep existing file"

    SaveWindow.selected = None
    assert api.save_ledger_csv(1) == {"ok": False, "cancelled": True}
    dialog_calls = len(requested)
    monkeypatch.setattr(widget.ledger_service, "http_response", lambda *_args: (503, {}, b""))
    assert api.save_ledger_csv(1) == {"ok": False, "error": "Saved history is unavailable or starting."}
    assert len(requested) == dialog_calls, "an unavailable filtered export does not open the native dialog"
