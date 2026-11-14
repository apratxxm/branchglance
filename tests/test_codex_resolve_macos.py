import os
import platform
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS Codex layouts")

from token_tracker import codex_client


def _exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def _elsewhere(tmp_path, monkeypatch):
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")


def test_native_codex_on_path(tmp_path, monkeypatch):
    native = _exe(tmp_path / "brew" / "bin" / "codex")
    monkeypatch.setenv("PATH", str(native.parent))
    _elsewhere(tmp_path, monkeypatch)
    assert codex_client.resolve_codex().path == str(native.resolve())


def test_npm_shim_runs_the_vendored_binary_not_node(tmp_path, monkeypatch):
    root = tmp_path / "lib" / "node_modules" / "@openai" / "codex"
    entry = _exe(root / "bin" / "codex.js")
    triple = "aarch64-apple-darwin" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64-apple-darwin"
    native = _exe(root / "vendor" / triple / "codex" / "codex")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    os.symlink(entry, bin_dir / "codex")
    monkeypatch.setenv("PATH", str(bin_dir))
    _elsewhere(tmp_path, monkeypatch)
    found = codex_client.resolve_codex()
    assert found.path == str(native.resolve())
    assert found.package_root == str(root.resolve())


def test_npm_shim_without_vendored_binary_is_not_run(tmp_path, monkeypatch):
    entry = _exe(tmp_path / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    os.symlink(entry, bin_dir / "codex")
    monkeypatch.setenv("PATH", str(bin_dir))
    _elsewhere(tmp_path, monkeypatch)
    assert codex_client.resolve_codex() is None


def test_codex_in_the_working_directory_is_never_used(tmp_path, monkeypatch):
    planted = _exe(tmp_path / "work" / "codex")
    monkeypatch.setenv("PATH", str(planted.parent))
    monkeypatch.chdir(planted.parent)
    assert codex_client.resolve_codex() is None
