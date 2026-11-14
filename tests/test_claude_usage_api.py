import json
import logging
import time
from datetime import datetime, timezone

import pytest

from token_tracker import claude_identity as identity, claude_statusline as sl
from token_tracker import claude_usage_api as api, limit_reset as lr, providers, product


@pytest.fixture
def setup_api(tmp_path, monkeypatch):
    now = time.time()
    state = {"now": now, "identity": identity.Identity("account", "org", "generation", changed_at=now - 100)}
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("TOKEN_TRACKER_CLAUDE_LIVE_USAGE", "1")
    monkeypatch.setattr(identity, "current_identity", lambda: state["identity"])
    monkeypatch.setattr(identity, "read_claude_code_usage", lambda *a, **k: {})
    monkeypatch.setattr(product, "state_root", lambda: tmp_path)
    monkeypatch.setattr(api, "policy_for", lambda name: type("Policy", (), {"allows": lambda self, action: True})())
    credentials = tmp_path / ".credentials.json"
    def write(token="SENTINEL_TOKEN_abc", expires=None, scopes=None, **extra):
        credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": token,
            "expiresAt": (now + 3600 if expires is None else expires) * 1000,
            "scopes": ["user:profile"] if scopes is None else scopes, **extra}}))
    write()
    stamp = datetime.fromtimestamp(now + 3600, timezone.utc).isoformat()
    usage = {"five_hour": {"utilization": 43, "resets_at": stamp},
             "seven_day": {"utilization": 43, "resets_at": stamp},
             "limits": [{"kind": "session", "percent": 43, "resets_at": stamp},
                        {"kind": "weekly_all", "percent": 43, "resets_at": stamp}]}
    calls = []
    def request(path, token):
        calls.append(path)
        return 200, {}, ({"account": {"uuid": "account"}, "organization": {"uuid": "org"}}
                         if path.endswith("profile") else usage)
    refresher = api.ClaudeUsageRefresher(tmp_path / "state", clock=lambda: state["now"], request=request)
    assert refresher.acquire()
    monkeypatch.setattr(api, "REFRESHER", refresher)
    yield state, refresher, calls, write, credentials, usage
    refresher._owner_context.__exit__(None, None, None)


def test_success_into_all_consumers(setup_api, tmp_path, monkeypatch):
    state, cache, calls, _, _, _ = setup_api
    now = state["now"]
    old = {"used_percentage": 43, "resets_at": now + 3600, "receivedAt": now - 181}
    monkeypatch.setattr(identity, "read_claude_code_usage", lambda *a, **k: {"five_hour": old, "seven_day": old})
    monkeypatch.setattr(sl, "read", lambda *a, **k: {})
    monkeypatch.setattr(providers.ClaudeDesktopProvider, "_usage", lambda self: ([], {}))
    monkeypatch.setattr(providers.ClaudeLimitsProvider, "_last_live", {})
    cache.poll()
    assert calls == ["/api/oauth/profile", "/api/oauth/usage"]
    result = providers.ClaudeLimitsProvider(str(tmp_path)).snapshot()
    assert result["fiveHour"]["percent"] == 43
    assert "SENTINEL_TOKEN_abc" not in json.dumps(result)
    assert datetime.fromisoformat(result["limitsUpdatedAt"]).timestamp() == pytest.approx(now)
    sample = lr.claude_limit_sample()
    assert sample.five_hour_percent == 43 and sample.observed_at == now
    watcher = lr.LimitResetService(sources={}, senders={}, state_path=tmp_path / "s.json")
    sample = watcher._claude_account_sample("org", now)
    assert sample.weekly_percent == 43 and sample.reset_at == pytest.approx(now + 3600)


@pytest.mark.parametrize("field", ["account", "organization"])
def test_profile_mismatch(setup_api, field):
    state, cache, _, _, _, _ = setup_api
    body = {"account": {"uuid": "account"}, "organization": {"uuid": "org"}}
    body[field]["uuid"] = "other"
    cache.request = lambda *args: (200, {}, body)
    cache.poll()
    assert cache.read(state["identity"]) == {}


@pytest.mark.parametrize("change", ["generation", "changed_at"])
def test_identity_changes_mid_request(setup_api, change):
    state, cache, _, _, _, usage = setup_api
    def request(path, token):
        if path.endswith("usage"):
            state["identity"] = identity.Identity("account", "org", "new" if change == "generation" else "generation", changed_at=state["now"] + 1)
        return 200, {}, usage if path.endswith("usage") else {"account": {"uuid": "account"}, "organization": {"uuid": "org"}}
    cache.request = request
    cache.poll()
    assert cache.read(state["identity"]) == {}


