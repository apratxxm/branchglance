from __future__ import annotations

import json
import ctypes
from pathlib import Path

from token_tracker import agent_links


def _filetime(epoch):
    return int((epoch + 11_644_473_600) * 10_000_000)


def test_resume_thread_parser_accepts_only_uuid_after_exec_resume():
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    assert agent_links.parse_exec_resume_thread(f'"codex.exe" exec resume {thread}') == thread
    assert agent_links.parse_exec_resume_thread(f'"codex.exe" exec resume {thread} --cwd C:/private') == thread
    assert agent_links.parse_exec_resume_thread(f"codex.exe exec resume {thread}x") is None
    assert agent_links.parse_exec_resume_thread(f"codex.exe resume {thread}") is None
    assert agent_links.parse_exec_invocation("codex.exe exec --json prompt") == ("exec", None, ())
    assert agent_links.parse_exec_invocation("codex.exe app-server --exec") is None
    assert agent_links.parse_exec_invocation("codex.exe exec resume") is None


def test_process_scan_requires_exact_valid_ancestry_and_unique_session():
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    states = {("session-a", ""): {"pid_associations": {
        "10:100": {"pid": 10, "created": 100, "first": 1, "last": 10},
    }}}
    table = {20: (10, "codex.exe"), 10: (1, "claude.exe"), 1: (0, "explorer.exe")}
    kwargs = dict(table=table, current_sid=b"sid", equal_sid=lambda left, right: left == right,
                  process_sid=lambda _pid: b"sid",
                  created=lambda pid: {20: _filetime(5), 10: 100, 1: 1}.get(pid),
                  command_line=lambda _pid: f"codex.exe exec resume {thread}", claude_states=states)
    found = agent_links.scan_codex_processes(**kwargs)
    assert found == [{"thread_id": thread, "codex_pid": 20, "codex_created": _filetime(5),
                      "claude_session_id": "session-a", "method": "cmdline"}]
    assert agent_links.scan_codex_processes(**{**kwargs, "created": lambda pid: _filetime(5)}) == []
    ambiguous = {**states, ("session-b", ""): states[("session-a", "")]}
    assert agent_links.scan_codex_processes(**{**kwargs, "claude_states": ambiguous}) == []
    assert agent_links.scan_codex_processes(**{**kwargs, "process_sid": lambda _pid: b"other"}) == []
    terminal_tree = {20: (1, "codex.exe"), 1: (0, "WindowsTerminal.exe")}
    assert agent_links.scan_codex_processes(**{**kwargs, "table": terminal_tree}) == []
    # PID 10 was reused: current creation time cannot match the recorded parent.
    assert agent_links.scan_codex_processes(
        **{**kwargs, "created": lambda pid: {20: _filetime(5), 10: 101, 1: 1}.get(pid)}
    ) == []


def test_fresh_exec_prompt_uuid_is_not_identity_but_resume_uuid_is():
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    table = {20: (1, "codex.exe"), 1: (0, "WindowsTerminal.exe")}
    kwargs = dict(table=table, current_sid=b"sid", equal_sid=lambda a, b: a == b,
                  process_sid=lambda _pid: b"sid", created=lambda pid: {20: _filetime(5), 1: 1}[pid],
                  watched_threads={thread}, claude_states={})
    line = f"codex.exe exec {thread}"
    found = agent_links.scan_codex_processes(**kwargs, command_line=lambda _pid: line)
    assert found == []
    found = agent_links.scan_codex_processes(
        **kwargs, command_line=lambda _pid: f"codex.exe exec resume {thread}")
    assert found == [{"thread_id": thread, "codex_pid": 20, "codex_created": _filetime(5),
                      "claude_session_id": None, "method": "unlinked"}]
    assert agent_links.scan_codex_processes(
        **kwargs, command_line=lambda _pid: "codex.exe app-server"
    ) == []


