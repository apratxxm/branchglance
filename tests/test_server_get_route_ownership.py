from types import SimpleNamespace
from io import BytesIO

import pytest

from token_tracker import server


ROUTES = {
    "_get_features_or_statusline": (
        "/api/features", "/api/statusline", "/api/claude-collection",
    ),
    "_get_provider_state": (
        "/api/provider-policy", "/api/debug-report", "/api/debug-report.json", "/api/identity",
    ),
    "_get_usage": (
        "/api/stats", "/api/insights", "/api/ledger/status", "/api/ledger",
        "/api/ledger/export", "/api/live", "/api/today",
    ),
    "_get_open": ("/api/open",),
    "_get_codex_state": ("/api/limit-reset", "/api/quota"),
    "_get_reset_action": ("/api/reset-action",),
}


class StubHandler:
    def __init__(self):
        self.responses = []
        self.headers = []
        self.wfile = BytesIO()

    def _send_json(self, payload, status=200):
        self.responses.append((status, payload))

    def _open_request_allowed(self):
        return False

    def _send_live_json(self, *_args):
        self.responses.append((200, {"ok": True}))

    def send_response(self, status):
        self.responses.append((status, None))

    def send_header(self, name, value):
        self.headers.append((name, value))

    def end_headers(self):
        pass


@pytest.mark.parametrize("owner,routes", ROUTES.items())
def test_get_helpers_own_exactly_their_dispatcher_routes(owner, routes, monkeypatch):
    handler = StubHandler()
    monkeypatch.setattr(server.CACHE, "get", lambda **kwargs: None)
    monkeypatch.setattr(server.LIVE_CACHE, "get", lambda **kwargs: None)
    monkeypatch.setattr(server, "today_usage", lambda: {})

    from token_tracker import ledger_service, provider_policy

    monkeypatch.setattr(provider_policy, "public_policies", lambda *args: {})
    monkeypatch.setattr(ledger_service, "status_payload", lambda: {})
    monkeypatch.setattr(ledger_service, "http_response", lambda *args: (200, {}, b""))
    monkeypatch.setattr(server, "_codex_allows", lambda _action: False)

    method = getattr(server.Handler, owner)
    for path in routes:
        parsed = SimpleNamespace(path=path, query="range=invalid" if path == "/api/insights" else "")
        assert method(handler, parsed) is True, (owner, path)
        assert handler.responses
        handler.responses.clear()
        handler.wfile = BytesIO()

    for other_owner, other_routes in ROUTES.items():
        if other_owner == owner:
            continue
        for path in other_routes:
            assert method(handler, SimpleNamespace(path=path, query="")) is False, (owner, path)


def test_get_unknown_path_falls_through_to_static_handler(monkeypatch):
    handler = object.__new__(server.Handler)
    handler.path = "/not-a-route"
    served = []
    monkeypatch.setattr(server.Handler, "_valid_local_host", lambda self: True)
    monkeypatch.setattr(server.SimpleHTTPRequestHandler, "do_GET", lambda self: served.append(self.path))

    server.Handler.do_GET(handler)

    assert served == ["/not-a-route"]


