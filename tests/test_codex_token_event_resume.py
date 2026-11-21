import json
import os
from datetime import date, datetime, timezone

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


def _token_record(total, timestamp="2026-11-21T12:00:00Z", rate_limits=None):
    return _record("event_msg", {
        "type": "token_count",
        "rate_limits": rate_limits if rate_limits is not None else {
            "limit": "weekly", "window": {"used": 4, "limit": 20}},
        "info": {"total_token_usage": {"input_tokens": total - 2, "total_tokens": total},
                 "last_token_usage": {"input_tokens": total - 2}},
    }, timestamp)


def _session_bytes(model="gpt-4.1"):
    at = "2026-11-21T12:00:00Z"
    rows = [
        _record("session_meta", {"id": "session", "timestamp": at, "source": {"type": "exec"}}, at),
        _record("turn_context", {"model": model}, at),
        _token_record(12, at),
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
    _write(path, raw[:split])
    with path.open("ab") as handle:
        handle.write(raw[split:])
    resumed = providers.CodexProvider._token_events(path)
    expected_path = tmp_path / "full.jsonl"
    expected_path.write_bytes(raw)
    expected = providers.CodexProvider._token_events(expected_path)
    assert json.dumps(list(resumed), sort_keys=True) == json.dumps(list(expected), sort_keys=True)


def test_growth_reads_from_saved_offset_and_preserves_populated_prior_projection(tmp_path, monkeypatch):
    path = tmp_path / "session.jsonl"
    initial = _session_bytes()
    original = _write(path, initial)
    original_projection = json.dumps(list(original), sort_keys=True)
    assert len(original) == 1
    checkpoint = providers.CodexProvider._token_event_resume[next(iter(
        providers.CodexProvider._token_event_cache))]
    second = (_token_record(20, "2026-11-21T12:01:00Z") | {})
    appended = (json.dumps(second) + "\n").encode()
    with path.open("ab") as handle:
        handle.write(appended)

    original_open = providers.Path.open
    first_read_offsets = []

    class ReadProbe:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def readline(self, *args, **kwargs):
            first_read_offsets.append(self.handle.tell())
            return self.handle.readline(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.handle, name)

    def probing_open(self, *args, **kwargs):
        handle = original_open(self, *args, **kwargs)
        return ReadProbe(handle) if self == path and (args and args[0] == "rb") else handle

    monkeypatch.setattr(providers.Path, "open", probing_open)
    resumed = providers.CodexProvider._token_events(path)
    expected_path = tmp_path / "full.jsonl"
    expected_path.write_bytes(initial + appended)
    expected = providers.CodexProvider._token_events(expected_path)
    assert first_read_offsets and first_read_offsets[0] == checkpoint.offset > 0
    assert json.dumps(list(resumed), sort_keys=True) == json.dumps(list(expected), sort_keys=True)
    assert resumed[0]["rate_limits"] is resumed[1]["rate_limits"]
    assert json.dumps(list(original), sort_keys=True) == original_projection


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
    original = _write(path, _session_bytes())
    old_rows = original._rows.copy()
    prior_projection = json.dumps(list(original), sort_keys=True)
    with path.open("ab") as handle:
        handle.write((json.dumps(_token_record(20, "2026-11-21T12:01:00Z")) + "\n").encode())
    resumed = providers.CodexProvider._token_events(path)
    assert resumed is not original
    assert original._rows == old_rows
    assert json.dumps(list(original), sort_keys=True) == prior_projection
    assert len(original) == 1
    assert len(resumed) == 2


def test_resume_oserror_falls_back_to_full_parse(tmp_path, monkeypatch):
    path = tmp_path / "resume-error.jsonl"
    initial = _write(path, _session_bytes())
    with path.open("ab") as handle:
        handle.write((json.dumps(_token_record(20, "2026-11-21T12:01:00Z")) + "\n").encode())
    original_open = providers.Path.open
    attempts = []

    def fail_resume_open(self, *args, **kwargs):
        if self == path and args and args[0] == "rb":
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("injected resume read failure")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(providers.Path, "open", fail_resume_open)
    events = providers.CodexProvider._token_events(path)
    assert len(attempts) == 2
    assert [event["usage"]["total_tokens"] for event in events] == [12, 20]
    assert len(initial) == 1


def test_file_replaced_between_stat_and_open_refreshes_identity_and_retries(tmp_path, monkeypatch):
    path = tmp_path / "replaced-during-open.jsonl"
    _write(path, _session_bytes())
    with path.open("ab") as handle:
        handle.write((json.dumps(_token_record(20, "2026-11-21T12:01:00Z")) + "\n").encode())
    staged = tmp_path / "replacement.jsonl"
    staged.write_bytes(_session_bytes("gpt-4.1-new").replace(b'"total_tokens": 12', b'"total_tokens": 30'))
    original_open = providers.Path.open
    armed = [True]
    calls = []

    def replace_before_open(self, *args, **kwargs):
        if self == path and args and args[0] == "rb":
            calls.append(1)
            if armed[0]:
                armed[0] = False
                os.replace(staged, path)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(providers.Path, "open", replace_before_open)
    events = providers.CodexProvider._token_events(path)
    assert len(calls) == 2
    assert events[0]["model"] == "gpt-4.1-new"
    assert events[0]["usage"]["total_tokens"] == 30


def test_rewrite_digest_mismatch_truncation_and_replacement_full_reparse(tmp_path):
    path = tmp_path / "session.jsonl"
    original = _write(path, _session_bytes("gpt-4.1"))
    raw = _session_bytes("gpt-4.1")
    replacement_bytes = raw.replace(b"gpt-4.1", b"gpt-4.1-updated")
    identity_before = (path.stat().st_dev, path.stat().st_ino)
    path.write_bytes(replacement_bytes)
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


def test_same_size_rewrite_forces_full_parse(tmp_path):
    path = tmp_path / "same-size.jsonl"
    raw = _session_bytes() + b'{"type":"ignored"}\n' * 400
    _write(path, raw)
    identity = (path.stat().st_dev, path.stat().st_ino)
    rewritten = raw.replace(b'"total_tokens": 12', b'"total_tokens": 99')
    assert len(rewritten) == len(raw)
    path.write_bytes(rewritten)
    assert (path.stat().st_dev, path.stat().st_ino) == identity
    events = providers.CodexProvider._token_events(path)
    assert events[0]["usage"]["total_tokens"] == 99


def test_shrinking_unfinished_tail_forces_full_parse(tmp_path):
    path = tmp_path / "shrinking-tail.jsonl"
    tail = b'{"type":"unfinished record that will shrink'
    raw = _session_bytes() + b'{"type":"ignored"}\n' * 400 + tail
    _write(path, raw)
    previous_key = next(iter(providers.CodexProvider._token_event_cache))
    checkpoint = providers.CodexProvider._token_event_resume[previous_key]
    assert len(raw) > checkpoint.offset
    rewritten = raw.replace(b'"total_tokens": 12', b'"total_tokens": 99')
    rewritten = rewritten[:-12]
    assert checkpoint.offset < len(rewritten) < len(raw)
    path.write_bytes(rewritten)
    events = providers.CodexProvider._token_events(path)
    assert events[0]["usage"]["total_tokens"] == 99


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
    monkeypatch.setattr(providers, "_local_today", lambda: date(2026, 11, 21))
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


def test_unterminated_first_record_is_read_but_not_memoized(tmp_path):
    path = tmp_path / "unterminated-meta.jsonl"
    valid = json.dumps(_record("session_meta", {"parent_thread_id": "root"}))
    path.write_text(valid, encoding="utf-8")
    assert providers._codex_is_subagent(path, path.stat()) is True
    assert str(path.resolve()) not in providers._CODEX_FIRST_RECORD_CACHE
    with path.open("ab") as handle:
        handle.write(b"invalid-suffix")
    assert providers._codex_is_subagent(path, path.stat()) is False


def test_shrunk_malformed_first_record_drops_stale_cache_entry(tmp_path):
    path = tmp_path / "shrunk-meta.jsonl"
    path.write_text(json.dumps(_record("session_meta", {"parent_thread_id": "root"})) + "\n",
                    encoding="utf-8")
    resolved = str(path.resolve())
    assert providers._codex_is_subagent(path, path.stat()) is True
    path.write_bytes(b"{bad")
    assert providers._codex_is_subagent(path, path.stat()) is False
    assert resolved not in providers._CODEX_FIRST_RECORD_CACHE
    path.write_text(json.dumps(_record("session_meta", {"id": "root"})) + "\n", encoding="utf-8")
    assert providers._codex_is_subagent(path, path.stat()) is False


def test_resume_carries_inherited_baseline_and_cumulative_counter_state(tmp_path, monkeypatch):
    path = tmp_path / "baseline.jsonl"
    at = "2026-11-21T12:00:00Z"
    rows = [
        _record("session_meta", {"id": "session", "timestamp": at}),
        _record("turn_context", {"model": "gpt-4.1"}),
        _token_record(80, "2026-11-21T11:59:00Z"),
        _token_record(100, "2026-11-21T12:01:00Z"),
    ]
    path.write_bytes(b"".join((json.dumps(row) + "\n").encode() for row in rows))
    initial = providers.CodexProvider._token_events(path)
    assert [event["inherited"] for event in initial] == [True, False]
    with path.open("ab") as handle:
        handle.write((json.dumps(_token_record(110, "2026-11-21T11:58:00Z")) + "\n").encode())
        handle.write((json.dumps(_token_record(130, "2026-11-21T12:02:00Z")) + "\n").encode())
    resumed = providers.CodexProvider._token_events(path)
    expected_path = tmp_path / "baseline-full.jsonl"
    expected_path.write_bytes(path.read_bytes())
    expected = providers.CodexProvider._token_events(expected_path)
    assert json.dumps(list(resumed), sort_keys=True) == json.dumps(list(expected), sort_keys=True)
    assert [event["inherited"] for event in resumed] == [True, False, True, False]
    monkeypatch.setattr(providers, "_local_today", lambda: date(2026, 11, 21))
    usage = providers.CodexProvider()._local_usage(session_paths=(path,))
    assert usage["totals"]["total_tokens"] == 40


def test_first_record_cache_watermark_comes_from_stable_opened_file(tmp_path, monkeypatch):
    path = tmp_path / "first-record-race.jsonl"
    path.write_bytes(b"{")
    stale_stat = path.stat()
    complete = (json.dumps(_record("session_meta", {"parent_thread_id": "parent"})) + "\n").encode()
    original_open = providers.Path.open
    completed = [False]

    def complete_before_open(self, *args, **kwargs):
        if self == path and not completed[0]:
            completed[0] = True
            with original_open(self, "wb") as handle:
                handle.write(complete)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(providers.Path, "open", complete_before_open)
    assert providers._codex_is_subagent(path, stale_stat) is True
    cache_key = providers._canonical_path_text(str(path.resolve()))
    cached = providers._CODEX_FIRST_RECORD_CACHE[cache_key]
    assert cached[1] == len(complete)

    with original_open(path, "wb") as handle:
        handle.write(b"{}")
    assert providers._codex_is_subagent(path, path.stat()) is False
    assert cache_key not in providers._CODEX_FIRST_RECORD_CACHE


def test_checkpoint_objects_are_included_in_cache_estimate_and_admission(tmp_path, monkeypatch):
    path = tmp_path / "large-model-checkpoint.jsonl"
    model = "x" * 8192
    rows = [_record("turn_context", {"model": model})]
    path.write_bytes((json.dumps(rows[0]) + "\n").encode())
    events = providers.CodexProvider._token_events(path)
    key = next(iter(providers.CodexProvider._token_event_cache))
    charge = providers.CodexProvider._token_event_cache_sizes[key]
    assert charge > len(model)
    monkeypatch.setattr(providers.CodexProvider, "_token_event_cache", providers.OrderedDict())
    monkeypatch.setattr(providers.CodexProvider, "_token_event_cache_sizes", {})
    monkeypatch.setattr(providers.CodexProvider, "_token_event_resume", {})
    monkeypatch.setattr(providers.CodexProvider, "_token_event_cache_bytes", 0)
    monkeypatch.setattr(providers.CodexProvider, "_TOKEN_EVENT_CACHE_MAX_BYTES", 4096)
    uncached_path = tmp_path / "large-model-uncached.jsonl"
    uncached_path.write_bytes(path.read_bytes())
    result = providers.CodexProvider._token_events(uncached_path)
    assert result == []
    assert not providers.CodexProvider._token_event_cache
    assert providers.CodexProvider._token_event_cache_bytes == 0
