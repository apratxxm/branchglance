from __future__ import annotations

import json

from token_tracker import agent_links


def test_resume_thread_parser_accepts_only_uuid_after_exec_resume():
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    assert agent_links.parse_exec_resume_thread(f'"codex.exe" exec resume {thread}') == thread
    assert agent_links.parse_exec_resume_thread(f'"codex.exe" exec resume {thread} --cwd C:/private') == thread
    assert agent_links.parse_exec_resume_thread(f"codex.exe exec resume {thread}x") is None
    assert agent_links.parse_exec_resume_thread(f"codex.exe resume {thread}") is None


def test_process_scan_requires_exact_valid_ancestry_and_unique_session():
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    states = {("session-a", ""): {"pid_associations": {
        "10:100": {"pid": 10, "created": 100, "first": 1, "last": 10},
    }}}
    table = {20: (10, "codex.exe"), 10: (1, "claude.exe"), 1: (0, "explorer.exe")}
    kwargs = dict(table=table, current_sid=b"sid", equal_sid=lambda left, right: left == right,
                  process_sid=lambda _pid: b"sid",
                  created=lambda pid: {20: 300, 10: 100, 1: 1}.get(pid),
                  command_line=lambda _pid: f"codex.exe exec resume {thread}", claude_states=states)
    found = agent_links.scan_codex_processes(**kwargs)
    assert found == [{"thread_id": thread, "codex_pid": 20, "codex_created": 300,
                      "claude_session_id": "session-a", "method": "cmdline"}]
    assert agent_links.scan_codex_processes(**{**kwargs, "created": lambda pid: 300}) == []
    ambiguous = {**states, ("session-b", ""): states[("session-a", "")]}
    assert agent_links.scan_codex_processes(**{**kwargs, "claude_states": ambiguous}) == []
    assert agent_links.scan_codex_processes(**{**kwargs, "process_sid": lambda _pid: b"other"}) == []
    terminal_tree = {20: (1, "codex.exe"), 1: (0, "WindowsTerminal.exe")}
    assert agent_links.scan_codex_processes(**{**kwargs, "table": terminal_tree}) == []
    # PID 10 was reused: current creation time cannot match the recorded parent.
    assert agent_links.scan_codex_processes(
        **{**kwargs, "created": lambda pid: {20: 300, 10: 101, 1: 1}.get(pid)}
    ) == []


def test_receipts_are_bounded_validated_and_never_set_status(tmp_path):
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert agent_links.write_receipt(thread, 22, environ={"CLAUDE_CODE_SESSION_ID": sid},
                                     created=lambda _pid: 123, root=tmp_path)
    receipts = agent_links.read_receipts(tmp_path)
    assert len(receipts) == 1
    assert receipts[0]["method"] == "receipt"
    assert "status" not in receipts[0]
    assert not agent_links.write_receipt("bad", 22, environ={"CLAUDE_CODE_SESSION_ID": sid},
                                         created=lambda _pid: 123, root=tmp_path)