def test_process_scan_rechecks_child_and_each_ancestor_before_publish():
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    states = {("session-a", ""): {"pid_associations": {
        "10:100": {"pid": 10, "created": 100, "first": 1, "last": 10},
    }}}
    table = {20: (10, "codex.exe"), 10: (1, "claude.exe"), 1: (0, "explorer.exe")}
    counts = {}

    def created(pid):
        counts[pid] = counts.get(pid, 0) + 1
        base = {20: _filetime(5), 10: 100, 1: 1}[pid]
        # Reuse ancestor 10 after the initial ancestry walk.
        return base + (1 if pid == 10 and counts[pid] >= 2 else 0)

    result = agent_links.scan_codex_processes(
        table=table, current_sid=b"sid", equal_sid=lambda a, b: a == b,
        process_sid=lambda _pid: b"sid", created=created,
        command_line=lambda _pid: f"codex.exe exec resume {thread}", claude_states=states)
    assert result == []
    assert counts[20] >= 3 and counts[10] >= 2


def test_codex_identity_change_between_sid_and_command_line_reads_is_rejected():
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    count = 0

    def created(pid):
        nonlocal count
        if pid == 20:
            count += 1
            return _filetime(5) + (1 if count >= 2 else 0)
        return {10: 100, 1: 1}[pid]

    states = {("session-a", ""): {"pid_associations": {
        "10:100": {"pid": 10, "created": 100, "first": 1, "last": 10},
    }}}
    result = agent_links.scan_codex_processes(
        table={20: (10, "codex.exe"), 10: (1, "claude.exe"), 1: (0, "explorer.exe")},
        current_sid=b"sid", equal_sid=lambda a, b: a == b,
        process_sid=lambda _pid: b"sid", created=created,
        command_line=lambda _pid: f"codex.exe exec resume {thread}", claude_states=states)
    assert result == [] and count >= 2


def test_association_intervals_select_only_the_session_containing_exec_start():
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    start = _filetime(50)
    states = {
        ("old-session", ""): {"pid_associations": {
            "10:100": {"pid": 10, "created": 100, "first": 1, "last": 49},
        }},
        ("current-session", ""): {"pid_associations": {
            "10:100": {"pid": 10, "created": 100, "first": 50, "last": 80},
        }},
    }
    result = agent_links.scan_codex_processes(
        table={20: (10, "codex.exe"), 10: (1, "claude.exe"), 1: (0, "explorer.exe")},
        current_sid=b"sid", equal_sid=lambda a, b: a == b,
        process_sid=lambda _pid: b"sid",
        created=lambda pid: {20: start, 10: 100, 1: 1}[pid],
        command_line=lambda _pid: f"codex.exe exec resume {thread}", claude_states=states)
    assert [row["claude_session_id"] for row in result] == ["current-session"]


def test_latest_hook_association_remains_open_after_last_hook_event():
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    states = {("claude-session", ""): {"pid_associations": {
        "10:100": {"pid": 10, "created": 100, "first": 10, "last": 20},
    }}}
    result = agent_links.scan_codex_processes(
        table={20: (10, "codex.exe"), 10: (1, "claude.exe"), 1: (0, "explorer.exe")},
        current_sid=b"sid", equal_sid=lambda a, b: a == b,
        process_sid=lambda _pid: b"sid",
        created=lambda pid: {20: _filetime(300), 10: 100, 1: 1}[pid],
        command_line=lambda _pid: f"codex.exe exec resume {thread}", claude_states=states)
    assert [row["claude_session_id"] for row in result] == ["claude-session"]


def test_process_scan_accepts_twelve_hop_ancestry():
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    base = _filetime(1000)
    # Codex(20) -> ten intermediate processes -> Claude(9): 12 identities.
    table = {20: (19, "codex.exe")}
    for pid in range(19, 9, -1):
        table[pid] = (pid - 1, "worker.exe")
    table[9] = (1, "claude.exe")
    table[1] = (0, "explorer.exe")
    states = {("session-a", ""): {"pid_associations": {
        "9:900": {"pid": 9, "created": base + 9, "first": 900, "last": 1100},
    }}}
    result = agent_links.scan_codex_processes(
        table=table, current_sid=b"sid", equal_sid=lambda a, b: a == b,
        process_sid=lambda _pid: b"sid",
        created=lambda pid: base + pid,
        command_line=lambda _pid: f"codex.exe exec resume {thread}", claude_states=states)
    assert len(result) == 1 and result[0]["claude_session_id"] == "session-a"


