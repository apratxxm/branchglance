import json

from token_tracker import claude_provider, providers


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _agent_call(tool_id, description, agent_id=None, tool="Agent"):
    rows = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": tool,
         "input": {"description": description, "subagent_type": "haiku-builder", "prompt": "secret prompt"}},
    ]}}]
    if agent_id is not None:
        rows.append({"type": "user", "toolUseResult": {"agentId": agent_id}, "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": "done"},
        ]}})
    return rows


def test_subagent_takes_agent_tool_description_from_parent(tmp_path):
    parent = tmp_path / "proj" / "sess-1.jsonl"
    rows = _agent_call("toolu_A", "UI intern", "abc123")
    rows.append({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_B", "content": "orphan"}]}})
    rows += _agent_call("toolu_B", "Grand reviewer", tool="Task")
    rows.append({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_B",
         "content": [{"type": "text", "text": "Finished. agentId: def456 (use SendMessage)"}]}]}})
    _write(parent, rows)
    found = claude_provider._agent_descriptions(parent)
    assert found == {"abc123": "UI intern", "def456": "Grand reviewer"}
    assert "secret prompt" not in json.dumps(found)


def test_description_reader_is_incremental_and_sees_appended_calls(tmp_path):
    parent = tmp_path / "sess-2.jsonl"
    _write(parent, _agent_call("toolu_1", "Ledger intern", "aaa111"))
    assert claude_provider._agent_descriptions(parent) == {"aaa111": "Ledger intern"}
    with parent.open("a", encoding="utf-8") as handle:
        for row in _agent_call("toolu_2", "Second pass", "bbb222"):
            handle.write(json.dumps(row) + "\n")
        handle.write("not json at all\n")
    assert claude_provider._agent_descriptions(parent) == {"aaa111": "Ledger intern", "bbb222": "Second pass"}


def test_unknown_or_missing_parent_yields_no_description(tmp_path):
    assert claude_provider._agent_descriptions(None) == {}
    assert claude_provider._agent_descriptions(tmp_path / "missing.jsonl") == {}
    parent = tmp_path / "bare.jsonl"
    _write(parent, [{"type": "user", "message": {"content": "hello"}}])
    assert claude_provider._agent_descriptions(parent) == {}


def test_haiku_5_window_unknown_keeps_recorded_tokens(tmp_path):
    usage = {"input": 1000, "cached": 900, "creation": 0, "output": 5}
    fields = claude_provider._context_fields(
        {"usage": usage, "usage_ts": 1_800_000_000.0, "model": "claude-haiku-5-5"}, None, None)
    assert fields["contextTokens"] == 1900
    assert fields["contextWindow"] == 1_000_000
    assert fields["contextPercent"] == 0
    assert fields["contextModel"] == "claude-haiku-5-5"
    assert fields["lastRequest"]["cached"] == 900


def test_every_priced_claude_model_has_a_context_window():
    keys = set(providers.CLAUDE_PRICING) | {alias for alias, target in providers.PRICE_MODEL_ALIASES.items()
                                            if target in providers.CLAUDE_PRICING}
    for key in providers.CLAUDE_PRICING:
        keys.add("claude-" + key)
    for key in sorted(keys):
        if not key.startswith("claude-"):
            key = "claude-" + key
        assert providers._claude_context_window(key) is not None, key
    for dated in ("claude-haiku-5-5-20261001", "claude-sonnet-5-5-20261001", "claude-opus-5-5-20261001",
                  "claude-fable-5-1-20261001", "anthropic/claude-mythos-5-1-20261001"):
        assert providers._claude_context_window(dated) == 1_000_000, dated
    assert providers._claude_context_window("claude-haiku-4-5-20251001") == 200_000
    assert providers._claude_context_window("claude-unknown-9") is None

