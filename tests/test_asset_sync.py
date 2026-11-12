"""Served icons and intentional brand aliases must match their masters."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "assets" / "brand"
STATIC = ROOT / "token_tracker" / "static"


@pytest.mark.parametrize("copy,master", [
    (STATIC / "favicon.ico", BRAND / "branchglance.ico"),
    (STATIC / "favicon.svg", BRAND / "icon-tile.svg"),
    (BRAND / "lockup.svg", BRAND / "lockup-dark.svg"),
    (BRAND / "social-preview.png", BRAND / "lockup-dark.png"),
])
def test_asset_matches_master(copy, master):
    assert copy.read_bytes() == master.read_bytes(), f"{copy} differs from {master}"
