"""Guarded, opt-in Claude Code status-line connection; never chains commands."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import claude_hook_install as hooks, claude_statusline, product

_lock = threading.RLock()


def _default_command():
    if sys.platform == "darwin" and not product.is_frozen():
        helper = Path.home() / "Library/Application Support/Branchglance/runtime/bin/branchglance-statusline"
        if helper.is_file():
            parts = [helper.as_posix()]
        else:
            parts = [Path(sys.executable).as_posix(), (Path(__file__).resolve().parents[1] / "packaging/build.py").as_posix(), "--statusline"]
    elif product.is_frozen():
        parts = [product.launcher_path().as_posix(), "--statusline"]
    else:
        parts = [Path(sys.executable).as_posix(), (Path(__file__).resolve().parents[1] / "packaging/build.py").as_posix(), "--statusline"]
    # Quoted literal paths work in both documented Windows shells; fail closed
    # for characters whose interpretation depends on the selected shell.
    if any(any(c in part for c in '"$`\r\n') for part in parts):
        raise hooks.HookInstallError("Helper path contains shell-sensitive characters; configure the status line manually.")
    return " ".join('"' + part + '"' for part in parts)


def settings_path():
    return product.claude_config_dir() / "settings.json"


def _target(path, record):
    """Explicit path; else the file this status line was connected to (so a later
    config-folder change cannot strand it); else the resolved default."""
    if path is not None:
        return Path(path)
    return Path(record["settingsPath"]) if record else settings_path()


def _recorded_settings(value):
    """The recorded settings file, normalised; refused unless absolute and literally named settings.json.

    The record is only a hint on disk: a file with any other name could hold the wrapper too.
    """
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise hooks.HookInstallError("Status line record has no absolute settings path; it was not changed.")
    normal = os.path.normpath(value)
    if Path(normal).name != "settings.json":
        raise hooks.HookInstallError("Status line record does not name settings.json; it was not changed.")
    return normal


def _record(root):
    try:
        with claude_statusline._guard_directories(root), claude_statusline._open_checked(root / "statusline-chain.json") as f:
            value = json.load(f)
        if (not isinstance(value, dict) or value.get("schemaVersion") != 1 or
                not isinstance(value.get("settingsPath"), str) or
                type(value.get("originalPresent")) is not bool or
                not isinstance(value.get("wrapper"), dict)):
            raise ValueError("invalid status line record")
        value["settingsPath"] = _recorded_settings(value["settingsPath"])
        return value
    except FileNotFoundError:
        return None


def _save_record(root, value):
    name = None
    with claude_statusline._guard_directories(root, create=True):
        path = root / "statusline-chain.json"
        claude_statusline._check_path(path, allow_missing=True, directory=False)
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False) as f:
                name = f.name
                json.dump(value, f)
            os.replace(name, path)
            name = None
        finally:
            if name:
                os.unlink(name)


def _drop_record(root):
    with claude_statusline._guard_directories(root):
        path = root / "statusline-chain.json"
        claude_statusline._check_path(path, directory=False)
        path.unlink()


def _write(path, snapshot, data, bom):
    path.parent.mkdir(parents=True, exist_ok=True)
    hooks._validate_path_chain(path)
    if snapshot.raw is not None:
        hooks._unique_adjacent(path, "backup-" + time.strftime("%Y%m%d-%H%M%S"), snapshot.raw)
    temp = hooks._unique_adjacent(path, "tmp", hooks._serialize(data, bom, snapshot.raw))
    try:
        if not snapshot.matches(hooks._snapshot_file(path)):
            raise hooks.HookInstallError("settings.json changed while editing; connect or disconnect refused")
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def status(*, root=None, path=None):
    root = Path(root) if root is not None else product.state_root()
    with _lock:
        record = _record(root)
        path = _target(path, record)
        hooks._validate_path_chain(path)
        if not record or record["settingsPath"] != str(path.resolve()):
            return {"connected": False}
        _, data, _ = hooks._read_versioned(path)
        return {"connected": data.get("statusLine") == record["wrapper"]}


def connect(*, root=None, path=None, command=None):
    root = Path(root) if root is not None else product.state_root()
    path = Path(path) if path is not None else settings_path()
    with _lock:
        hooks._validate_path_chain(path)
        snapshot, data, bom = hooks._read_versioned(path)
        record = _record(root)
        canonical = str(path.resolve())
        if record:
            if record["settingsPath"] != canonical:
                raise hooks.HookInstallError("Status line belongs to another Claude configuration; disconnect it there first.")
            if data.get("statusLine") == record["wrapper"]:
                return {"connected": True, "changed": False}
            _drop_record(root)
        hooks.preflight_statusline_connect(path)
        if not snapshot.matches(hooks._snapshot_file(path)):
            raise hooks.HookInstallError("settings.json changed while reading; connect refused")
        if command is None:
            command = _default_command()
        wrapper = {"type": "command", "command": command}
        record = {"schemaVersion": 1, "settingsPath": canonical, "originalPresent": False,
                  "originalValue": None, "wrapper": wrapper, "createdAt": time.time()}
        _save_record(root, record)
        data["statusLine"] = wrapper
        _write(path, snapshot, data, bom)
        return {"connected": True, "changed": True}


def disconnect(*, root=None, path=None):
    root = Path(root) if root is not None else product.state_root()
    with _lock:
        record = _record(root)
        path = _target(path, record)
        hooks._validate_path_chain(path)
        if not record:
            return {"connected": False, "changed": False}
        if record["settingsPath"] != str(path.resolve()):
            raise hooks.HookInstallError("Status line belongs to another Claude configuration; it was not changed.")
        snapshot, data, bom = hooks._read_versioned(path)
        if data.get("statusLine") != record["wrapper"]:
            _drop_record(root)
            return {"connected": False, "changed": False, "reason": "Status line changed since connecting; left untouched."}
        if record["originalPresent"]:
            data["statusLine"] = record.get("originalValue")
        else:
            data.pop("statusLine", None)
        _write(path, snapshot, data, bom)
        _drop_record(root)
        return {"connected": False, "changed": True}