def test_command_line_query_retries_reported_large_size_once():
    text = "x" * 20_000
    encoded = text.encode("utf-16-le")
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    header = (8 if pointer_size == 8 else 4) + pointer_size
    required = header + len(encoded)
    calls = []

    def query(buffer, capacity):
        calls.append(capacity)
        if buffer is None:
            return -1, required
        base = ctypes.addressof(buffer)
        buffer.raw = (len(encoded).to_bytes(2, "little")
                      + len(encoded).to_bytes(2, "little")
                      + bytes((8 if pointer_size == 8 else 4) - 4)
                      + (base + header).to_bytes(pointer_size, "little")
                      + encoded)
        return 0, required

    assert agent_links._read_process_command_line(query, pointer_size=pointer_size) == text
    assert calls == [0, required]


def test_receipts_are_bounded_validated_and_never_set_status(tmp_path):
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert agent_links.write_receipt(thread, 22, environ={"CLAUDE_CODE_SESSION_ID": sid},
                                     created=lambda _pid: 123, root=tmp_path,
                                     permission_check=lambda _policies: True,
                                     pid_check=lambda _pid, _thread: True)
    receipts = agent_links.read_receipts(tmp_path, permission_check=lambda _policies: True,
                                         owner_check=lambda: True)
    assert len(receipts) == 1
    assert receipts[0]["method"] == "receipt"
    assert "status" not in receipts[0]
    assert not agent_links.write_receipt("bad", 22, environ={"CLAUDE_CODE_SESSION_ID": sid},
                                         created=lambda _pid: 123, root=tmp_path,
                                         permission_check=lambda _policies: True,
                                         pid_check=lambda _pid, _thread: True)


def test_receipt_policy_denial_precedes_process_or_filesystem_access(tmp_path):
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    fail = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not read"))
    assert not agent_links.write_receipt(
        thread, 42, environ={"CLAUDE_CODE_SESSION_ID": sid}, root=tmp_path,
        permission_check=lambda _policies: False, created=fail, pid_check=fail)
    assert agent_links.read_receipts(
        tmp_path, permission_check=lambda _policies: False, owner_check=fail) == []
    assert list(tmp_path.iterdir()) == []


def test_receipt_pid_must_be_current_user_codex_and_thread_consistent():
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    kwargs = dict(table_reader=lambda: {42: (1, "codex.exe")},
                  current_sid=lambda: b"current", process_sid=lambda _pid: b"current",
                  equal_sid=lambda left, right: left == right, created=lambda _pid: 500,
                  command_line=lambda _pid: f"codex.exe exec resume {thread}")
    assert agent_links.validate_codex_pid(42, thread, **kwargs)
    assert not agent_links.validate_codex_pid(
        42, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", **kwargs)
    assert not agent_links.validate_codex_pid(
        42, thread, **{**kwargs, "table_reader": lambda: {42: (1, "python.exe")}})
    assert not agent_links.validate_codex_pid(
        42, thread, **{**kwargs, "process_sid": lambda _pid: b"other"})
    fresh = {**kwargs, "command_line": lambda _pid: "codex.exe exec --json prompt"}
    assert agent_links.validate_codex_pid(42, thread, **fresh)
    assert agent_links.validate_codex_pid(
        42, thread, **{**kwargs, "command_line": lambda _pid: "codex.exe app-server"}) is None


def test_receipt_merge_rejects_thread_different_from_codex_execution(monkeypatch):
    _sid, _thread, grouped, _run = _link_fixture()
    receipt = {"thread_id": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
               "codex_pid": 42, "codex_created": 500,
               "claude_session_id": grouped[0]["chats"][0]["id"],
               "recorded_at": 100, "method": "receipt"}
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", float("-inf"))
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: True)
    agent_links.apply_exact_links(
        grouped, now=200, registry_path=Path("unused"), permission_check=_allow_collection,
        process_table_reader=lambda: {42: (1, "codex.exe")},
        current_sid_reader=lambda: b"sid",
        process_sid=lambda _pid: b"sid",
        equal_sid=lambda left, right: left == right,
        process_created_reader=lambda _pid: 500,
        command_line=lambda _pid: "codex.exe exec resume 01234567-89ab-cdef-0123-456789abcdef",
        registry_reader=lambda _path: [], hooks=lambda: {},
        scan=lambda **_kwargs: [],
        receipts_reader=lambda _root: [receipt],
        # Ownership is required for the inbox read and the merge.
        )
    assert "parentId" not in grouped[1]["chats"][0]


