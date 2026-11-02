"""Shared fixtures for the work-ledger tests: an isolation guard and transcript builders.

Every ledger test runs with the real profile roots forbidden. The guard redirects home and
environment variables into tmp_path and fails any file open under the real ~/.claude,
~/.codex or %LOCALAPPDATA%.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

import pytest


def _profile_roots(home, environ):
    roots = [home / ".claude", home / ".codex"]
    roots.extend(Path(environ[name]) for name in ("CODEX_HOME", "CLAUDE_HOME", "CLAUDE_CONFIG_DIR")
                 if environ.get(name))
    # The product's data dirs, not all of LOCALAPPDATA: pytest's tmp_path lives under LOCALAPPDATA\Temp.
    if environ.get("LOCALAPPDATA"):
        local = Path(environ["LOCALAPPDATA"])
        roots.extend(local / name for name in ("Branchglance", "TokenTracker", "Packages"))
    return [root.resolve() for root in roots]


def _install_path_guard(monkeypatch, forbidden):
    def check(file, *, dir_fd=None):
        if not isinstance(file, (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(file))
        # A relative path against an unknown directory descriptor cannot be checked.
        if dir_fd is not None and not path.is_absolute():
            raise AssertionError("ledger test used an unchecked directory descriptor")
        target = path.resolve()
        if any(target == root or root in target.parents for root in forbidden):
            raise AssertionError(f"ledger test touched a real profile path: {target}")

    def wrap(function):
        def guarded(file, *args, **kwargs):
            check(file, dir_fd=kwargs.get("dir_fd"))
            return function(file, *args, **kwargs)
        return guarded

    real_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        if isinstance(database, (str, bytes, os.PathLike)):
            name = os.fsdecode(database)
            if name.startswith("file:"):
                uri = urlsplit(name)
                name = unquote(("//" + uri.netloc if uri.netloc else "") + uri.path)
                if os.name == "nt" and len(name) >= 3 and name[0] == "/" and name[2] == ":":
                    name = name[1:]
            if name != ":memory:":
                check(name)
        return real_connect(database, *args, **kwargs)

    for module, name in ((io, "open"), (builtins, "open"), (os, "open"),
                         (os, "listdir"), (os, "scandir")):
        monkeypatch.setattr(module, name, wrap(getattr(module, name)))
    monkeypatch.setattr(sqlite3, "connect", guarded_connect)


@pytest.fixture(autouse=True)
def ledger_guard(tmp_path, monkeypatch):
    real_home = Path(os.path.expanduser("~")).resolve()
    forbidden = _profile_roots(real_home, os.environ)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    for name in ("USERPROFILE", "HOME", "LOCALAPPDATA", "APPDATA"):
        monkeypatch.setenv(name, str(fake_home / name.lower()))
    for name in ("CLAUDE_HOME", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-env-unused"))

    _install_path_guard(monkeypatch, forbidden)
    yield


def test_profile_roots_include_original_overrides(tmp_path):
    env = {name: str(tmp_path / name) for name in
           ("LOCALAPPDATA", "CODEX_HOME", "CLAUDE_HOME", "CLAUDE_CONFIG_DIR")}
    roots = set(_profile_roots(tmp_path / "home", env))
    local = Path(env.pop("LOCALAPPDATA"))
    assert set(map(Path, env.values())) <= roots
    assert {local / name for name in ("Branchglance", "TokenTracker", "Packages")} <= roots
    assert local / "Temp" not in roots


@pytest.mark.parametrize("operation", ["open", "native_open", "listdir", "scandir", "sqlite", "sqlite_uri"])
def test_path_guard_blocks_bypasses_before_backend(tmp_path, monkeypatch, operation):
    root = tmp_path / "mock-profile"
    touched = []
    def backend(*args, **kwargs):
        touched.append((args, kwargs))
    with monkeypatch.context() as patch:
        for module, name in ((io, "open"), (builtins, "open"), (os, "open"),
                             (os, "listdir"), (os, "scandir"), (sqlite3, "connect")):
            patch.setattr(module, name, backend)
        _install_path_guard(patch, [root.resolve()])
        target = root / "data.db"
        calls = {
            "open": lambda: open(target),
            "native_open": lambda: os.open(target, os.O_RDONLY),
            "listdir": lambda: os.listdir(root),
            "scandir": lambda: os.scandir(root),
            "sqlite": lambda: sqlite3.connect(target),
            "sqlite_uri": lambda: sqlite3.connect(target.as_uri() + "?mode=ro", uri=True),
        }
        with pytest.raises(AssertionError, match="real profile path"):
            calls[operation]()
        assert not touched
        sqlite3.connect(tmp_path / "safe.db")
        assert len(touched) == 1


def test_path_guard_rejects_relative_directory_descriptor(tmp_path):
    with pytest.raises(AssertionError, match="directory descriptor"):
        os.open("data.db", os.O_RDONLY, dir_fd=123)


T0 = datetime(2026, 11, 2, 10, 0, 0, tzinfo=timezone.utc)
HOUR_START = int(T0.timestamp())
DAY_END = HOUR_START + 24 * 3600


def stamp(seconds: int) -> str:
    """ISO timestamp (UTC, Z suffix) for T0 plus seconds."""
    return datetime.fromtimestamp(HOUR_START + seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def write_jsonl(path: Path, items: Iterable[Any], *, newline: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(item) for item in items)
    if newline and text:
        text += "\n"
    path.write_text(text, encoding="utf-8")
    return path


def append_jsonl(path: Path, items: Iterable[Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item) + "\n")


def claude_response(seconds: int, msg_id: str, *, output: int, input_tokens: int = 10, cache_read: int = 0,
                    model: str = "claude-haiku-5-5", breakdown: dict | None = None,
                    creation: int | None = None, prompt: str | None = None) -> dict[str, Any]:
    usage: dict[str, Any] = {"input_tokens": input_tokens, "output_tokens": output,
                             "cache_read_input_tokens": cache_read}
    if breakdown is not None:
        usage["cache_creation"] = breakdown
        usage["cache_creation_input_tokens"] = sum(breakdown.values())
    elif creation is not None:
        usage["cache_creation_input_tokens"] = creation
    item: dict[str, Any] = {"type": "assistant", "timestamp": stamp(seconds),
                            "message": {"id": msg_id, "model": model, "usage": usage}}
    if prompt is not None:
        item = [{"type": "user", "timestamp": stamp(seconds - 1), "message": {"role": "user", "content": prompt}}, item]
    return item


def claude_response_items(seconds: int, msg_id: str, **kwargs) -> list[dict[str, Any]]:
    item = claude_response(seconds, msg_id, **kwargs)
    return item if isinstance(item, list) else [item]


def codex_meta(thread: str, seconds: int, *, source: str = "cli", prompt: str | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [{"type": "session_meta", "timestamp": stamp(seconds),
                                    "payload": {"id": thread, "timestamp": stamp(seconds), "source": source}},
                                   {"type": "turn_context", "timestamp": stamp(seconds),
                                    "payload": {"model": "gpt-6.1-terra"}}]
    if prompt is not None:
        items.append({"type": "response_item", "timestamp": stamp(seconds),
                      "payload": {"type": "message", "role": "user",
                                  "content": [{"type": "input_text", "text": prompt}]}})
    return items


def codex_count(seconds: int, *, input_tokens: int, output_tokens: int, cached: int = 0,
                reasoning: int = 0, total: int | None = None, last: dict | None = None) -> dict[str, Any]:
    usage = {"input_tokens": input_tokens, "cached_input_tokens": cached, "output_tokens": output_tokens,
             "reasoning_output_tokens": reasoning,
             "total_tokens": total if total is not None else input_tokens + output_tokens}
    info: dict[str, Any] = {"total_token_usage": usage}
    if last is not None:
        info["last_token_usage"] = last
    return {"type": "event_msg", "timestamp": stamp(seconds), "payload": {"type": "token_count", "info": info}}


def make_roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(data_dir, claude_root, codex_root), all under tmp_path."""
    return tmp_path / "data", tmp_path / "claude", tmp_path / "codex"


def claude_file(claude_root: Path, session: str, project: str = "-tmp-project") -> Path:
    return claude_root / "projects" / project / f"{session}.jsonl"


def codex_file(codex_root: Path, name: str) -> Path:
    return codex_root / "sessions" / "2026" / "11" / "02" / f"rollout-{name}.jsonl"


def clock_at(seconds: int):
    return lambda: float(HOUR_START + seconds)
