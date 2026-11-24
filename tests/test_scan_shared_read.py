"""Freshness expectations for the separate Codex reduction passes."""

import json
import os

import pytest

from token_tracker import providers


@pytest.mark.parametrize("change", ["grow", "replace", "truncate"])
def test_group_reducer_observes_file_changes_after_token_read(tmp_path, change):
    path = tmp_path / "rollout.jsonl"
    context = b'{"type":"turn_context","payload":{"model":"test"}}\n'
    other = b'{"type":"session_meta","payload":{}}\n'
    event = (b'{"timestamp":"2026-01-01T00:00:00Z","payload":{"info":'
             b'{"total_token_usage":{"input_tokens":10,"total_tokens":10}}}}\n')
    path.write_bytes(context + other + event)
    with providers._build_scope():
        providers.CodexProvider._token_events(path)
        if change == "grow":
            with path.open("ab") as stream:
                stream.write(context + event)
        elif change == "replace":
            replacement = tmp_path / "replacement.jsonl"
            replacement.write_bytes(context + context + other + event)
            os.replace(replacement, path)
        else:
            path.write_bytes(other)
        spec = providers._codex_turn_count_spec()
        result = providers._incremental_jsonl_reduce_group(path, [spec])[spec["kind"]]
        fresh = sum(json.loads(line).get("type") == "turn_context" for line in path.read_bytes().splitlines())
        assert result == (fresh or None)
        specs = providers._codex_rollout_reducer_specs(path)
        projected = providers._incremental_jsonl_reduce_group(path, specs)
        fresh_specs = [dict(item, kind=item["kind"] + "-fresh") for item in specs]
        recomputed = providers._incremental_jsonl_reduce_group(path, fresh_specs)
        for item in specs:
            assert projected[item["kind"]] == recomputed[item["kind"] + "-fresh"]
        reference = tmp_path / "reference.jsonl"
        reference.write_bytes(path.read_bytes())
        assert list(providers.CodexProvider._token_events(path)) == list(
            providers.CodexProvider._token_events(reference))


def _clear_token_event_cache():
    provider = providers.CodexProvider
    with provider._token_event_cache_lock:
        for key in list(provider._token_event_cache):
            provider._drop_token_event_cache_entry(key)


def _token_result(path):
    """Events and the resume point, read from scratch."""
    _clear_token_event_cache()
    with providers._build_scope():
        events = providers.CodexProvider._token_events(path)
    resume = events._checkpoint
    return (list(events), resume.offset, resume.complete_rows, resume.model, resume.started, resume.digest)


def _adversarial_transcript():
    def usage(total, stamp="2026-01-01T00:00:00Z"):
        return json.dumps({"timestamp": stamp, "type": "event_msg", "payload": {
            "info": {"total_token_usage": {"input_tokens": total, "total_tokens": total}}}}).encode()

    return [
        b'{"type":"session_meta","payload":{"timestamp":"2026-01-01T00:00:00Z"}}\n',
        b'{"type":"turn_context","payload":{"model":"first"}}\r\n',
        usage(10) + b"\n",
        b'{"type":"response_item","payload":{"text":"ordinary"}}\n',
        b'\n', b'not json\n', b'[]\n', b'null\n', b'{"payload":"text"}\n',
        b'{"type":"response_item","payload":{"text":"mentions total_token_usage"}}\n',
        usage(20).replace(b"total_token_usage", b"total\\u005ftoken_usage") + b"\n",
        b'{"type":"turn\\u005fcontext","payload":{"model":"escaped"}}\n',
        usage(30).replace(b"total_token_usage", b"total_token\xff_usage") + b"\n",
        b'{"type":"turn_con\xfetext","payload":{"model":"dropped byte"}}\n',
        b'{"type":"response_item","payload":{"text":"\xff\xfe"}}\n',
        usage(40).replace(b'"type"', b'"type":"session_meta","type"') + b"\n",
        '{"type":"response_item","payload":{"text":"ç\u2028"}}\n'.encode(),
        usage(50) + b"\r\n",
        b'{"type":"turn_context","payload":{"model":"last"}}\n',
        usage(60) + b"\n",
    ]


def test_token_filter_equals_parsing_every_line(tmp_path, monkeypatch):
    lines = _adversarial_transcript()
    path = tmp_path / "rollout.jsonl"
    for tail in (b"", b'{"type":"turn_context"', lines[2].rstrip(b"\n")):
        path.write_bytes(b"".join(lines) + tail)
        filtered = _token_result(path)
        with monkeypatch.context() as patch:
            patch.setattr(providers, "_TOKEN_EVENT_NEEDLES", (b"",))
            unfiltered = _token_result(path)
        assert filtered == unfiltered
        # Escaped and damaged keys must still be counted, not just agree.
        assert len(filtered[0]) == 6 + bool(tail.endswith(b"}"))
        assert filtered[1] == sum(map(len, lines))


def test_token_filter_parses_only_lines_that_can_matter(tmp_path, monkeypatch):
    path = tmp_path / "rollout.jsonl"
    ordinary = b'{"type":"response_item","payload":{"text":"ordinary"}}\n'
    path.write_bytes(_adversarial_transcript()[1] + ordinary * 50)
    parsed = []
    loads = json.loads
    monkeypatch.setattr(providers.json, "loads", lambda text, *a, **k: parsed.append(text) or loads(text, *a, **k))
    _token_result(path)
    assert len(parsed) == 1


def test_resumed_token_read_lands_on_the_same_offsets(tmp_path):
    lines = _adversarial_transcript()
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b"".join(lines[:7]))
    _clear_token_event_cache()
    with providers._build_scope():
        providers.CodexProvider._token_events(path)
    with path.open("ab") as stream:
        stream.write(b"".join(lines[7:]))
    with providers._build_scope():
        resumed = providers.CodexProvider._token_events(path)
    point = resumed._checkpoint
    assert (list(resumed), point.offset, point.complete_rows, point.model, point.started, point.digest) == _token_result(path)


def test_a_line_that_cannot_be_checked_is_parsed():
    class Unchecked(bytes):
        def decode(self, *args, **kwargs):
            raise MemoryError

    assert providers._token_line_is_inert(b'{"type":"response_item"}\n')
    assert not providers._token_line_is_inert(Unchecked(b'{"type":"response_item"}\n'))


def test_too_deeply_nested_irrelevant_line_does_not_abort_the_read(tmp_path):
    lines = _adversarial_transcript()
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(lines[1] + b"[" * 200_000 + b"\n" + lines[2] + b"\n")
    events, offset = _token_result(path)[:2]
    assert len(events) == 1 and offset == path.stat().st_size
