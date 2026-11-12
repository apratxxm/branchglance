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
    "_get_codex_state": ("/api/avatars", "/api/limit-reset", "/api/quota"),
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