def test_registry_validates_and_atomically_retains_seven_days(tmp_path):
    path = tmp_path / "registry.json"
    good = {"thread_id": "01234567-89ab-cdef-0123-456789abcdef", "codex_pid": 22,
            "codex_created": 123, "claude_session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "first_seen": 1, "last_seen": 9, "method": "receipt"}
    old = {**good, "last_seen": 10 - agent_links.RETENTION_SECONDS - 1}
    assert agent_links.write_registry([good, old, {**good, "thread_id": "invalid"}], path, now=10)
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value == {"version": 1, "runs": [good]}


def test_registry_load_rejects_nonfinite_future_and_expired_times(tmp_path):
    path = tmp_path / "registry.json"
    good = {"thread_id": "01234567-89ab-cdef-0123-456789abcdef", "codex_pid": 22,
            "codex_created": 123, "claude_session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "first_seen": 2, "last_seen": 9, "verified_at": 8, "method": "receipt"}
    rows = [
        good,
        {**good, "last_seen": float("nan")},
        {**good, "last_seen": 11},
        {**good, "first_seen": 2, "last_seen": 1},
        {**good, "first_seen": float("inf")},
        {**good, "last_seen": 10 - agent_links.RETENTION_SECONDS - 1,
         "first_seen": 10 - agent_links.RETENTION_SECONDS - 2},
    ]
    path.write_text(json.dumps({"version": 1, "runs": rows}), encoding="utf-8")
    assert agent_links.load_registry(path, now=10) == [good]


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


def test_live_graph_is_full_for_every_poller(monkeypatch):
    # Two clients polling /api/live: the second must not get an empty edge
    # map just because the first one already saw the new revision.
    monkeypatch.setattr(agent_links, "_GRAPH_EDGES", {})
    monkeypatch.setattr(agent_links, "_GRAPH_REVISION", 0)
    parent = {"id": "claude-id", "provider": "Claude"}
    child = {"id": "codex-id", "provider": "Codex", "parentId": "claude-id", "parentProvider": "Claude"}
    first = agent_links.snapshot_graph([dict(parent), dict(child)])
    second = agent_links.snapshot_graph([dict(parent), dict(child)])
    assert first["edges"] == second["edges"] == {
        "Claude:claude-id": None, "Codex:codex-id": "Claude:claude-id"}
    import inspect
    from token_tracker import providers
    assert "delta=True" not in inspect.getsource(providers.live_state)


def test_verify_process_retires_gone_runs_but_not_unknown_ones(monkeypatch):
    monkeypatch.setattr(agent_links, "_probe_process", lambda pid: ("gone", None))
    assert agent_links.verify_process(17644, 123) is False
    monkeypatch.setattr(agent_links, "_probe_process", lambda pid: ("unknown", None))
    assert agent_links.verify_process(17644, 123) is None
    monkeypatch.setattr(agent_links, "_probe_process", lambda pid: ("alive", 123))
    assert agent_links.verify_process(17644, 123) is True
    monkeypatch.setattr(agent_links, "_probe_process", lambda pid: ("alive", 999))
    assert agent_links.verify_process(17644, 123) is False


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


def _allow_collection(_policies):
    return True


def test_link_poll_skips_full_scan_for_recently_verified_execution(monkeypatch):
    _sid, _thread, providers, run = _link_fixture()
    called = []
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 104)
    agent_links.apply_exact_links(
        providers, now=105, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [dict(run)],
        verify=lambda *_args: called.append("verify"),
        scan=lambda **_kwargs: called.append("scan"),
        hooks=lambda: called.append("hooks"),
        receipts_reader=lambda _root: called.append("receipts"),
    )
    assert called == []


def test_expired_verification_checks_known_process_without_full_scan(monkeypatch):
    _sid, _thread, providers, run = _link_fixture()
    run["verified_at"] = 100
    called = []
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 105)
    agent_links.apply_exact_links(
        providers, now=111, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [dict(run)],
        verify=lambda pid, created: called.append((pid, created)) or True,
        scan=lambda **_kwargs: called.append("scan"),
        hooks=lambda: called.append("hooks"),
        receipts_reader=lambda _root: called.append("receipts"),
        process_table_reader=lambda: {},
    )
    assert called == [(22, 123)]


