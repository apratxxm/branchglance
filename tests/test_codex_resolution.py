from pathlib import Path

import pytest

from token_tracker import codex_client


def test_path_install_takes_priority_over_known_install(tmp_path, monkeypatch):
    cwd = tmp_path / "work"
    cwd.mkdir()
    path_executable = tmp_path / "path-bin" / "codex.exe"
    path_executable.parent.mkdir()
    path_executable.touch()
    local = tmp_path / "local"
    old_executable = local / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    old_executable.parent.mkdir(parents=True)
    old_executable.touch()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", str(path_executable.parent))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "absent"))
    assert codex_client.resolve_codex().path == str(path_executable.resolve())


def test_known_install_is_used_when_path_has_no_codex(tmp_path, monkeypatch):
    cwd = tmp_path / "work"
    cwd.mkdir()
    local = tmp_path / "local"
    executable = local / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "absent"))
    assert codex_client.resolve_codex().path == str(executable.resolve())


@pytest.mark.parametrize("shim", ["codex.cmd", "codex.ps1"])
def test_resolver_finds_npm_dot_bin_native(tmp_path, monkeypatch, shim):
    cwd = tmp_path / "work"
    cwd.mkdir()
    modules = tmp_path / "tools" / "node_modules"
    bin_dir = modules / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / shim).write_text("not executable", encoding="utf-8")
    package = modules / "@openai" / "codex"
    (package / "bin").mkdir(parents=True)
    (package / "bin" / "codex.js").touch()
    vendor = modules / "@openai" / "codex-win32-x64"
    vendor.mkdir(parents=True)
    (vendor / "package.json").write_text('{}', encoding="utf-8")
    native = vendor / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
    native.parent.mkdir(parents=True)
    native.touch()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("LOCALAPPDATA", "")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "absent"))
    monkeypatch.setattr(codex_client.platform, "machine", lambda: "AMD64")
    found = codex_client.resolve_codex()
    assert found.path == str(native.resolve())
    assert found.package_root == str(package.resolve())


def test_resolver_rejects_cwd_and_relative_path_entries(tmp_path, monkeypatch):
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "codex.exe").touch()
    (cwd / "nested").mkdir()
    (cwd / "nested" / "codex.exe").touch()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", f";.;nested;{cwd / 'nested'}")
    monkeypatch.setenv("LOCALAPPDATA", "")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "absent"))
    assert codex_client.resolve_codex() is None


def test_resolver_finds_npm_native_and_passes_npm_environment(tmp_path, monkeypatch):
    cwd = tmp_path / "work"
    cwd.mkdir()
    npm = tmp_path / "npm"
    package = npm / "node_modules" / "@openai" / "codex"
    (package / "bin").mkdir(parents=True)
    (package / "bin" / "codex.js").touch()
    (npm / "codex.cmd").touch()
    native = package.parent / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
    native.parent.mkdir(parents=True)
    native.touch()
    (package.parent / "codex-win32-x64" / "package.json").write_text('{}', encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", str(npm))
    monkeypatch.setenv("LOCALAPPDATA", "")
    monkeypatch.setenv("CODEX_MANAGED_BY_BUN", "1")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "absent"))
    monkeypatch.setattr(codex_client.platform, "machine", lambda: "AMD64")
    found = codex_client.resolve_codex()
    assert found.path == str(native.resolve())
    env = found.environment()
    assert env["CODEX_MANAGED_PACKAGE_ROOT"] == str(package.resolve())
    assert env["CODEX_MANAGED_BY_NPM"] == "1"
    assert "CODEX_MANAGED_BY_BUN" not in env


def test_resolver_does_not_run_unrecognized_shim(tmp_path, monkeypatch):
    cwd = tmp_path / "work"
    cwd.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "codex.cmd").touch()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("LOCALAPPDATA", "")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "absent"))
    assert codex_client.resolve_codex() is None


def test_npm_package_manager_hints_and_pnpm_ownership(tmp_path, monkeypatch):
    root = tmp_path / "node_modules" / "@openai" / "codex"
    root.mkdir(parents=True)
    monkeypatch.setenv("npm_config_user_agent", "bun/1.2")
    assert codex_client._package_manager(root.resolve(), root) == "BUN"
    (tmp_path / "node_modules" / ".modules.yaml").touch()
    assert codex_client._package_manager(root.resolve(), root) == "PNPM"


def test_resolver_rejects_absolute_symlink_into_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "codex.exe").touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    try:
        (bin_dir / "codex.exe").symlink_to(cwd / "codex.exe")
    except OSError:
        import pytest
        pytest.skip("symlink creation unavailable")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("LOCALAPPDATA", "")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "absent"))
    assert codex_client.resolve_codex() is None