def _allow_success_paths(monkeypatch):
    from token_tracker import debug_report, insights, ledger_service, provider_policy

    snapshot = {"providers": [], "ok": True}
    monkeypatch.setattr(server.CACHE, "get", lambda **_kwargs: snapshot)
    monkeypatch.setattr(server.LIVE_CACHE, "get", lambda **_kwargs: (b"{}", None, None))
    monkeypatch.setattr(server, "today_usage", lambda: {})
    monkeypatch.setattr(provider_policy, "public_policies", lambda *_args: {"generation": 1})
    monkeypatch.setattr(ledger_service, "status_payload", lambda: {})
    monkeypatch.setattr(ledger_service, "http_response", lambda *_args: (200, {}, b"{}"))
    monkeypatch.setattr(server, "_codex_allows", lambda _action: True)
    monkeypatch.setattr(server, "_identity_payload", lambda: {"ok": True})
    monkeypatch.setattr(debug_report, "build_report", lambda _snapshot: {})
    monkeypatch.setattr(debug_report, "serialize", lambda _report: "report")
    monkeypatch.setattr(debug_report, "report_to_dict", lambda _report: {})
    monkeypatch.setattr(insights, "RANGES", {"30d"})
    monkeypatch.setattr(insights.INSIGHTS_CACHE, "get", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(server, "_insights_allowed_names", lambda: set())
    monkeypatch.setattr(server, "_insights_snapshot", lambda: snapshot)
    import token_tracker.limit_reset as limit_reset
    monkeypatch.setattr(limit_reset, "_runtime_dir", lambda: ".")
    monkeypatch.setattr(limit_reset, "codex_quota_http_view", lambda: {})
    monkeypatch.setattr(server, "_is_collected_codex_thread", lambda _thread: True)
    monkeypatch.setattr(server.os, "startfile", lambda _url: None, raising=False)
    monkeypatch.setattr(server.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ledger_service, "status_payload", lambda: {})
    monkeypatch.setattr(server.Handler, "reset_service", SimpleNamespace(
        status=lambda: {}, get_reset_action=lambda: {}))
    import token_tracker.reset_delivery as reset_delivery
    monkeypatch.setattr(reset_delivery, "list_candidates", lambda: [])


@pytest.mark.parametrize("owner,routes", ROUTES.items())
def test_get_helpers_own_success_exits(owner, routes, monkeypatch):
    _allow_success_paths(monkeypatch)
    handler = StubHandler()
    handler._open_request_allowed = lambda: True
    handler.reset_service = SimpleNamespace(status=lambda: {}, get_reset_action=lambda: {})
    method = getattr(server.Handler, owner)
    for path in routes:
        query = "thread=01234567-89ab-cdef-0123-456789abcdef" if path == "/api/open" else ""
        assert method(handler, SimpleNamespace(path=path, query=query)) is True, (owner, path)
        assert len(handler.responses) == 1, (owner, path, handler.responses)
        handler.responses.clear()
        handler.wfile = BytesIO()


# Each combination reaches different exits: same-origin rejection, Codex disabled, display-only disabled, success.
@pytest.mark.parametrize("origin_ok,codex", [(False, "none"), (True, "none"), (True, "collect"), (True, "all")])
def test_do_get_api_routes_never_fall_back_and_send_one_response(origin_ok, codex, monkeypatch):
    _allow_success_paths(monkeypatch)
    served = []
    monkeypatch.setattr(server.SimpleHTTPRequestHandler, "do_GET", lambda self: served.append(self.path))
    monkeypatch.setattr(server.Handler, "_valid_local_host", lambda self: True)
    monkeypatch.setattr(server.Handler, "_open_request_allowed", lambda self: origin_ok)
    monkeypatch.setattr(server, "_codex_allows", lambda action: codex == "all" or (codex == "collect" and action == "collect"))
    starts = []
    monkeypatch.setattr(server.Handler, "send_response", lambda self, status: starts.append(status))
    monkeypatch.setattr(server.Handler, "send_header", lambda *_args: None)
    monkeypatch.setattr(server.Handler, "end_headers", lambda self: None)
    monkeypatch.setattr(server.Handler, "_send_json", lambda self, *_args, **_kwargs: starts.append(200))
    monkeypatch.setattr(server.Handler, "_send_live_json", lambda self, *_args: starts.append(200))
    for routes in ROUTES.values():
        for path in routes:
            handler = object.__new__(server.Handler)
            handler.path = path + ("?thread=01234567-89ab-cdef-0123-456789abcdef" if path == "/api/open" else "")
            handler.wfile = BytesIO()
            starts.clear()
            server.Handler.do_GET(handler)
            assert served == [], (origin_ok, codex, path, served)
            assert len(starts) == 1, (origin_ok, codex, path, starts)
