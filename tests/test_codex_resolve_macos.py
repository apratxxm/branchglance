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


FAKE_APP_SERVER = '''#!{python}
import json, sys
assert sys.argv[1:] == ["app-server", "--stdio"], sys.argv
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {{"userAgent": "fake"}}
    elif message["method"] == "account/rateLimits/read":
        result = {{"rateLimits": {{"primary": {{"usedPercent": 37, "resetsAt": 1900000000}},
                                   "secondary": {{"usedPercent": 61, "resetsAt": 1900500000}}}}}}
    else:
        result = {{}}
    print(json.dumps({{"jsonrpc": "2.0", "id": message["id"], "result": result}}), flush=True)
'''


def test_codex_limits_and_reset_times_reach_a_mac_with_codex_installed(tmp_path, monkeypatch):
    from token_tracker import limit_reset
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.write_text(FAKE_APP_SERVER.format(python=sys.executable))
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", str(codex.parent))
    _elsewhere(tmp_path, monkeypatch)
    client = codex_client.CodexAppServer(timeout=10)
    try:
        assert client.executable == str(codex.resolve())
        sample = limit_reset._read_codex_limit_sample(client)
    finally:
        client.close()
    assert (sample.five_hour_percent, sample.weekly_percent) == (37, 61)
    assert (sample.reset_at, sample.weekly_reset_at) == (1900000000, 1900500000)