def test_full_discovery_runs_at_ten_seconds_even_if_known_pid_verifies(monkeypatch):
    _sid, _thread, providers, run = _link_fixture()
    run["verified_at"] = 105
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 100)
    calls = []
    agent_links.apply_exact_links(
        providers, now=109, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [dict(run)],
        verify=lambda *_args: calls.append("verify") or True,
        scan=lambda **_kwargs: calls.append("scan") or [],
        process_table_reader=lambda: {}, hooks=lambda: {},
        receipts_reader=lambda _root: [],
    )
    assert calls == []
    agent_links.apply_exact_links(
        _link_fixture()[2], now=110, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [dict(run)],
        verify=lambda *_args: calls.append("verify") or True,
        scan=lambda **_kwargs: calls.append("scan") or [],
        process_table_reader=lambda: {}, hooks=lambda: {},
        receipts_reader=lambda _root: [],
    )
    assert calls == ["scan"]


def test_unverifiable_known_process_blocks_without_bypassing_scan_cadence(monkeypatch):
    _sid, _thread, providers, run = _link_fixture()
    run["verified_at"] = 100
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 109)
    calls = []
    agent_links.apply_exact_links(
        providers, now=110, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [dict(run)],
        verify=lambda *_args: None,
        scan=lambda **_kwargs: calls.append("scan") or [],
        process_table_reader=lambda: {}, hooks=lambda: {},
        receipts_reader=lambda _root: [],
    )
    assert calls == []
    assert "parentId" not in providers[1]["chats"][0]


def test_new_exec_triggers_scan_and_links_only_exact_session():
    sid, thread, providers, run = _link_fixture()
    run["verified_at"] = 0
    calls = []
    agent_links.apply_exact_links(
        providers, now=105, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [],
        hooks=lambda: {},
        scan=lambda **kwargs: calls.append("scan") or [{**run, "method": "cmdline"}],
        receipts_reader=lambda _root: calls.append("receipts") or [],
        registry_writer=lambda *_args, **_kwargs: True,
        process_table_reader=lambda: {},
    )
    assert calls == ["scan"]
    assert providers[1]["chats"][0]["parentId"] == sid
    assert providers[1]["chats"][0]["parentProvider"] == "Claude"
    assert providers[1]["chats"][0]["id"] == thread


def test_previous_claude_parent_survives_live_index_and_is_revalidated(monkeypatch):
    sid, _thread, grouped, run = _link_fixture()
    codex_row = grouped[1]["chats"][0]
    codex_row.update(parentId=sid, parentProvider="Claude")
    from token_tracker import providers
    monkeypatch.setattr(providers, "_LIVE_INDEX", {})
    monkeypatch.setattr(providers, "_LIVE_INDEX_POLICY_GENERATION", None)
    providers._index_live_paths([{"name": "Codex", "chats": [{
        **codex_row, "path": "synthetic-rollout.jsonl",
    }]}], "generation")
    assert providers._LIVE_INDEX[f"Codex:{codex_row['id']}"]["parentProvider"] == "Claude"
    agent_links.apply_exact_links(
        grouped, now=105, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [run],
        scan=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("recent live link is verified")),
    )
    assert codex_row["parentId"] == sid and codex_row["parentProvider"] == "Claude"


def test_native_parent_replaces_cached_claude_parent_as_one_pair():
    from token_tracker import providers

    indexed = {"parentId": "claude-session", "parentProvider": "Claude"}
    assert providers._codex_parent_pair("native-thread", indexed) == ("native-thread", "Codex")
    assert providers._codex_parent_pair(None, indexed) == ("claude-session", "Claude")


def test_native_codex_parent_is_not_rechecked_as_cross_provider_link():
    sid, _thread, grouped, _run = _link_fixture()
    row = grouped[1]["chats"][0]
    row.update(parentId="native-parent", parentProvider="Codex")
    agent_links.apply_exact_links(
        grouped, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [],
        scan=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("native parent is preserved")),
    )
    assert row["parentId"] == "native-parent" and row["parentProvider"] == "Codex"
    assert sid != row["parentId"]


def test_cached_claude_parent_without_provider_is_revalidated_while_running(monkeypatch):
    sid, _thread, grouped, run = _link_fixture()
    row = grouped[1]["chats"][0]
    row.update(parentId=sid, parentProvider=None)
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 104)
    agent_links.apply_exact_links(
        grouped, now=105, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [dict(run)],
        scan=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("recent live link is verified")),
    )
    assert row["parentId"] == sid and row["parentProvider"] == "Claude"


