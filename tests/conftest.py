"""Suite-wide isolation: tests never touch the real product state, policy or WebView2 data."""

from __future__ import annotations

import pytest

from token_tracker import claude_provider, product, t3_projection


@pytest.fixture(autouse=True)
def isolate_t3_database(tmp_path, monkeypatch):
    monkeypatch.setattr(t3_projection, "database", lambda: tmp_path / "absent-t3.sqlite")
    t3_projection.reset()
    yield
    t3_projection.reset()


@pytest.fixture(autouse=True)
def _isolated_product_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setenv(product.STATE_ROOT_ENV, str(tmp_path / "product-state"))
    monkeypatch.delenv(product.POLICY_FILE_ENV, raising=False)
    # Feature switches from the developer's own environment would change provider policy under test.
    for name in ("TOKEN_TRACKER_LEDGER", "TOKEN_TRACKER_CLAUDE_SEND", "TOKEN_TRACKER_CLAUDE_LIVE_USAGE",
                 "TOKEN_TRACKER_LIMIT_RESET"):
        monkeypatch.delenv(name, raising=False)
    # Claude Code's config holds the selected account and its usage cache; never read the real one.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    # Codex sessions and the app-server binary live under CODEX_HOME; never scan the real ones.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(product, "legacy_webview_path", lambda environ=None: tmp_path / "no-legacy-webview")
    yield
    from token_tracker import providers
    providers._snapshot_shutdown = False
    providers._PERSISTED_CATALOG = None
    import sys
    widget = sys.modules.get("token_tracker.widget")
    if widget is not None:
        widget._cancel_shutdown_trace()


@pytest.fixture
def real_claude_cached_rows():
    return claude_provider._cached_rows


@pytest.fixture(autouse=True)
def isolate_claude_live_roster(monkeypatch, real_claude_cached_rows):
    monkeypatch.setattr(claude_provider, "live_discovery_rows", lambda: [])
    # Detailed rows also consult the cached hook roster, even without discovery.
    monkeypatch.setattr(claude_provider, "_cached_rows", lambda: [])