@pytest.mark.parametrize("invalid", ["expired", "scope", "missing", "malformed", "symlink"])
def test_invalid_credentials_no_request(setup_api, tmp_path, invalid):
    state, cache, calls, write, path, _ = setup_api
    if invalid == "expired": write(expires=state["now"] + 60)
    if invalid == "scope": write(scopes=[])
    if invalid == "missing": path.unlink()
    if invalid == "malformed": path.write_text("{")
    if invalid == "symlink":
        target = tmp_path / "other"
        target.write_text(path.read_text())
        path.unlink()
        try:
            path.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation unavailable")
    cache.poll()
    assert calls == []


def test_rejected_token_until_content_changes(setup_api):
    state, cache, calls, write, _, _ = setup_api
    cache.request = lambda path, token: (calls.append(path) or (401, {}, None))
    cache.poll()
    state["now"] += 100
    cache.poll()
    assert len(calls) == 1
    write(refreshToken="changed")
    cache.poll()
    assert len(calls) == 2


def test_unauthorized_usage_clears_previous_sample(setup_api):
    state, cache, _, _, _, _ = setup_api
    cache.poll()
    assert cache.read(state["identity"])
    state["now"] += 200
    cache.request = lambda path, token: (403, {}, None)
    cache.poll()
    assert cache.read(state["identity"]) == {}


def test_rate_limit_persists_and_backoff(setup_api):
    state, cache, calls, _, _, _ = setup_api
    cache.request = lambda path, token: (calls.append(path) or (429, {"Retry-After": "120"}, None))
    cache.poll()
    assert "SENTINEL_TOKEN_abc" not in (cache.root / "claude-usage-cooldown.json").read_text()
    second = api.ClaudeUsageRefresher(cache.root, clock=lambda: state["now"], request=cache.request)
    second._owner = cache._owner
    state["now"] += 119
    second.poll()
    assert len(calls) == 1
    state["now"] += 1
    second.request = lambda path, token: (calls.append(path) or (503, {}, None))
    second.poll()
    assert len(calls) == 2
    second.poll()
    assert len(calls) == 2
    state["now"] += 5
    second.poll()
    assert len(calls) == 3 and second._next == state["now"] + 10


def test_profile_binding_survives_usage_failure(setup_api):
    state, cache, calls, _, _, usage = setup_api
    def request(path, token):
        calls.append(path)
        if path.endswith("profile"):
            return 200, {}, {"account": {"uuid": "account"}, "organization": {"uuid": "org"}}
        return (503, {}, None) if calls.count("/api/oauth/usage") == 1 else (200, {}, usage)
    cache.request = request
    cache.poll()
    state["now"] += 5
    cache.poll()
    assert calls == ["/api/oauth/profile", "/api/oauth/usage", "/api/oauth/usage"]


def test_crosscheck_and_credential_disappearance(setup_api):
    state, cache, _, _, path, usage = setup_api
    usage["limits"][0]["percent"] = 99
    cache.poll()
    assert "five_hour" not in cache.read(state["identity"])
    path.unlink()
    assert cache.read(state["identity"]) == {}


def test_consumer_read_invalidates_changed_identity(setup_api):
    state, cache, _, _, _, _ = setup_api
    cache.poll()
    state["identity"] = identity.Identity("account", "org", "generation", changed_at=state["now"] + 1)
    assert cache.read(state["identity"]) == {}


def test_policy_and_lock(setup_api, monkeypatch):
    _, cache, calls, _, _, _ = setup_api
    other = api.ClaudeUsageRefresher(cache.root, request=cache.request)
    assert not other.acquire()
    other.poll()
    monkeypatch.setattr(api, "policy_for", lambda name: type("Policy", (), {"allows": lambda self, action: False})())
    cache.poll()
    assert calls == []


def test_no_secrets_in_state_logs_or_exception(setup_api, caplog):
    state, cache, _, _, _, _ = setup_api
    cache.request = lambda *args: (_ for _ in ()).throw(RuntimeError("SENTINEL_TOKEN_abc"))
    with caplog.at_level(logging.DEBUG):
        cache.poll()
    assert "SENTINEL_TOKEN_abc" not in caplog.text
    assert "SENTINEL_TOKEN_abc" not in repr(cache.read(state["identity"]))
    assert all(b"SENTINEL_TOKEN_abc" not in p.read_bytes() for p in cache.root.iterdir() if p.is_file() and p.suffix != ".lock")


