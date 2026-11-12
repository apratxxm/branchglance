"""Repository hygiene checks over git-tracked files."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UTF8_BOM = b"\xef\xbb\xbf"
SNIFF_BYTES = 8192

# Tracked text files allowed to start with a UTF-8 BOM, each with its reason.
# Stale entries (no longer tracked or no longer BOM-prefixed) fail the test.
BOM_ALLOW_LIST = {
    "scripts/launcher/BgCodex.cs": "temporary: an open branch edits this file; strip the BOM once it merges",
}


def _tracked_files():
    # Release builds test from an export with no .git metadata; skip there.
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("git metadata unavailable (export or source archive)")
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=False, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"git ls-files unavailable: {exc}")
    if result.returncode != 0:
        pytest.skip("git ls-files failed; repository metadata unavailable")
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def test_tracked_text_files_have_no_utf8_bom():
    bom_files = set()
    for rel in _tracked_files():
        path = ROOT / rel
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            head = handle.read(SNIFF_BYTES)
        if b"\0" in head:  # binary, not text
            continue
        if head.startswith(UTF8_BOM):
            bom_files.add(rel)

    unexpected = sorted(bom_files - BOM_ALLOW_LIST.keys())
    stale = sorted(BOM_ALLOW_LIST.keys() - bom_files)
    assert not unexpected, (
        "tracked text files start with a UTF-8 BOM (remove it, or allow-list with a reason): "
        + ", ".join(unexpected)
    )
    assert not stale, "BOM allow-list entries are no longer tracked BOM files: " + ", ".join(stale)