def test_registry_validates_and_atomically_retains_seven_days(tmp_path):
    path = tmp_path / "registry.json"
    good = {"thread_id": "01234567-89ab-cdef-0123-456789abcdef", "codex_pid": 22,
            "codex_created": 123, "claude_session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "first_seen": 1, "last_seen": 9, "method": "receipt"}
    old = {**good, "last_seen": 10 - agent_links.RETENTION_SECONDS - 1}
    assert agent_links.write_registry([good, old, {**good, "thread_id": "invalid"}], path, now=10)
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value == {"version": 1, "runs": [good]}


def test_graph_keys_revisions_and_null_removal(monkeypatch):
    monkeypatch.setattr(agent_links, "_GRAPH_EDGES", {})
    monkeypatch.setattr(agent_links, "_GRAPH_REVISION", 0)
    parent = {"id": "claude-id", "provider": "Claude"}
    child = {"id": "codex-id", "provider": "Codex", "parentId": "claude-id", "parentProvider": "Claude"}
    first = agent_links.snapshot_graph([parent, child])
    assert parent["key"] == "Claude:claude-id" and parent["parentKey"] is None
    assert child["key"] == "Codex:codex-id" and child["parentKey"] == "Claude:claude-id"
    assert first == {"revision": 1, "edges": {
        "Claude:claude-id": None, "Codex:codex-id": "Claude:claude-id",
    }}
    assert agent_links.snapshot_graph([parent, child])["revision"] == 1
    removal = agent_links.snapshot_graph([parent], delta=True)
    assert removal == {"revision": 2, "edges": {"Codex:codex-id": None}}


def test_graph_missing_parent_and_cycle_become_roots(monkeypatch):
    monkeypatch.setattr(agent_links, "_GRAPH_EDGES", {})
    monkeypatch.setattr(agent_links, "_GRAPH_REVISION", 0)
    left = {"id": "left", "provider": "Codex", "parentId": "right"}
    right = {"id": "right", "provider": "Codex", "parentId": "left"}
    orphan = {"id": "orphan", "provider": "Claude", "parentId": "missing"}
    graph = agent_links.snapshot_graph([left, right, orphan])
    assert left["parentKey"] is None and right["parentKey"] is None
    assert orphan["parentKey"] is None
    assert graph["edges"] == {"Codex:left": None, "Codex:right": None, "Claude:orphan": None}


def _link_fixture():
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    claude = {"name": "Claude", "chats": [{"id": sid}]}
    codex = {"name": "Codex", "chats": [{
        "id": thread, "source": "exec", "executionStatus": "running",
    }]}
    run = {"thread_id": thread, "codex_pid": 22, "codex_created": 123,
           "claude_session_id": sid, "first_seen": 1, "last_seen": 100,
           "verified_at": 100, "method": "receipt"}
    return sid, thread, [claude, codex], run


def test_link_poll_skips_full_scan_for_recently_verified_execution():
    _sid, _thread, providers, run = _link_fixture()
    called = []
    agent_links.apply_exact_links(
        providers, now=105, registry_path=__import__("pathlib").Path("unused"),
        registry_reader=lambda _path: [dict(run)],
        verify=lambda *_args: called.append("verify"),
        scan=lambda **_kwargs: called.append("scan"),
        hooks=lambda: called.append("hooks"),
        receipts_reader=lambda _root: called.append("receipts"),
    )
    assert called == []


def test_expired_verification_checks_known_process_without_full_scan():
    _sid, _thread, providers, run = _link_fixture()
    run["verified_at"] = 100
    called = []
    agent_links.apply_exact_links(
        providers, now=111, registry_path=__import__("pathlib").Path("unused"),
        registry_reader=lambda _path: [dict(run)],
        verify=lambda pid, created: called.append((pid, created)) or True,
        scan=lambda **_kwargs: called.append("scan"),
        hooks=lambda: called.append("hooks"),
        receipts_reader=lambda _root: called.append("receipts"),
    )
    assert called == [(22, 123)]


def test_new_exec_triggers_scan_and_links_only_exact_session():
    sid, thread, providers, run = _link_fixture()
    run["verified_at"] = 0
    calls = []
    agent_links.apply_exact_links(
        providers, now=105, registry_path=__import__("pathlib").Path("unused"),
        registry_reader=lambda _path: [],
        hooks=lambda: {},
        scan=lambda **kwargs: calls.append("scan") or [{**run, "method": "cmdline"}],
        receipts_reader=lambda _root: calls.append("receipts") or [],
        registry_writer=lambda *_args, **_kwargs: True,
    )
    assert calls == ["scan", "receipts"]
    assert providers[1]["chats"][0]["parentId"] == sid
    assert providers[1]["chats"][0]["parentProvider"] == "Claude"
    assert providers[1]["chats"][0]["id"] == thread


def test_idle_parent_is_invalidated_by_newer_plain_terminal_turn():
    sid, _thread, providers, run = _link_fixture()
    providers[1]["chats"][0].update(source="exec", executionStatus="idle",
                                    runStartedAt="1970-01-01T00:03:00Z")
    run["last_seen"] = 100
    run["verified_at"] = 100
    agent_links.apply_exact_links(
        providers, now=105, registry_path=__import__("pathlib").Path("unused"),
        registry_reader=lambda _path: [run], scan=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("idle rows never scan")), verify=lambda *_args: None,
    )
    assert "parentId" not in providers[1]["chats"][0]
    assert sid != providers[1]["chats"][0].get("parentId")


def test_conflicting_concurrent_runs_remain_unlinked():
    sid, _thread, providers, run = _link_fixture()
    other = {**run, "claude_session_id": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
             "codex_pid": 23, "codex_created": 124}
    agent_links.apply_exact_links(
        providers, now=105, registry_path=__import__("pathlib").Path("unused"),
        registry_reader=lambda _path: [run, other], scan=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("recent link needs no scan")),
    )
    assert "parentId" not in providers[1]["chats"][0]
    assert sid != providers[1]["chats"][0].get("parentId")


def test_missing_provider_policy_pair_reads_no_link_inputs():
    calls = []
    agent_links.apply_exact_links(
        [{"name": "Claude", "chats": []}], registry_path=__import__("pathlib").Path("unused"),
        registry_reader=lambda _path: calls.append("registry"),
        scan=lambda **_kwargs: calls.append("scan"),
        hooks=lambda: calls.append("hooks"),
        receipts_reader=lambda _root: calls.append("receipts"),
    )
    assert calls == []


def test_disabled_collection_skips_process_hooks_and_receipt_readers(monkeypatch):
    from token_tracker import claude_events, providers

    monkeypatch.setattr(providers, "_provider_collection_sets", lambda _policies: ((), ()))
    monkeypatch.setattr(providers, "_structure_stamp", lambda _visible: "test")
    for target, name in (
        (agent_links, "scan_codex_processes"),
        (agent_links, "read_receipts"),
        (claude_events, "load_states"),
    ):
        monkeypatch.setattr(target, name, lambda *a, **k: (_ for _ in ()).throw(
            AssertionError(f"{name} must not be read when collection is disabled")))
    snapshot = providers.build_snapshot()
    assert snapshot["providers"] == []
