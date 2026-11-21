import json
import os
from datetime import datetime, timezone

import pytest

from token_tracker import providers


@pytest.fixture(autouse=True)
def isolated_caches(monkeypatch):
    monkeypatch.setattr(providers.CodexProvider, "_token_event_cache", providers.OrderedDict())
    monkeypatch.setattr(providers.CodexProvider, "_token_event_cache_sizes", {})
    monkeypatch.setattr(providers.CodexProvider, "_token_event_resume", {})
    monkeypatch.setattr(providers.CodexProvider, "_token_event_cache_bytes", 0)
    monkeypatch.setattr(providers, "_CODEX_FIRST_RECORD_CACHE", providers.OrderedDict())


def _record(kind, payload=None, timestamp=None):
    return {"type": kind, "timestamp": timestamp or "2026-11-21T12:00:00Z", "payload": payload or {}}


def _session_bytes(model="gpt-4.1"):
    at = "2026-11-21T12:00:00Z"
    rows = [
        _record("session_meta", {"id": "session", "timestamp": at, "source": {"type": "exec"}}, at),
        _record("turn_context", {"model": model}, at),
        _record("event_msg", {"type": "token_count", "rate_limits": {"limit": "weekly", "used": 4},
                               "info": {"total_token_usage": {"input_tokens": 10, "total_tokens": 12},
                                        "last_token_usage": {"input_tokens": 10}}}, at),
    ]
    return b"".join((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8") for row in rows)


def _write(path, content):
    path.write_bytes(content)
    return providers.CodexProvider._token_events(path)


@pytest.mark.parametrize("split", [0, 1, 3, 15, 27, 50, 81, 117, 153, 200, 245, 300, 400,
                                    _session_bytes("gpt-4.1-🔥").index("🔥".encode()) + 1,
                                    _session_bytes("gpt-4.1-🔥").index("🔥".encode()) + 2,
                                    _session_bytes("gpt-4.1-🔥").index("🔥".encode()) + 3])
def test_append_resume_matches_full_parse_at_json_and_utf8_splits(tmp_path, split):
    raw = _session_bytes("gpt-4.1-🔥")
    split = min(split, len(raw) - 1)
    path = tmp_path / "session.jsonl"
    before = _write(path, raw[:split])
    with path.open("ab") as handle:
        handle.write(raw[split:])
    resumed = providers.CodexProvider._token_events(path)
    expected_path = tmp_path / "full.jsonl"
    expected_path.write_bytes(raw)
    expected = providers.CodexProvider._token_events(expected_path)
    assert resumed == expected


def test_partial_last_line_is_consumed_once_when_completed(tmp_path):
    raw = _session_bytes()
    cut = raw.rfind(b"\n", 0, len(raw) - 2) + 1
    path = tmp_path / "session.jsonl"
    first = _write(path, raw[:cut] + raw[cut:cut + 25])
    with path.open("ab") as handle:
        handle.write(raw[cut + 25:])
    resumed = providers.CodexProvider._token_events(path)
    assert len(first) == 0
    assert len(resumed) == 1
    assert resumed == providers.CodexProvider._token_events(path)


def test_append_returns_new_projection_without_mutating_prior_object(tmp_path):
    path = tmp_path / "session.jsonl"
    first_bytes = _session_bytes().splitlines(keepends=True)[:2]
    original = _write(path, b"".join(first_bytes))
    old_rows = original._rows.copy()
    with path.open("ab") as handle:
        handle.write(_session_bytes().splitlines(keepends=True)[2])
    resumed = providers.CodexProvider._token_events(path)
    assert resumed is not original
    assert original._rows == old_rows == []
    assert len(resumed) == 1


def test_rewrite_digest_mismatch_truncation_and_replacement_full_reparse(tmp_path):
    path = tmp_path / "session.jsonl"
    original = _write(path, _session_bytes("gpt-4.1"))
    raw = _session_bytes("gpt-4.1")
    replacement_bytes = raw.replace(b"gpt-4.1", b"gpt-4.1-updated")
    path.write_bytes(replacement_bytes)
    identity_before = (path.stat().st_dev, path.stat().st_ino)
    rewritten = providers.CodexProvider._token_events(path)
    assert (path.stat().st_dev, path.stat().st_ino) == identity_before
    expected = tmp_path / "rewrite.jsonl"
    expected.write_bytes(replacement_bytes)
    assert rewritten == providers.CodexProvider._token_events(expected)
    assert rewritten[0]["model"] == "gpt-4.1-updated"

    path.write_bytes(raw[: raw.index(b"turn_context")])
    assert providers.CodexProvider._token_events(path) == []

    old_identity = (path.stat().st_dev, path.stat().st_ino)
    staged = tmp_path / "staged.jsonl"
    staged.write_bytes(_session_bytes("gpt-4.1-replaced"))
    os.replace(staged, path)
    new_identity = (path.stat().st_dev, path.stat().st_ino)
    assert new_identity != old_identity
    replaced = providers.CodexProvider._token_events(path)
    assert replaced[0]["model"] == "gpt-4.1-replaced"
    assert original[0]["model"] == "gpt-4.1"


def test_resume_cache_byte_accounting_tracks_replacements(tmp_path):
    path = tmp_path / "session.jsonl"
    _write(path, _session_bytes())
    with path.open("ab") as handle:
        handle.write(_session_bytes().splitlines(keepends=True)[-1])
    providers.CodexProvider._token_events(path)
    assert providers.CodexProvider._token_event_cache_bytes == sum(
        providers.CodexProvider._token_event_cache_sizes.values())
    assert len(providers.CodexProvider._token_event_cache) == 1
    path.write_bytes(_session_bytes("gpt-4.1-longer"))
    providers.CodexProvider._token_events(path)
    assert len(providers.CodexProvider._token_event_cache) == 1
    assert providers.CodexProvider._token_event_cache_bytes == sum(
        providers.CodexProvider._token_event_cache_sizes.values())


def test_local_usage_reuses_first_record_and_recomputes_on_identity_change(tmp_path, monkeypatch):
    path = tmp_path / "session.jsonl"
    path.write_bytes(_session_bytes())
    original_open = providers.Path.open
    opens = []

    def counted_open(self, *args, **kwargs):
        if self == path:
            opens.append(args[0] if args else "r")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(providers.Path, "open", counted_open)
    provider = providers.CodexProvider()
    provider._local_usage(session_paths=(path,))
    opens.clear()
    provider._local_usage(session_paths=(path,))
    assert opens == []

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(_session_bytes().replace(b'"source": {"type": "exec"}',
                                                      b'"parent_thread_id": "parent"'))
    os.replace(replacement, path)
    result = provider._local_usage(session_paths=(path,))
    assert result["models"]["gpt-4.1"]["subagent_tokens"] == 12