def test_untagged_parent_that_is_not_a_claude_session_stays_native():
    _sid, _thread, grouped, _run = _link_fixture()
    row = grouped[1]["chats"][0]
    row.update(parentId="native-parent", parentProvider=None)
    agent_links.apply_exact_links(
        grouped, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [],
        scan=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("native parent is preserved")),
    )
    assert row["parentId"] == "native-parent" and row.get("parentProvider") is None


def test_untagged_parent_matching_both_a_codex_and_a_claude_id_stays_native():
    sid, _thread, grouped, run = _link_fixture()
    grouped[1]["chats"].append({"id": sid, "source": "exec", "executionStatus": "idle"})
    row = grouped[1]["chats"][0]
    row.update(parentId=sid, parentProvider=None)
    agent_links.apply_exact_links(
        grouped, now=105, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [dict(run)],
        scan=lambda **_kwargs: [],
    )
    assert row["parentId"] == sid and row.get("parentProvider") is None


def test_idle_parent_is_invalidated_by_newer_plain_terminal_turn():
    sid, _thread, providers, run = _link_fixture()
    providers[1]["chats"][0].update(source="exec", executionStatus="idle",
                                    runStartedAt="1970-01-01T00:03:00Z")
    run["last_seen"] = 100
    run["verified_at"] = 100
    agent_links.apply_exact_links(
        providers, now=105, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [run], scan=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("idle rows never scan")), verify=lambda *_args: None,
    )
    assert "parentId" not in providers[1]["chats"][0]
    assert sid != providers[1]["chats"][0].get("parentId")


def test_conflicting_concurrent_runs_remain_unlinked():
    sid, _thread, providers, run = _link_fixture()
    other = {**run, "claude_session_id": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
             "codex_pid": 23, "codex_created": 124}
    agent_links._LAST_FULL_SCAN_AT = float("-inf")
    agent_links.apply_exact_links(
        providers, now=105, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [run, other], hooks=lambda: {},
        process_table_reader=lambda: {},
        scan=lambda **_kwargs: [{**run, "method": "cmdline"}, {**other, "method": "cmdline"}],
        receipts_reader=lambda _root: [],
    )
    assert "parentId" not in providers[1]["chats"][0]
    assert sid != providers[1]["chats"][0].get("parentId")


def test_all_live_registry_candidates_are_reverified_and_ambiguity_persists(monkeypatch):
    _sid, _thread, providers, first = _link_fixture()
    first["verified_at"] = 90
    second = {**first, "claude_session_id": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
              "codex_pid": 23, "codex_created": 124}
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 104)
    calls = []
    agent_links.apply_exact_links(
        providers, now=105, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [first, second],
        verify=lambda pid, _created: calls.append(pid) or (True if pid == 22 else None),
        scan=lambda **_kwargs: [], process_table_reader=lambda: {}, hooks=lambda: {},
        receipts_reader=lambda _path: [],
    )
    assert calls == [22, 23]
    assert "parentId" not in providers[1]["chats"][0]


def test_unlinked_terminal_exec_blocks_cached_parent_between_scans(monkeypatch):
    _sid, thread, providers, run = _link_fixture()
    unlinked = {"thread_id": thread, "codex_pid": 44, "codex_created": 444,
                "claude_session_id": None, "method": "unlinked"}
    monkeypatch.setattr(agent_links, "_UNLINKED_EXECUTIONS", {})
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", float("-inf"))
    agent_links.apply_exact_links(
        providers, now=1000, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [], hooks=lambda: {},
        process_table_reader=lambda: {},
        scan=lambda **_kwargs: [unlinked], receipts_reader=lambda _path: [],
    )
    assert "parentId" not in providers[1]["chats"][0]
    providers[1]["chats"][0].update(parentId="cached-claude", parentProvider="Claude")
    agent_links.apply_exact_links(
        providers, now=1001, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [run],
        verify=lambda _pid, _created: True,
        scan=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("negative result remains cached")),
    )
    assert "parentId" not in providers[1]["chats"][0]
    agent_links.apply_exact_links(
        providers, now=1002, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [run],
        verify=lambda pid, _created: False if pid == 44 else True,
        scan=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("retirement is revalidated")),
    )
    assert providers[1]["chats"][0]["parentId"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_negative_discovery_result_respects_ten_second_scan_cadence(monkeypatch):
    _sid, _thread, providers, _run = _link_fixture()
    monkeypatch.setattr(agent_links, "_UNLINKED_EXECUTIONS", {})
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 100)
    calls = []
    def poll(now):
        agent_links.apply_exact_links(
            _link_fixture()[2], now=now, registry_path=Path("unused"),
            permission_check=_allow_collection, registry_reader=lambda _path: [],
            process_table_reader=lambda: {}, hooks=lambda: {},
            scan=lambda **_kwargs: calls.append(now) or [], receipts_reader=lambda _path: [],
        )
    poll(109)
    poll(110)
    poll(111)
    assert calls == [110]


