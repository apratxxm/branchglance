"""Suite-wide isolation: tests never touch the real product state, policy or WebView2 data."""

from __future__ import annotations

import pytest

from token_tracker import product


@pytest.fixture(autouse=True)
def _isolated_product_state(tmp_path, monkeypatch):
    monkeypatch.setenv(product.STATE_ROOT_ENV, str(tmp_path / "product-state"))
    monkeypatch.delenv(product.POLICY_FILE_ENV, raising=False)
    monkeypatch.setattr(product, "legacy_webview_path", lambda environ=None: tmp_path / "no-legacy-webview")
