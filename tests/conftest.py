"""Suite-wide isolation: tests never touch the real product state, policy or WebView2 data."""

from __future__ import annotations

import pytest

from token_tracker import claude_provider, product


@pytest.fixture(autouse=True)
def _isolated_product_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setenv(product.STATE_ROOT_ENV, str(tmp_path / "product-state"))
    monkeypatch.delenv(product.POLICY_FILE_ENV, raising=False)
    # Claude Code's config holds the selected account and its usage cache; never read the real one.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setattr(product, "legacy_webview_path", lambda environ=None: tmp_path / "no-legacy-webview")


@pytest.fixture
def real_claude_cached_rows():
    return claude_provider._cached_rows


@pytest.fixture(autouse=True)
def isolate_claude_live_roster(monkeypatch, real_claude_cached_rows):
    monkeypatch.setattr(claude_provider, "live_discovery_rows", lambda: [])
    # Detailed rows also consult the cached hook roster, even without discovery.
    monkeypatch.setattr(claude_provider, "_cached_rows", lambda: [])