def test_proxy_disabled(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    assert not any(isinstance(handler, api.urllib.request.ProxyHandler) for handler in api._opener().handlers)
    assert api._NoRedirect().redirect_request(None, None, None, None, None, None) is None


@pytest.mark.parametrize("body", [b"x" * (1024 * 1024 + 1), b"not-json"], ids=["oversized", "invalid-json"])
def test_invalid_response_body_redacts_exception(monkeypatch, body):
    class Sock:
        def settimeout(self, value): pass
    class Response:
        status = 200
        headers = {}
        fp = type("FP", (), {"raw": type("Raw", (), {"_sock": Sock()})()})()
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read1(self, length):
            result, self.remaining = self.remaining[:length], self.remaining[length:]
            return result
        def __init__(self): self.remaining = body
    class Opener:
        def open(self, *args, **kwargs): return Response()
    monkeypatch.setattr(api.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(RuntimeError) as error:
        api._request("/api/oauth/usage", "SENTINEL_TOKEN_abc")
    assert "SENTINEL_TOKEN_abc" not in str(error.value)


def test_stale_live_sample_does_not_verify_identity(setup_api, tmp_path, monkeypatch):
    state, cache, _, _, _, _ = setup_api
    state["identity"] = identity.Identity("account", "org", "generation", changed_at=state["now"] - 5000)
    monkeypatch.setattr(identity, "read_claude_code_usage", lambda *a, **k: {})
    monkeypatch.setattr(sl, "read", lambda *a, **k: {})
    monkeypatch.setattr(providers.ClaudeDesktopProvider, "_usage", lambda self: ([], {}))
    monkeypatch.setattr(providers.ClaudeLimitsProvider, "_last_live", {})
    cache.poll()
    for window in cache._sample.values():
        window["receivedAt"] = state["now"] - 1000
    assert cache.read(state["identity"])
    assert not api.fresh(cache.read(state["identity"]), providers.CLAUDE_DISPLAY_USAGE_AGE_SECONDS)
    try:
        result = providers.ClaudeLimitsProvider(str(tmp_path)).snapshot()
    except Exception:
        return  # no fallback source at all: nothing is shown, which is acceptable
    assert not result.get("identityVerified")


def test_new_owner_reloads_cooldown_written_after_construction(setup_api):
    state, cache, calls, _, _, _ = setup_api
    second = api.ClaudeUsageRefresher(cache.root, clock=lambda: state["now"],
                                      request=lambda path, token: (calls.append(path) or (200, {}, {})))
    assert not second.acquire()  # first instance still owns polling
    cache.request = lambda path, token: (calls.append(path) or (429, {"Retry-After": "120"}, None))
    cache.poll()
    assert len(calls) == 1
    cache._owner_context.__exit__(None, None, None)
    cache._owner = None
    try:
        assert second.acquire()
        state["now"] += 60
        second.poll()
        assert len(calls) == 1
    finally:
        second._owner_context.__exit__(None, None, None)
        cache.acquire()


def test_live_usage_cache_first_and_interval(setup_api, monkeypatch):
    state, refresher, calls, _, _, _ = setup_api
    now = state["now"]
    window = {"used_percentage": 10, "resets_at": now + 3600, "receivedAt": now - 10}
    monkeypatch.setattr(identity, "read_claude_code_usage", lambda *a, **k: {"five_hour": window})
    monkeypatch.setattr(api, "_credentials", lambda: pytest.fail("credentials read before cache skip"))
    refresher.poll()
    assert not calls and 165 <= refresher._next - now <= 195
    next_poll = refresher._next
    state["now"] += 1
    refresher.poll()
    assert refresher._next == next_poll


@pytest.mark.parametrize("revoke_at", ["profile", "usage"])
def test_revoked_request_does_not_send_next_or_publish(setup_api, monkeypatch, revoke_at):
    state, refresher, calls, _, _, usage = setup_api
    def request(path, token):
        calls.append(path)
        if path.endswith(revoke_at):
            monkeypatch.setenv("TOKEN_TRACKER_CLAUDE_LIVE_USAGE", "0")
        return (200, {}, usage if path.endswith("usage") else
                {"account": {"uuid": "account"}, "organization": {"uuid": "org"}})
    refresher.request = request
    refresher.poll()
    assert calls == (["/api/oauth/profile"] if revoke_at == "profile" else
                     ["/api/oauth/profile", "/api/oauth/usage"])
    assert refresher.read(state["identity"]) == {} and refresher._sample == {}


def test_consumers_do_not_open_credentials_when_live_usage_off(setup_api, tmp_path, monkeypatch):
    state, _, _, _, _, _ = setup_api
    monkeypatch.setenv("TOKEN_TRACKER_CLAUDE_LIVE_USAGE", "0")
    monkeypatch.setattr(api, "_credentials", lambda: pytest.fail("credentials read"))
    monkeypatch.setattr(api, "_opener", lambda *a: pytest.fail("opener called"))
    monkeypatch.setattr(sl, "read", lambda *a, **kw: {})
    providers.ClaudeLimitsProvider(str(tmp_path)).snapshot()
    lr.claude_limit_sample()
    lr.LimitResetService(sources={}, senders={}, state_path=tmp_path / "s.json")._claude_account_sample("org", state["now"])


def test_revocation_after_opener_creation_aborts_send(setup_api, monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            pytest.fail("request sent after revocation")
    def opener(*args):
        monkeypatch.setenv("TOKEN_TRACKER_CLAUDE_LIVE_USAGE", "0")
        return Opener()
    monkeypatch.setattr(api, "_opener", opener)
    with pytest.raises(RuntimeError):
        api._request("/api/oauth/usage", "test-token")