def test_dead_historical_run_is_retired_without_repeated_full_scans(monkeypatch):
    _sid, _thread, providers, dead = _link_fixture()
    dead["verified_at"] = 90
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 100)
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: True)
    saved = []
    current_registry = [dict(dead)]
    common = dict(
        registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [dict(run) for run in current_registry], process_table_reader=lambda: (
            (_ for _ in ()).throw(AssertionError("negative scan cadence"))),
        hooks=lambda: (_ for _ in ()).throw(AssertionError("no hook reads")),
        receipts_reader=lambda _path: (_ for _ in ()).throw(AssertionError("no receipts")),
        scan=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("negative scan cadence")),
        registry_writer=lambda runs, _path, now=None: (
            saved.extend(dict(run) for run in runs),
            current_registry.__setitem__(slice(None), [dict(run) for run in runs]),
            True)[-1],
    )
    agent_links.apply_exact_links(providers, now=105, verify=lambda *_args: False, **common)
    assert saved[0]["retired_at"] == 105
    agent_links.apply_exact_links(providers, now=106, verify=lambda *_args: (
        _ for _ in ()).throw(AssertionError("retired identity is no longer verified")), **common)


def test_full_discovery_finds_new_execution_despite_verified_old_pid(monkeypatch):
    _sid, _thread, providers, old = _link_fixture()
    old.update(claude_session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
               verified_at=105, last_seen=105, method="cmdline")
    new = {**old, "claude_session_id": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
           "codex_pid": 23, "codex_created": 124, "method": "cmdline"}
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 95)
    calls = []
    agent_links.apply_exact_links(
        providers, now=105, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [dict(old)],
        verify=lambda *_args: calls.append("verify") or True,
        scan=lambda **_kwargs: calls.append("scan") or [new],
        hooks=lambda: {}, receipts_reader=lambda _root: [],
        process_table_reader=lambda: {},
    )
    assert calls == ["scan"]
    assert "parentId" not in providers[1]["chats"][0]


def test_sequential_session_history_selects_latest_idle_parent():
    first_sid, thread, providers, old = _link_fixture()
    second_sid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    providers[1]["chats"][0].update(source="exec", executionStatus="idle",
                                    runStartedAt="1970-01-01T00:01:30Z")
    latest = {**old, "claude_session_id": second_sid, "codex_pid": 23,
              "codex_created": 124, "first_seen": 110, "last_seen": 120,
              "verified_at": 120, "method": "receipt"}
    providers[0]["chats"].append({"id": second_sid})
    agent_links.apply_exact_links(
        providers, now=125, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [old, latest],
        scan=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("idle history does not scan")),
    )
    assert providers[1]["chats"][0]["parentId"] == second_sid
    assert providers[1]["chats"][0]["parentId"] != first_sid
    assert providers[1]["chats"][0]["id"] == thread


def test_missing_provider_policy_pair_reads_no_link_inputs():
    calls = []
    agent_links.apply_exact_links(
        [{"name": "Claude", "chats": []}], registry_path=Path("unused"),
        registry_reader=lambda _path: calls.append("registry"),
        scan=lambda **_kwargs: calls.append("scan"),
        hooks=lambda: calls.append("hooks"),
        receipts_reader=lambda _root: calls.append("receipts"),
    )
    assert calls == []


def test_receipts_are_not_read_without_instance_mutex(monkeypatch):
    _sid, _thread, grouped, _run = _link_fixture()
    calls = []
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: False)
    agent_links._LAST_FULL_SCAN_AT = float("-inf")
    agent_links.apply_exact_links(
        grouped, now=200, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [], process_table_reader=lambda: {},
        hooks=lambda: {}, scan=lambda **_kwargs: [],
        receipts_reader=lambda _root: calls.append("receipt-read") or [],
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
