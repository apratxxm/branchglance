from __future__ import annotations

import json
import os
import ctypes
from pathlib import Path
import pytest

from token_tracker import agent_links
from token_tracker import claude_events


@pytest.fixture(autouse=True)
def isolate_relative_registry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_links, "_RUN_ROWS", {})
    monkeypatch.setattr(agent_links, "_CLAUDE_PROCESS_LINKS", {})
    monkeypatch.setattr(agent_links, "_CLAUDE_LINK_CONTRADICTIONS", set())
    monkeypatch.setattr(agent_links, "_RECEIPT_CONFLICTS", set())
    monkeypatch.setattr(agent_links, "_CODEX_LINK_CONTRADICTIONS", set())
    monkeypatch.setattr(agent_links, "_CODEX_PARENT_CONTRADICTIONS", set())
    monkeypatch.setattr(agent_links, "_CONTRADICTION_SEEN", {})
    monkeypatch.setattr(agent_links, "_OBSERVED_IDENTITIES", {})


@pytest.fixture
def receipt_inbox_creation(monkeypatch):
    # Restricted Windows sandbox tokens cannot access objects protected solely
    # for the user SID. Exercise receipt behavior without changing that DACL.
    def create(directory, file):
        directory.mkdir(exist_ok=True)
        file.touch(exist_ok=True)
    monkeypatch.setattr(agent_links, "_create_private_inbox", create)


def _filetime(epoch):
    return int((epoch + 11_644_473_600) * 10_000_000)


def _reduced_pid_session(sid, pid, created, first, last=None):
    file_state = claude_events.new_file_state()
    payload = {"session_id": sid, "claude_pid": pid, "claude_pid_created": created}
    claude_events.reduce_event(file_state, {**payload, "ts": first, "event": "SessionStart"})
    if last is not None and last > first:
        claude_events.reduce_event(file_state, {**payload, "ts": last, "event": "UserPromptSubmit"})
    return file_state["keys"][(sid, "")]


def test_resume_thread_parser_accepts_only_uuid_after_exec_resume():
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    assert agent_links.parse_exec_resume_thread(f'"codex.exe" exec resume {thread}') == thread
    assert agent_links.parse_exec_resume_thread(f'"codex.exe" exec resume {thread} --cwd C:/private') == thread
    assert agent_links.parse_exec_resume_thread(f"codex.exe exec resume {thread}x") is None
    assert agent_links.parse_exec_resume_thread(f"codex.exe resume {thread}") is None
    assert agent_links.parse_exec_invocation("codex.exe exec --json prompt") == ("exec", None, ())
    assert agent_links.parse_exec_invocation("codex.exe app-server --exec") is None
    assert agent_links.parse_exec_invocation("codex.exe exec resume") is None


def test_structured_argv_parser_preserves_prompt_tokens_and_resume_identity():
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    assert agent_links.parse_exec_argv(["codex", "exec", "resume", thread]) == ("resume", thread, (thread,))
    assert agent_links.parse_exec_argv(["codex", "exec", f"resume {thread}"]) == ("exec", None, ())
    assert agent_links.parse_exec_argv(["node", "C:\\node_modules\\@openai\\codex\\bin\\codex.js",
                                        "exec", "resume", thread]) == ("resume", thread, (thread,))
    prompt = ["codex", "exec", "a prompt with spaces", 'a "quoted" token', ""]
    assert agent_links.parse_exec_argv(prompt) == ("exec", None, ())
    assert agent_links.parse_exec_argv(["codex", "exec", "resume", thread + "x"]) is None


def test_agent_recognizer_stops_on_unreadable_node_argv():
    assert agent_links.classify_agent_process("node.exe", None) == "unknown"
    assert agent_links.classify_agent_process("node.exe", ["node", "script.js"]) == "other"
    assert agent_links.classify_agent_process("node.exe", ["node", "node_modules/@openai/codex/bin/codex.js"]) == "codex"


def test_claude_launcher_requires_one_session_interval_for_direct_child_and_child():
    parent_created, direct_created, child_created = map(_filetime, (900, 950, 1000))
    table = {30: (20, "claude.exe"), 20: (10, "zsh"), 10: (1, "claude.exe"), 1: (0, "launchd")}
    states = {
        ("child", ""): _reduced_pid_session("child", 30, child_created, 1001),
        ("parent-a", ""): _reduced_pid_session("parent-a", 10, parent_created, 900, 1200),
    }
    found = agent_links.scan_claude_process_links(
        claude_states=states, table=table, current_sid=b"sid",
        equal_sid=lambda left, right: left == right, process_sid=lambda _pid: b"sid",
        created=lambda pid: {30: child_created, 20: direct_created, 10: parent_created,
                             1: _filetime(800)}[pid],
        command_argv=lambda _pid: None)
    assert len(found) == 1 and found[0]["parent_session_id"] == "parent-a"
    states[("parent-a", "")] = _reduced_pid_session("parent-a", 10, parent_created, 900, 999)
    states[("parent-b", "")] = _reduced_pid_session("parent-b", 10, parent_created, 1000, 1200)
    assert agent_links.scan_claude_process_links(
        claude_states=states, table=table, current_sid=b"sid",
        equal_sid=lambda left, right: left == right, process_sid=lambda _pid: b"sid",
        created=lambda pid: {30: child_created, 20: direct_created, 10: parent_created,
                             1: _filetime(800)}[pid], command_argv=lambda _pid: None) == []


def test_resumed_child_session_with_shared_process_identity_is_ambiguous():
    parent_created, direct_created, child_created = map(_filetime, (900, 950, 1000))
    table = {30: (20, "claude.exe"), 20: (10, "zsh"), 10: (1, "claude.exe"), 1: (0, "launchd")}
    states = {
        ("child-a", ""): _reduced_pid_session("child-a", 30, child_created, 1001),
        ("child-b", ""): _reduced_pid_session("child-b", 30, child_created, 1002),
        ("parent", ""): _reduced_pid_session("parent", 10, parent_created, 900, 1200),
    }
    assert agent_links.scan_claude_process_links(
        claude_states=states, table=table, current_sid=b"sid",
        equal_sid=lambda left, right: left == right, process_sid=lambda _pid: b"sid",
        created=lambda pid: {30: child_created, 20: direct_created, 10: parent_created,
                             1: _filetime(800)}[pid], command_argv=lambda _pid: None) == []


def test_claude_launcher_chain_change_between_snapshots_is_rejected():
    parent_created, direct_created, child_created = map(_filetime, (900, 950, 1000))
    first = {30: (20, "claude.exe"), 20: (10, "zsh"), 10: (1, "claude.exe"), 1: (0, "launchd")}
    changed = {**first, 20: (11, "zsh")}
    states = {
        ("child", ""): {"pid_associations": [{"pid": 30, "created": child_created,
                                               "sid": "child", "first": 1000, "last": 1000}]},
        ("parent", ""): {"pid_associations": [{"pid": 10, "created": parent_created,
                                                "sid": "parent", "first": 900, "last": 1200}]},
    }
    result = agent_links.scan_claude_process_links(
        claude_states=states, table=first, table_reader=lambda: changed, current_sid=b"sid",
        equal_sid=lambda left, right: left == right, process_sid=lambda _pid: b"sid",
        created=lambda pid: {30: child_created, 20: direct_created, 10: parent_created,
                             11: _filetime(850), 1: _filetime(800)}[pid],
        command_argv=lambda _pid: None)
    assert result == []


def test_codex_launcher_switch_off_disables_environment_and_receipt_attribution(monkeypatch):
    monkeypatch.setattr(agent_links, "_CODEX_ENV_PER_THREAD_VERIFIED", False)
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    parent_created, direct_created, child_created = map(_filetime, (900, 950, 1000))
    table = {30: (20, "claude.exe"), 20: (10, "zsh"), 10: (1, "codex.exe"), 1: (0, "launchd")}
    states = {
        ("child", ""): {"pid_associations": [{"pid": 30, "created": child_created,
                                               "sid": "child", "first": 1000, "last": 1000}],
                          "launcher_associations": [{"pid": 30, "created": child_created,
                                                     "launcher_codex_thread_id": thread}]},
    }
    assert agent_links.scan_claude_process_links(
        claude_states=states, table=table,
        codex_threads=[{"id": thread, "runPid": 10, "runCreated": str(parent_created)}],
        current_sid=b"sid", equal_sid=lambda left, right: left == right,
        process_sid=lambda _pid: b"sid",
        created=lambda pid: {30: child_created, 20: direct_created, 10: parent_created,
                             1: _filetime(800)}[pid], command_argv=lambda _pid: None) == []


def test_receipt_without_claude_parent_is_written_and_old_receipts_still_read(tmp_path, receipt_inbox_creation):
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    assert agent_links.initialize_receipt_inbox(tmp_path)
    assert agent_links.write_receipt(thread, 22, environ={"CODEX_THREAD_ID": thread},
                                     created=lambda _pid: 123, root=tmp_path,
                                     permission_check=lambda _policies: True,
                                     pid_check=lambda _pid, _thread: True)
    old = {"thread_id": thread, "codex_pid": 23, "codex_created": 124,
           "claude_session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
           "recorded_at": 1, "method": "receipt"}
    path = tmp_path / agent_links.INBOX_NAME / agent_links.RECEIPT_FILE
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(old) + "\n")
    found = agent_links.read_receipts(tmp_path, permission_check=lambda _: True,
                                      owner_check=lambda: True)
    assert len(found) == 2
    assert found[0]["thread_id"] == thread and "claude_session_id" not in found[0]
    assert found[1]["claude_session_id"] == old["claude_session_id"]


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
                      "claude_pid": 10, "claude_created": 100,
                      "direct_child_pid": 20, "direct_child_created": _filetime(5),
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
                      "claude_pid": None, "claude_created": None,
                      "direct_child_pid": None, "direct_child_created": None,
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


@pytest.mark.skipif(os.name != "nt", reason="Windows-only: decodes the UTF-16 PROCESS_COMMAND_LINE_INFORMATION buffer")
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


def test_receipts_are_bounded_validated_and_never_set_status(tmp_path, receipt_inbox_creation):
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert agent_links.initialize_receipt_inbox(tmp_path)
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


def test_receipt_missing_file_is_not_created(tmp_path):
    directory = tmp_path / agent_links.INBOX_NAME
    directory.mkdir()
    assert not _write_test_receipt(tmp_path)
    assert not (directory / agent_links.RECEIPT_FILE).exists()


def test_codex_only_receipt_binds_child_run_without_a_parent(tmp_path, monkeypatch):
    _, thread, providers, _ = _link_fixture()
    row = providers[1]["chats"][0]
    row.pop("runPid"); row.pop("runCreated")
    receipt = {"thread_id": thread, "codex_pid": 22, "codex_created": 123,
               "launcher_codex_thread_id": thread, "recorded_at": 100, "method": "receipt"}
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", float("-inf"))
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: True)
    agent_links.apply_exact_links(
        providers, root=tmp_path, now=200, registry_path=tmp_path / "registry.json",
        permission_check=_allow_collection, registry_reader=lambda _: [],
        registry_writer=lambda *a, **k: True, receipts_reader=lambda _: [receipt],
        process_table_reader=lambda: {22: (1, "codex.exe")},
        current_sid_reader=lambda: b"sid", process_sid=lambda _: b"sid",
        equal_sid=lambda left, right: left == right,
        process_created_reader=lambda _pid: 123,
        command_line=lambda _pid: f"codex.exe exec resume {thread}",
        scan=lambda **_kwargs: [], hooks=lambda: {})
    assert row["runPid"] == 22 and row["runVerified"] is True
    assert "parentId" not in row


def test_conflicting_receipts_revoke_frozen_parent_for_child_run(tmp_path, monkeypatch):
    sid, thread, providers, _ = _link_fixture()
    row = providers[1]["chats"][0]
    row.update(parentId=sid, parentProvider="Claude", linkSource="process")
    identity = (thread, 22, 123)
    monkeypatch.setattr(agent_links, "_RUN_ROWS", {identity: {
        "row": dict(row), "parent": sid, "parentProvider": "Claude", "stoppedAt": None,
    }})
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", float("-inf"))
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: True)
    receipts = [
        {"thread_id": thread, "codex_pid": 22, "codex_created": 123,
         "launcher_claude_session_id": sid,
         "recorded_at": 100, "method": "receipt"},
        {"thread_id": thread, "codex_pid": 22, "codex_created": 123,
         "launcher_claude_session_id": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
         "recorded_at": 101, "method": "receipt"},
    ]
    agent_links.apply_exact_links(
        providers, root=tmp_path, now=200, registry_path=tmp_path / "registry.json",
        permission_check=_allow_collection, registry_reader=lambda _: [],
        registry_writer=lambda *a, **k: True, receipts_reader=lambda _: receipts,
        process_table_reader=lambda: {22: (1, "codex.exe")},
        current_sid_reader=lambda: b"sid", process_sid=lambda _: b"sid",
        equal_sid=lambda left, right: left == right,
        process_created_reader=lambda _pid: 123,
        command_line=lambda _pid: f"codex.exe exec resume {thread}",
        scan=lambda **_kwargs: [], verify=lambda *_: True, hooks=lambda: {})
    assert "parentId" not in row
    assert agent_links._RUN_ROWS[identity]["parent"] is None


def test_codex_frozen_parent_changes_when_current_scan_proves_other_owner(tmp_path, monkeypatch):
    sid_a, sid_b = "session-a", "session-b"
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    parent_created, direct_created, child_created = map(_filetime, (900, 980, 1000))
    codex_row = {"id": thread, "source": "exec", "status": "running",
                 "executionStatus": "running", "runPid": 22, "runCreated": str(child_created),
                 "parentId": sid_a, "parentProvider": "Claude", "linkSource": "process"}
    providers = [{"name": "Claude", "chats": [{"id": sid_a}, {"id": sid_b}]},
                 {"name": "Codex", "chats": [codex_row]}]
    run_key = (thread, 22, child_created)
    saved_row = dict(codex_row)
    monkeypatch.setattr(agent_links, "_RUN_ROWS", {run_key: {
        "row": saved_row, "parent": sid_a, "parentProvider": "Claude", "stoppedAt": None,
        "processProof": {"parent_id": sid_a, "parent_pid": 10, "parent_created": parent_created,
                         "direct_child_pid": 20, "direct_child_created": direct_created,
                         "child_pid": 22, "child_created": child_created},
    }})
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", float("-inf"))
    states = {
        (sid_a, ""): {"pid_associations": [{"pid": 10, "created": parent_created,
                                              "sid": sid_a, "first": 900, "last": 949}]},
        (sid_b, ""): {"pid_associations": [{"pid": 10, "created": parent_created,
                                              "sid": sid_b, "first": 950, "last": 1200}]},
    }
    current_scan = [{"thread_id": thread, "codex_pid": 22, "codex_created": child_created,
                     "claude_session_id": sid_b, "claude_pid": 10, "claude_created": parent_created,
                     "direct_child_pid": 20, "direct_child_created": direct_created, "method": "cmdline"}]
    agent_links.apply_exact_links(
        providers, now=2000, registry_path=tmp_path / "registry.json",
        permission_check=_allow_collection, registry_reader=lambda _: [],
        registry_writer=lambda *a, **k: True, process_table_reader=lambda: {},
        current_sid_reader=lambda: b"sid", process_sid=lambda _pid: b"sid",
        equal_sid=lambda left, right: left == right,
        process_created_reader=lambda _pid: None, scan=lambda **_kwargs: current_scan,
        verify=lambda *_: True, hooks=lambda: states)
    assert codex_row["parentId"] == sid_b
    assert agent_links._RUN_ROWS[run_key]["processProof"]["parent_id"] == sid_b


def test_codex_proof_survives_registry_roundtrip_then_blocks_contradicted_old_parent(tmp_path, monkeypatch):
    sid_a, sid_b = "session-a", "session-b"
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    parent_created, direct_created, child_created = map(_filetime, (900, 980, 1000))
    claude = {"name": "Claude", "chats": [{"id": sid_a}, {"id": sid_b}]}
    codex_row = {"id": thread, "source": "exec", "status": "running",
                 "executionStatus": "running", "runPid": 22, "runCreated": str(child_created),
                 "parentId": sid_a, "parentProvider": "Claude", "linkSource": "process"}
    providers = [claude, {"name": "Codex", "chats": [codex_row]}]
    run_key = (thread, 22, child_created)
    proof = {"parent_id": sid_a, "parent_pid": 10, "parent_created": parent_created,
             "direct_child_pid": 20, "direct_child_created": direct_created,
             "child_pid": 22, "child_created": child_created}
    monkeypatch.setattr(agent_links, "_RUN_ROWS", {run_key: {
        "row": dict(codex_row), "parent": sid_a, "parentProvider": "Claude",
        "stoppedAt": None, "processProof": dict(proof),
    }})
    registry_run = {"thread_id": thread, "codex_pid": 22, "codex_created": child_created,
                    "claude_session_id": sid_a, "first_seen": 900, "last_seen": 1000,
                    "verified_at": 1000, "method": "receipt"}
    common = dict(registry_path=tmp_path / "registry.json", permission_check=_allow_collection,
                  registry_reader=lambda _: [dict(registry_run)],
                  registry_writer=lambda *a, **k: True, verify=lambda *_: True)

    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 1000)
    agent_links.apply_exact_links(providers, now=1001, **common)
    assert agent_links._RUN_ROWS[run_key]["processProof"] == proof

    states = {
        (sid_a, ""): _reduced_pid_session(sid_a, 10, parent_created, 900, 949),
        (sid_b, ""): _reduced_pid_session(sid_b, 10, parent_created, 950, 1200),
    }
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", float("-inf"))
    agent_links.apply_exact_links(
        providers, now=1011, claude_states=states,
        process_table_reader=lambda: {}, scan=lambda **_kwargs: [], **common)
    assert run_key in agent_links._CODEX_PARENT_CONTRADICTIONS
    assert agent_links._RUN_ROWS[run_key]["processProof"] is None
    assert "parentId" not in codex_row

    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 1011)
    agent_links.apply_exact_links(providers, now=1012, **common)
    assert "parentId" not in codex_row
    assert agent_links._RUN_ROWS[run_key]["parent"] is None


@pytest.mark.parametrize("status", ["idle", "stopped"])
def test_contradicted_codex_parent_stays_revoked_for_idle_and_stopped_rows(tmp_path, monkeypatch, status):
    sid_a, sid_b = "session-a", "session-b"
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    parent_created, direct_created, child_created = map(_filetime, (900, 980, 1000))
    claude = {"name": "Claude", "chats": [{"id": sid_a}, {"id": sid_b}]}
    codex_row = {"id": thread, "source": "exec", "status": status, "executionStatus": status,
                 "runPid": 22, "runCreated": str(child_created),
                 "runStartedAt": "1970-01-01T00:16:30+00:00"}
    providers = [claude, {"name": "Codex", "chats": [codex_row]}]
    run_key = (thread, 22, child_created)
    proof = {"parent_id": sid_a, "parent_pid": 10, "parent_created": parent_created,
             "direct_child_pid": 20, "direct_child_created": direct_created,
             "child_pid": 22, "child_created": child_created}
    monkeypatch.setattr(agent_links, "_CODEX_PARENT_CONTRADICTIONS", set())
    monkeypatch.setattr(agent_links, "_RUN_ROWS", {run_key: {
        "row": dict(codex_row, parentId=sid_a, parentProvider="Claude", linkSource="process"),
        "parent": sid_a, "parentProvider": "Claude", "stoppedAt": None, "processProof": dict(proof),
    }})
    registry_run = {"thread_id": thread, "codex_pid": 22, "codex_created": child_created,
                    "claude_session_id": sid_a, "first_seen": 900, "last_seen": 1000,
                    "verified_at": 1000, "method": "receipt"}
    common = dict(registry_path=tmp_path / "registry.json", permission_check=_allow_collection,
                  registry_reader=lambda _: [dict(registry_run)],
                  registry_writer=lambda *a, **k: True, verify=lambda *_: False)
    states = {
        (sid_a, ""): _reduced_pid_session(sid_a, 10, parent_created, 900, 949),
        (sid_b, ""): _reduced_pid_session(sid_b, 10, parent_created, 950, 1200),
    }

    def published_parents():
        return [chat.get("parentId") for chat in providers[1]["chats"] if str(chat.get("id")) == thread]

    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", float("-inf"))
    agent_links.apply_exact_links(
        providers, now=1011, claude_states=states,
        process_table_reader=lambda: {}, scan=lambda **_kwargs: [], **common)
    assert run_key in agent_links._CODEX_PARENT_CONTRADICTIONS
    assert sid_a not in published_parents()

    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 1011)
    agent_links.apply_exact_links(providers, now=1012, **common)
    assert sid_a not in published_parents()

    # The saved row expires but the registry still offers the old association:
    # the revocation must outlive the row.
    expired = 1012 + agent_links.STOPPED_RETENTION_SECONDS + 1
    agent_links.apply_exact_links(providers, now=expired, **common)
    assert run_key not in agent_links._RUN_ROWS
    assert run_key in agent_links._CODEX_PARENT_CONTRADICTIONS
    assert sid_a not in published_parents()
    still_offered = expired + agent_links.RETENTION_SECONDS + 1
    agent_links.apply_exact_links(providers, now=still_offered, **common)
    assert run_key in agent_links._CODEX_PARENT_CONTRADICTIONS
    assert sid_a not in published_parents()

    # A registry that briefly reads empty (a failed read looks the same) does not
    # drop the revocation; only absence for the registry's whole retention does.
    common["registry_reader"] = lambda _: []
    agent_links.apply_exact_links(providers, now=still_offered + 1, **common)
    assert run_key in agent_links._CODEX_PARENT_CONTRADICTIONS
    agent_links.apply_exact_links(providers, now=still_offered + agent_links.RETENTION_SECONDS + 2, **common)
    assert run_key not in agent_links._CODEX_PARENT_CONTRADICTIONS
    assert not agent_links._CONTRADICTION_SEEN


def test_claude_link_contradiction_is_forgotten_only_after_its_session_is_gone(tmp_path, monkeypatch):
    sid = "session-c"
    key = (sid, 30, 3000)
    monkeypatch.setattr(agent_links, "_CLAUDE_LINK_CONTRADICTIONS", {key})
    common = dict(registry_path=tmp_path / "registry.json", permission_check=_allow_collection,
                  registry_reader=lambda _: [], registry_writer=lambda *a, **k: True,
                  verify=lambda *_: False, process_table_reader=lambda: {}, scan=lambda **_kwargs: [])
    present = [{"name": "Claude", "chats": [{"id": sid}]}, {"name": "Codex", "chats": []}]
    absent = [{"name": "Claude", "chats": []}, {"name": "Codex", "chats": []}]
    agent_links.apply_exact_links(present, now=1000, **common)
    agent_links.apply_exact_links(present, now=1000 + agent_links.RETENTION_SECONDS + 1, **common)
    assert key in agent_links._CLAUDE_LINK_CONTRADICTIONS
    gone = 1000 + agent_links.RETENTION_SECONDS + 2
    agent_links.apply_exact_links(absent, now=gone, **common)
    assert key in agent_links._CLAUDE_LINK_CONTRADICTIONS
    agent_links.apply_exact_links(absent, now=gone + agent_links.RETENTION_SECONDS + 1, **common)
    assert key not in agent_links._CLAUDE_LINK_CONTRADICTIONS


def test_codex_parent_proof_detects_delayed_owner_switch():
    parent_created, direct_created, child_created = map(_filetime, (900, 980, 1000))
    proof = {"parent_id": "session-a", "parent_pid": 10, "parent_created": parent_created,
             "direct_child_pid": 20, "direct_child_created": direct_created,
             "child_pid": 22, "child_created": child_created}
    states = {
        ("session-a", ""): _reduced_pid_session("session-a", 10, parent_created, 900, 949),
        ("session-b", ""): _reduced_pid_session("session-b", 10, parent_created, 950, 1200),
    }
    assert agent_links._codex_parent_proof_contradicted(proof, states)


def test_cached_codex_process_parent_is_suppressed_when_verification_switch_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_links, "_CODEX_ENV_PER_THREAD_VERIFIED", False)
    sid, thread, providers, _ = _link_fixture()
    codex_row = providers[1]["chats"][0]
    codex_row.update(parentId="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                     parentProvider="Codex", parentLinkMethod="receipt", linkSource="process")
    identity = (thread, 22, 123)
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 1000)
    monkeypatch.setattr(agent_links, "_RUN_ROWS", {identity: {
        "row": dict(codex_row), "parent": codex_row["parentId"],
        "parentProvider": "Codex", "stoppedAt": None,
    }})
    agent_links.apply_exact_links(providers, now=1001, registry_path=tmp_path / "registry.json",
                                  permission_check=_allow_collection, registry_reader=lambda _: [],
                                  registry_writer=lambda *a, **k: True)
    assert "parentId" not in codex_row
    assert "linkSource" not in codex_row


def test_gate_off_filters_codex_process_edges_from_final_live_and_retained_chats(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_links, "_CODEX_ENV_PER_THREAD_VERIFIED", False)
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    parent = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _, _, live_providers, _ = _link_fixture()
    live_codex = live_providers[1]
    live_codex["chats"].append({"id": parent})
    live_row = live_codex["chats"][0]
    live_row.update(parentId=parent, parentProvider="Codex", parentLinkMethod="receipt",
                    linkSource="process")
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 1000)
    agent_links.apply_exact_links(
        live_providers, now=1001, registry_path=tmp_path / "live-registry.json",
        permission_check=_allow_collection, registry_reader=lambda _: [],
        registry_writer=lambda *a, **k: True)
    published_live = next(row for row in live_codex["chats"] if row.get("id") == thread)
    assert "parentId" not in published_live and "linkSource" not in published_live

    saved_row = {"id": thread, "source": "exec", "status": "running", "executionStatus": "running",
                 "runPid": 22, "runCreated": "123", "parentId": parent,
                 "parentProvider": "Codex", "parentLinkMethod": "receipt", "linkSource": "process"}
    retained_providers = [{"name": "Claude", "chats": []},
                          {"name": "Codex", "chats": [{"id": parent},
                                                         {**saved_row, "status": "idle", "executionStatus": "idle"}]}]
    run_key = (thread, 22, 123)
    monkeypatch.setattr(agent_links, "_RUN_ROWS", {run_key: {
        "row": saved_row, "parent": parent, "parentProvider": "Codex", "stoppedAt": None,
    }})
    agent_links.apply_exact_links(
        retained_providers, now=1002, registry_path=tmp_path / "retained-registry.json",
        permission_check=_allow_collection, registry_reader=lambda _: [],
        registry_writer=lambda *a, **k: True, verify=lambda *_: False)
    published_stopped = next(row for row in retained_providers[1]["chats"] if row.get("id") == thread)
    assert published_stopped.get("retainedRun") is True
    assert "parentId" not in published_stopped and "linkSource" not in published_stopped


def test_gate_off_filters_saved_process_edge_from_final_retained_provider_output(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_links, "_CODEX_ENV_PER_THREAD_VERIFIED", False)
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    parent = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    retained = {"id": thread, "source": "exec", "status": "idle", "executionStatus": "idle",
                "runPid": 22, "runCreated": "123", "retainedRun": True,
                "parentId": parent, "parentProvider": "Codex", "parentLinkMethod": "receipt",
                "linkSource": "process"}
    providers = [{"name": "Claude", "chats": []}, {"name": "Codex", "chats": [{"id": parent}]}]
    run_key = (thread, 22, 123)
    monkeypatch.setattr(agent_links, "_RUN_ROWS", {run_key: {
        "row": dict(retained), "parent": parent, "parentProvider": "Codex", "stoppedAt": 1000,
    }})
    agent_links.apply_exact_links(
        providers, now=1001, registry_path=tmp_path / "retained-registry.json",
        permission_check=_allow_collection, registry_reader=lambda _: [],
        registry_writer=lambda *a, **k: True, verify=lambda *_: False)
    final_row = next(row for row in providers[1]["chats"] if row.get("id") == thread)
    assert final_row.get("retainedRun") is True
    assert "parentId" not in final_row and "linkSource" not in final_row


def test_codex_receipt_gated_exact_launcher_links_only_when_switch_true(tmp_path, monkeypatch):
    parent = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    child_thread = "01234567-89ab-cdef-0123-456789abcdef"
    parent_created, child_created = 100, 123
    monkeypatch.setattr(agent_links, "_CODEX_ENV_PER_THREAD_VERIFIED", True)
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", float("-inf"))
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: True)
    codex = {"name": "Codex", "chats": [
        {"id": parent, "runPid": 10, "runCreated": str(parent_created)},
        {"id": child_thread, "source": "exec", "status": "running", "executionStatus": "running"},
    ]}
    claude = {"name": "Claude", "chats": []}
    receipt = {"thread_id": child_thread, "codex_pid": 30, "codex_created": child_created,
               "launcher_codex_thread_id": parent, "recorded_at": 100, "method": "receipt"}
    table = {30: (10, "codex.exe"), 10: (1, "codex.exe"), 1: (0, "explorer.exe")}
    agent_links.apply_exact_links(
        [claude, codex], root=tmp_path, now=200, registry_path=tmp_path / "registry.json",
        permission_check=_allow_collection, registry_reader=lambda _: [],
        registry_writer=lambda *a, **k: True, receipts_reader=lambda _: [receipt],
        process_table_reader=lambda: table, current_sid_reader=lambda: b"sid",
        process_sid=lambda _pid: b"sid", equal_sid=lambda left, right: left == right,
        process_created_reader=lambda pid: {30: child_created, 10: parent_created, 1: 1}[pid],
        command_line=lambda _pid: f"codex.exe exec resume {child_thread}",
        scan=lambda **_kwargs: [], hooks=lambda: {})
    child, = [row for row in codex["chats"] if row["id"] == child_thread]
    assert child["parentId"] == parent and child["parentProvider"] == "Codex"
    assert child["linkSource"] == "process"


def test_receipt_expected_creation_rejects_reused_or_exited_process(tmp_path, receipt_inbox_creation):
    assert agent_links.initialize_receipt_inbox(tmp_path)
    kwargs = dict(environ={"CLAUDE_CODE_SESSION_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
                  root=tmp_path, permission_check=lambda _: True,
                  expected_created=123)
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    assert not agent_links.write_receipt(thread, 22, pid_check=lambda *_: 124,
                                         created=lambda _: 124, **kwargs)
    assert not agent_links.write_receipt(thread, 22, pid_check=lambda *_: 123,
                                         created=lambda _: None, **kwargs)
    assert (tmp_path / agent_links.INBOX_NAME / agent_links.RECEIPT_FILE).read_bytes() == b""
    assert agent_links.write_receipt(thread, 22, pid_check=lambda *_: 123,
                                     created=lambda _: 123, **kwargs)


def _write_test_receipt(root=None):
    return agent_links.write_receipt(
        "01234567-89ab-cdef-0123-456789abcdef", 22,
        environ={"CLAUDE_CODE_SESSION_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
        root=root, permission_check=lambda _: True, pid_check=lambda *_: 123)


def test_receipt_frozen_uses_known_folder_not_environment(tmp_path, monkeypatch, receipt_inbox_creation):
    from token_tracker import product
    known = tmp_path / "known"
    known.mkdir()
    (known / "Branchglance").mkdir()
    monkeypatch.setattr(product, "is_frozen", lambda: True)
    monkeypatch.setattr(agent_links, "_known_local_app_data", lambda: known)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "elsewhere"))
    assert agent_links.initialize_receipt_inbox()
    assert _write_test_receipt()
    assert (known / "Branchglance" / agent_links.INBOX_NAME / agent_links.RECEIPT_FILE).is_file()
    assert not (tmp_path / "elsewhere").exists()


def test_receipt_refuses_redirected_chain(tmp_path, monkeypatch, receipt_inbox_creation):
    from types import SimpleNamespace
    assert agent_links.initialize_receipt_inbox(tmp_path)
    original = agent_links.os.lstat
    inbox = tmp_path / agent_links.INBOX_NAME
    def reparse(path):
        if Path(path) == inbox:
            info = original(path)
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return original(path)
    monkeypatch.setattr(agent_links.os, "lstat", reparse)
    assert not _write_test_receipt(tmp_path)


def test_receipt_refuses_identity_mismatch(tmp_path, monkeypatch, receipt_inbox_creation):
    assert agent_links.initialize_receipt_inbox(tmp_path)
    identities = iter([(1, 2, 3), (1, 2, 4)])
    monkeypatch.setattr(agent_links, "_file_identity", lambda _: next(identities))
    assert not _write_test_receipt(tmp_path)
    assert (tmp_path / agent_links.INBOX_NAME / agent_links.RECEIPT_FILE).read_bytes() == b""


def test_receipt_refuses_package_shadow(tmp_path, monkeypatch, caplog, receipt_inbox_creation):
    from token_tracker import product
    (tmp_path / "Branchglance").mkdir()
    monkeypatch.setattr(product, "is_frozen", lambda: True)
    monkeypatch.setattr(agent_links, "_known_local_app_data", lambda: tmp_path)
    assert agent_links.initialize_receipt_inbox()
    shadow = tmp_path / "Packages" / "Claude" / "LocalCache" / "Local" / "Branchglance" / agent_links.INBOX_NAME
    shadow.mkdir(parents=True)
    assert not _write_test_receipt()
    assert "package-local shadow" in caplog.text


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
        "runPid": 22, "runCreated": "123",
    }]}
    run = {"thread_id": thread, "codex_pid": 22, "codex_created": 123,
           "claude_session_id": sid, "first_seen": 1, "last_seen": 100,
           "verified_at": 100, "method": "receipt"}
    return sid, thread, [claude, codex], run


def _allow_collection(_policies):
    return True


def _receipt_parent_proof(sid, parent_pid=10, parent_created=100):
    return {(sid, ""): {"pid_associations": [{
        "pid": parent_pid, "created": parent_created, "sid": sid,
        "first": -11_644_473_700, "last": -11_644_473_500,
    }]}}


def test_unresolved_new_run_stays_top_level_when_old_run_is_retained(tmp_path, monkeypatch):
    sid, thread, providers, run = _link_fixture()
    old = dict(providers[1]["chats"][0], parentId=sid, parentProvider="Claude")
    monkeypatch.setattr(agent_links, "_RUN_ROWS", {(thread, 22, 123): {
        "row": old, "parent": sid, "parentProvider": "Claude", "parentRow": {"id": sid}, "stoppedAt": None}})
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 105)
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: False)
    # Older than the retained run, so only the unresolved rule keeps it authoritative.
    providers[1]["chats"][0].update(runPid=21, runCreated="122")
    agent_links.apply_exact_links(providers, now=106, registry_path=tmp_path / "registry.json",
                                 permission_check=_allow_collection, registry_reader=lambda _: [run],
                                 verify=lambda *_: False)
    row, = providers[1]["chats"]
    assert row["runBindingUnresolved"] is True
    assert not row.get("parentId")
    assert row["resumeCount"] == 2
    assert "firstSeenAt" not in row


def test_single_observed_run_uses_its_registry_first_seen(tmp_path, monkeypatch):
    _, _, providers, run = _link_fixture()
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 105)
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: False)
    agent_links.apply_exact_links(providers, now=106, registry_path=tmp_path / "registry.json",
                                 permission_check=_allow_collection, registry_reader=lambda _: [run],
                                 verify=lambda *_: True)
    row, = providers[1]["chats"]
    assert "resumeCount" not in row
    assert row["firstSeenAt"] == "1970-01-01T00:00:01+00:00"


def test_newest_valid_live_receipt_binds_and_collapses_to_one_row(tmp_path, monkeypatch):
    sid, thread, providers, _ = _link_fixture()
    newer_sid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    providers[0]["chats"].append({"id": newer_sid})
    providers[1]["chats"][0].pop("runPid")
    providers[1]["chats"][0].pop("runCreated")
    rows = [{"thread_id": thread, "codex_pid": pid, "codex_created": created,
             "claude_session_id": parent, "recorded_at": 100 + pid, "method": "receipt"}
            for pid, created, parent in ((22, 123, sid), (23, 124, newer_sid))]
    inbox = agent_links.receipt_path(tmp_path)
    inbox.mkdir()
    (inbox / agent_links.RECEIPT_FILE).write_text("\n".join(json.dumps(row) for row in rows))
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", float("-inf"))
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: True)
    monkeypatch.setattr(agent_links, "_UNLINKED_EXECUTIONS", {})
    agent_links.apply_exact_links(
        providers, root=tmp_path, now=200, registry_path=tmp_path / "registry.json",
        permission_check=_allow_collection, registry_reader=lambda _: [], registry_writer=lambda *a, **k: True,
        receipts_reader=lambda root: agent_links.read_receipts(root, permission_check=_allow_collection, owner_check=lambda: True),
        process_table_reader=lambda: {22: (10, "codex.exe"), 23: (11, "codex.exe"),
                                      10: (1, "claude.exe"), 11: (1, "claude.exe"), 1: (0, "explorer.exe")},
        current_sid_reader=lambda: b"sid", process_sid=lambda _: b"sid", equal_sid=lambda a,b: a == b,
        process_created_reader=lambda pid: {22: 123, 23: 124, 10: 100, 11: 101, 1: 1}[pid],
        command_line=lambda _: f"codex.exe exec resume {thread}",
        scan=lambda **kw: [], verify=lambda *_: True,
        hooks=lambda: {**_receipt_parent_proof(sid, 10, 100), **_receipt_parent_proof(newer_sid, 11, 101)},
    )
    current, = providers[1]["chats"]
    assert (current["runPid"], current["parentId"], current["runVerified"]) == (23, newer_sid, True)
    assert current["resumeCount"] == 2
    assert current["firstSeenAt"] == "1970-01-01T00:02:03+00:00"


def test_ambiguous_scan_without_receipt_is_unknown(tmp_path, monkeypatch):
    _, thread, providers, _ = _link_fixture()
    row = providers[1]["chats"][0]
    row.pop("runPid"); row.pop("runCreated")
    row["runStartedAt"] = "1970-01-01T00:01:30Z"
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", float("-inf"))
    monkeypatch.setattr(agent_links, "_UNLINKED_EXECUTIONS", {})
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: False)
    scans = [{"thread_id": thread, "codex_pid": pid, "codex_created": _filetime(100 + pid),
              "claude_session_id": None, "method": "unlinked"} for pid in (22, 23)]
    agent_links.apply_exact_links(providers, now=200, registry_path=tmp_path / "registry.json",
                                 permission_check=_allow_collection, registry_reader=lambda _: [],
                                 process_table_reader=lambda: {}, hooks=lambda: {}, scan=lambda **kw: scans)
    assert row["runVerified"] is None and row["executionStatus"] == "unavailable"
    assert not row.get("parentId") and not row.get("stoppedAt")


def test_scan_only_binds_when_process_follows_latest_rollout(tmp_path, monkeypatch):
    _, thread, providers, _ = _link_fixture()
    row = providers[1]["chats"][0]
    row.pop("runPid"); row.pop("runCreated")
    row["runStartedAt"] = "1970-01-01T00:01:30Z"
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", float("-inf"))
    monkeypatch.setattr(agent_links, "_UNLINKED_EXECUTIONS", {})
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: False)
    scan = {"thread_id": thread, "codex_pid": 23, "codex_created": _filetime(100),
            "claude_session_id": None, "method": "unlinked"}
    agent_links.apply_exact_links(providers, now=200, registry_path=tmp_path / "registry.json",
                                 permission_check=_allow_collection, registry_reader=lambda _: [],
                                 process_table_reader=lambda: {}, hooks=lambda: {}, scan=lambda **kw: [scan])
    assert row["runVerified"] is True and row["runPid"] == 23
    assert not row.get("parentId")


def test_dead_old_receipt_cannot_override_new_live_receipt(tmp_path, monkeypatch):
    old_sid, thread, providers, old = _link_fixture()
    new_sid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    providers[0]["chats"].append({"id": new_sid})
    row = providers[1]["chats"][0]
    row.pop("runPid"); row.pop("runCreated")
    new = {**old, "codex_pid": 23, "codex_created": 124, "claude_session_id": new_sid}
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 105)
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: False)
    monkeypatch.setattr(agent_links, "_UNLINKED_EXECUTIONS", {})
    agent_links.apply_exact_links(providers, now=106, registry_path=tmp_path / "registry.json",
                                 permission_check=_allow_collection, registry_reader=lambda _: [old, new],
                                 verify=lambda pid, created: pid == 23)
    assert row["parentId"] == new_sid and row["parentId"] != old_sid
    assert row["runPid"] == 23 and row["runVerified"] is True


def test_native_child_links_unique_retained_ancestor(monkeypatch):
    monkeypatch.setattr(agent_links, "_GRAPH_EDGES", {})
    parent = {"provider": "Codex", "id": "parent", "retainedRun": True, "runPid": 22, "runCreated": "123"}
    child = {"provider": "Codex", "id": "child", "parentId": "parent", "parentProvider": "Codex"}
    graph = agent_links.snapshot_graph([parent, child])
    assert graph["edges"]["Codex:child"] == "Codex:parent"


def test_terminal_thread_does_not_inherit_an_old_run_without_process_identity(tmp_path, monkeypatch):
    sid, _, providers, run = _link_fixture()
    row = providers[1]["chats"][0]
    row.update(executionStatus="idle", parentId=sid, parentProvider="Claude")
    row.pop("runPid");row.pop("runCreated")
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 105)
    agent_links.apply_exact_links(providers, now=106, registry_path=tmp_path / "registry.json",
                                 permission_check=_allow_collection, registry_reader=lambda _: [run])
    assert not row.get("parentId")
    assert row["runVerified"] is None


def test_two_live_runs_for_one_thread_collapse_to_newest_execution(tmp_path, monkeypatch):
    sid, _, providers, run = _link_fixture()
    providers[1]["chats"][0].update(tokens=5, contextTokens=7, toolCalls=3)
    other = {**run, "codex_pid": 23, "codex_created": 124}
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 105)
    agent_links.apply_exact_links(providers, now=106, registry_path=tmp_path / "registry.json",
                                 permission_check=_allow_collection, registry_reader=lambda _: [run, other],
                                 verify=lambda *_: True, registry_writer=lambda *a, **k: True)
    row = providers[1]["chats"][0]
    assert row["parentId"] == sid
    assert row["runPid"] == 23
    assert row["resumeCount"] == 2
    assert row["runVerified"] is True
    # The current row is the base: the newer execution's metric-less copy never replaces it.
    assert (row["tokens"], row["contextTokens"], row["toolCalls"]) == (5, 7, 3)
    assert not row.get("retainedRun") and not row.get("metricsUnavailable")


def test_one_live_registry_run_without_row_process_evidence_binds_receipt(tmp_path, monkeypatch):
    sid, _, providers, run = _link_fixture()
    row = providers[1]["chats"][0]
    row.pop("runPid");row.pop("runCreated")
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 105)
    agent_links.apply_exact_links(providers, now=106, registry_path=tmp_path / "registry.json",
                                 permission_check=_allow_collection, registry_reader=lambda _: [run],
                                 verify=lambda *_: True, registry_writer=lambda *a, **k: True)
    assert row["parentId"] == sid
    assert row["parentLinkMethod"] == "receipt"
    assert row["runVerified"] is True


def test_row_process_evidence_and_newer_receipt_collapse_to_newest_execution(tmp_path, monkeypatch):
    sid, thread, providers, _ = _link_fixture()
    newer_sid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    providers[0]["chats"].append({"id": newer_sid})
    rows = [{"thread_id": thread, "codex_pid": pid, "codex_created": created,
             "claude_session_id": parent, "recorded_at": 100 + pid, "method": "receipt"}
            for pid, created, parent in ((22, 123, sid), (23, 124, newer_sid))]
    inbox = agent_links.receipt_path(tmp_path)
    inbox.mkdir()
    (inbox / agent_links.RECEIPT_FILE).write_text("\n".join(json.dumps(row) for row in rows))
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", float("-inf"))
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: True)
    monkeypatch.setattr(agent_links, "_UNLINKED_EXECUTIONS", {})
    agent_links.apply_exact_links(
        providers, root=tmp_path, now=200, registry_path=tmp_path / "registry.json",
        permission_check=_allow_collection, registry_reader=lambda _: [], registry_writer=lambda *a, **k: True,
        receipts_reader=lambda root: agent_links.read_receipts(root, permission_check=_allow_collection, owner_check=lambda: True),
        process_table_reader=lambda: {22: (10, "codex.exe"), 23: (11, "codex.exe"),
                                      10: (1, "claude.exe"), 11: (1, "claude.exe"), 1: (0, "explorer.exe")},
        current_sid_reader=lambda: b"sid", process_sid=lambda _: b"sid", equal_sid=lambda a, b: a == b,
        process_created_reader=lambda pid: {22: 123, 23: 124, 10: 100, 11: 101, 1: 1}[pid],
        command_line=lambda _: f"codex.exe exec resume {thread}",
        scan=lambda **kw: [], verify=lambda *_: True,
        hooks=lambda: {**_receipt_parent_proof(sid, 10, 100), **_receipt_parent_proof(newer_sid, 11, 101)},
    )
    current, = providers[1]["chats"]
    assert (current["runPid"], current["parentId"], current["runVerified"]) == (23, newer_sid, True)
    assert current["resumeCount"] == 2


def test_running_exec_without_link_history_keeps_its_status(tmp_path, monkeypatch):
    _, _, providers, _ = _link_fixture()
    row = providers[1]["chats"][0]
    row.update(status="running")
    row.pop("runPid"); row.pop("runCreated")
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 105)
    agent_links.apply_exact_links(providers, now=106, registry_path=tmp_path / "registry.json",
                                 permission_check=_allow_collection, registry_reader=lambda _: [],
                                 verify=lambda *_: True, registry_writer=lambda *a, **k: True)
    assert (row["status"], row["executionStatus"]) == ("running", "running")
    assert "parentId" not in row
    assert not row.get("runBindingUnresolved")


def test_finished_exec_row_keeps_its_status_when_binding_is_not_possible(tmp_path, monkeypatch):
    _, _, providers, run = _link_fixture()
    row = providers[1]["chats"][0]
    row.update(status="completed", executionStatus="idle")
    row.pop("runPid"); row.pop("runCreated")
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 105)
    agent_links.apply_exact_links(providers, now=106, registry_path=tmp_path / "registry.json",
                                 permission_check=_allow_collection, registry_reader=lambda _: [run],
                                 verify=lambda *_: True, registry_writer=lambda *a, **k: True)
    assert (row["status"], row["executionStatus"]) == ("completed", "idle")
    assert "parentId" not in row
    assert not row.get("runBindingUnresolved")


def test_live_receipt_without_claude_parent_in_list_keeps_running_status(tmp_path, monkeypatch):
    sid, thread, providers, _ = _link_fixture()
    providers[0]["chats"] = []
    row = providers[1]["chats"][0]
    row.update(status="running")
    row.pop("runPid"); row.pop("runCreated")
    receipt = {"thread_id": thread, "codex_pid": 22, "codex_created": 123,
               "claude_session_id": sid, "recorded_at": 100, "method": "receipt"}
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", float("-inf"))
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: True)
    monkeypatch.setattr(agent_links, "_UNLINKED_EXECUTIONS", {})
    agent_links.apply_exact_links(
        providers, root=tmp_path, now=200, registry_path=tmp_path / "registry.json",
        permission_check=_allow_collection, registry_reader=lambda _: [], registry_writer=lambda *a, **k: True,
        receipts_reader=lambda _root: [receipt],
        process_table_reader=lambda: {22: (10, "codex.exe"), 10: (1, "claude.exe"), 1: (0, "explorer.exe")},
        current_sid_reader=lambda: b"sid", process_sid=lambda _: b"sid", equal_sid=lambda a, b: a == b,
        process_created_reader=lambda pid: {22: 123, 10: 100, 1: 1}[pid],
        command_line=lambda _: f"codex.exe exec resume {thread}",
        scan=lambda **kw: [], verify=lambda *_: True, hooks=lambda: _receipt_parent_proof(sid),
    )
    assert (row["status"], row["executionStatus"]) == ("running", "running")
    assert "parentId" not in row
    assert (row["runPid"], row["runVerified"]) == (22, True)


def test_parentless_binding_does_not_block_later_receipt_parent(tmp_path, monkeypatch):
    sid, thread, providers, run = _link_fixture()
    run = {**run, "claude_session_id": None}
    row = providers[1]["chats"][0]
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 105)
    agent_links.apply_exact_links(providers, now=106, registry_path=tmp_path / "registry.json",
                                 permission_check=_allow_collection, registry_reader=lambda _: [run],
                                 verify=lambda *_: True, registry_writer=lambda *a, **k: True)
    assert "parentId" not in row and row["runVerified"] is True
    receipt = {"thread_id": thread, "codex_pid": 22, "codex_created": 123,
               "claude_session_id": sid, "recorded_at": 100, "method": "receipt"}
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", float("-inf"))
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: True)
    monkeypatch.setattr(agent_links, "_UNLINKED_EXECUTIONS", {})
    agent_links.apply_exact_links(
        providers, root=tmp_path, now=107, registry_path=tmp_path / "registry.json",
        permission_check=_allow_collection, registry_reader=lambda _: [], registry_writer=lambda *a, **k: True,
        receipts_reader=lambda _root: [receipt],
        process_table_reader=lambda: {22: (10, "codex.exe"), 10: (1, "claude.exe"), 1: (0, "explorer.exe")},
        current_sid_reader=lambda: b"sid", process_sid=lambda _: b"sid", equal_sid=lambda a, b: a == b,
        process_created_reader=lambda pid: {22: 123, 10: 100, 1: 1}[pid],
        command_line=lambda _: f"codex.exe exec resume {thread}",
        scan=lambda **kw: [], verify=lambda *_: True, hooks=lambda: _receipt_parent_proof(sid),
    )
    assert (row["parentId"], row["parentProvider"], row["runVerified"]) == (sid, "Claude", True)


def test_dead_old_run_does_not_parent_an_unresolved_new_run(tmp_path, monkeypatch):
    _, thread, providers, old = _link_fixture()
    providers[1]["chats"][0].pop("runPid");providers[1]["chats"][0].pop("runCreated")
    new = {"thread_id": thread, "codex_pid": 23, "codex_created": 124,
           "claude_session_id": None, "method": "unlinked"}
    monkeypatch.setattr(agent_links, "_UNLINKED_EXECUTIONS", {})
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", float("-inf"))
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: False)
    agent_links.apply_exact_links(providers, now=106, registry_path=tmp_path / "registry.json",
                                 permission_check=_allow_collection, registry_reader=lambda _: [old],
                                 verify=lambda pid, created: False if pid == 22 else None,
                                 process_table_reader=lambda: {}, hooks=lambda: {}, scan=lambda **kw: [new])
    row = providers[1]["chats"][0]
    assert not row.get("parentId")
    assert row["runVerified"] is None
    assert row.get("stateDetail") != "stopped"


def test_verified_run_retains_frozen_telemetry_until_expiry(tmp_path, monkeypatch):
    sid, _, providers, run = _link_fixture()
    monkeypatch.setattr(agent_links, "_RUN_ROWS", {})
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 105)
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: False)
    providers[1]["chats"][0].update(tokens=42, contextTokens=100)
    common = dict(registry_path=tmp_path / "registry.json", permission_check=_allow_collection,
                  registry_reader=lambda _: [dict(run)])
    agent_links.apply_exact_links(providers, now=106, verify=lambda *_: True, **common)
    assert providers[1]["chats"][0]["parentId"] == sid
    _, _, after, _ = _link_fixture()
    after[1]["chats"][0].update(tokens=999, contextTokens=999)
    agent_links.apply_exact_links(after, now=107, verify=lambda *_: False, **common)
    stopped = next(row for row in after[1]["chats"] if row.get("retainedRun"))
    assert stopped["stateDetail"] == "stopped" and stopped["runVerified"] is False
    assert stopped["tokens"] == 42 and stopped["contextTokens"] == 100
    stopped_at = stopped["stoppedAt"]
    _, _, later, _ = _link_fixture()
    agent_links.apply_exact_links(later, now=107 + 899, verify=lambda *_: False, **common)
    assert next(row for row in later[1]["chats"] if row.get("retainedRun"))["stoppedAt"] == stopped_at
    _, _, expired, _ = _link_fixture()
    agent_links.apply_exact_links(expired, now=107 + 900, verify=lambda *_: False, **common)
    assert not any(row.get("retainedRun") for row in expired[1]["chats"])


def test_retained_run_keeps_its_last_known_parent(tmp_path, monkeypatch):
    sid, _, providers, run = _link_fixture()
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 105)
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: False)
    common = dict(registry_path=tmp_path / "registry.json", permission_check=_allow_collection,
                  registry_reader=lambda _: [dict(run)])
    agent_links.apply_exact_links(providers, now=106, verify=lambda *_: True, **common)
    _, _, after, _ = _link_fixture()
    after[0]["chats"] = []
    agent_links.apply_exact_links(after, now=107, verify=lambda *_: False, **common)
    assert any(row.get("id") == sid for row in after[0]["chats"])
    assert any(row.get("retainedRun") and row.get("parentId") == sid for row in after[1]["chats"])


def test_link_poll_rechecks_recently_verified_execution(monkeypatch):
    _sid, _thread, providers, run = _link_fixture()
    called = []
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 104)
    agent_links.apply_exact_links(
        providers, now=105, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [dict(run)],
        verify=lambda *_args: called.append("verify") or True,
        scan=lambda **_kwargs: called.append("scan"),
        hooks=lambda: called.append("hooks"),
        receipts_reader=lambda _root: called.append("receipts"),
    )
    assert called == ["verify"]


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
    assert calls == ["verify"]
    agent_links.apply_exact_links(
        _link_fixture()[2], now=110, registry_path=Path("unused"), permission_check=_allow_collection,
        registry_reader=lambda _path: [dict(run)],
        verify=lambda *_args: calls.append("verify") or True,
        scan=lambda **_kwargs: calls.append("scan") or [],
        process_table_reader=lambda: {}, hooks=lambda: {},
        receipts_reader=lambda _root: [],
    )
    assert calls == ["verify", "scan"]


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


def test_new_exec_triggers_scan_and_links_only_exact_session(monkeypatch):
    sid, thread, providers, run = _link_fixture()
    run["verified_at"] = 0
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", float("-inf"))
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: False)
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
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 104)
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
        registry_reader=lambda _path: [run], verify=lambda *_args: True,
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
        registry_reader=lambda _path: [dict(run)], verify=lambda *_args: True,
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


@pytest.mark.skipif(os.name != "nt", reason="Windows known folders")
def test_known_folder_lookup_disables_package_redirection(monkeypatch):
    import ctypes
    seen = []
    buffer = ctypes.create_unicode_buffer(r"C:\Users\someone\AppData\Local")

    class Fn:
        def __init__(self, impl):
            self.impl, self.argtypes, self.restype = impl, None, None

        def __call__(self, *args):
            return self.impl(*args)

    def known_folder(_folder, flags, _token, out):
        seen.append(flags)
        out._obj.value = ctypes.addressof(buffer)
        return 0

    class Shell:
        SHGetKnownFolderPath = Fn(known_folder)

    class Ole:
        CoTaskMemFree = Fn(lambda _ptr: None)

    real = ctypes.WinDLL
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: Shell() if name == "shell32"
                        else Ole() if name == "ole32" else real(name, **kw))
    assert str(agent_links._known_local_app_data()) == r"C:\Users\someone\AppData\Local"
    assert seen == [agent_links.KF_FLAG_NO_PACKAGE_REDIRECTION] and seen[0] == 0x10000


def test_current_run_is_not_retained_again_when_its_claude_parent_leaves_the_list(monkeypatch):
    # R6: the row's own run must appear exactly once after its Claude session ages out.
    from pathlib import Path
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: True)
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    record = {"thread_id": thread, "codex_pid": 22, "codex_created": 123, "claude_session_id": sid,
              "first_seen": 1, "last_seen": 199, "verified_at": 199, "method": "receipt"}

    def poll(claude, now):
        monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", now - 1)
        providers = [{"name": "Claude", "chats": claude},
                     {"name": "Codex", "chats": [{"id": thread, "source": "exec", "status": "running",
                                                  "executionStatus": "running", "runPid": 22, "runCreated": "123"}]}]
        agent_links.apply_exact_links(
            providers, now=now, registry_path=Path("unused"), permission_check=lambda _: True,
            registry_reader=lambda _: [dict(record)], registry_writer=lambda *a, **k: True, hooks=lambda: {},
            process_table_reader=lambda: {}, receipts_reader=lambda _: [], verify=lambda *_: True,
            scan=lambda **k: pytest.fail("scan must not run"))
        return providers[1]["chats"]

    first = poll([{"id": sid}], 200)
    assert [row.get("parentId") for row in first] == [sid]
    second = poll([], 201)
    assert len(second) == 1
    assert second[0]["runPid"] == 22 and second[0].get("parentId") is None
    assert second[0]["status"] == "running"


def _stopped_run(thread, sid, row, stopped_at=100):
    old = dict(row, title="old", tokens=1, parentId=sid, parentProvider="Claude", runPid=22, runCreated="123")
    return {(thread, 22, 123): {"row": old, "parent": sid, "parentProvider": "Claude",
                                "parentRow": {"id": sid}, "stoppedAt": stopped_at}}


def _collapse(providers, tmp_path, monkeypatch, runs, now=106):
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", now - 1)
    monkeypatch.setattr(agent_links, "_instance_mutex_held", lambda: False)
    agent_links.apply_exact_links(providers, now=now, registry_path=tmp_path / "registry.json",
                                 permission_check=_allow_collection, registry_reader=lambda _: list(runs),
                                 verify=lambda *_: False)
    return providers[1]["chats"]


def test_thread_running_again_is_not_replaced_by_its_stopped_run(tmp_path, monkeypatch):
    sid, thread, providers, run = _link_fixture()
    current = providers[1]["chats"][0]
    monkeypatch.setattr(agent_links, "_RUN_ROWS", _stopped_run(thread, sid, current))
    current.clear()
    current.update(id=thread, source="vscode", title="new", status="running", executionStatus="running")
    row, = _collapse(providers, tmp_path, monkeypatch, [run])
    assert (row["title"], row["status"]) == ("new", "running")
    assert not row.get("retainedRun") and not row.get("stoppedAt")
    assert not row.get("parentId"), "a stopped execution's parent cannot prove the new activity"


def test_finished_run_keeps_current_data_and_takes_the_stopped_execution(tmp_path, monkeypatch):
    sid, thread, providers, run = _link_fixture()
    current = providers[1]["chats"][0]
    monkeypatch.setattr(agent_links, "_RUN_ROWS", _stopped_run(thread, sid, current))
    current.clear()
    current.update(id=thread, source="exec", title="new", tokens=9, status="completed", executionStatus="idle")
    row, = _collapse(providers, tmp_path, monkeypatch, [run])
    assert (row["title"], row["tokens"], row["status"]) == ("new", 9, "completed")
    assert row["retainedRun"] is True and row["stoppedAt"]
    assert (row["parentId"], row["runPid"]) == (sid, 22)


def test_resume_count_survives_run_retention_expiry(tmp_path, monkeypatch):
    sid, thread, providers, _ = _link_fixture()
    current = providers[1]["chats"][0]
    runs = _stopped_run(thread, sid, current, stopped_at=0)
    newer = dict(runs[(thread, 22, 123)], row=dict(runs[(thread, 22, 123)]["row"], runPid=23, runCreated="124"))
    runs[(thread, 23, 124)] = newer
    monkeypatch.setattr(agent_links, "_RUN_ROWS", runs)
    current.pop("runPid"); current.pop("runCreated")
    row, = _collapse(providers, tmp_path, monkeypatch, [])
    assert row["resumeCount"] == 2
    row, = _collapse(providers, tmp_path, monkeypatch, [], now=106 + agent_links.STOPPED_RETENTION_SECONDS)
    assert not agent_links._RUN_ROWS, "retention expired"
    assert row["resumeCount"] == 2


@pytest.mark.parametrize("state", [
    {"status": "needs input"},
    {"status": "unavailable", "executionStatus": "unavailable", "previousStatus": "running"},
    {"status": "idle", "executionStatus": "idle", "activityAt": "1970-01-01T00:01:44+00:00"},
    {"status": "idle", "telemetryUnavailable": True, "previousStatus": "running"},
])
def test_thread_newer_than_its_stopped_run_is_not_marked_stopped(tmp_path, monkeypatch, state):
    sid, thread, providers, run = _link_fixture()
    current = providers[1]["chats"][0]
    monkeypatch.setattr(agent_links, "_RUN_ROWS", _stopped_run(thread, sid, current))
    current.clear()
    current.update(id=thread, source="vscode", title="new", **state)
    row, = _collapse(providers, tmp_path, monkeypatch, [run])
    assert row["title"] == "new" and row.get("stateDetail") != "stopped"
    assert not row.get("retainedRun") and not row.get("parentId")


def test_finished_thread_with_leftover_previous_status_is_still_stopped(tmp_path, monkeypatch):
    sid, thread, providers, run = _link_fixture()
    current = providers[1]["chats"][0]
    monkeypatch.setattr(agent_links, "_RUN_ROWS", _stopped_run(thread, sid, current))
    current.clear()
    current.update(id=thread, source="vscode", title="new", status="idle", executionStatus="idle",
                   previousStatus="running", activityAt="1970-01-01T00:01:39+00:00")
    row, = _collapse(providers, tmp_path, monkeypatch, [run])
    assert row["title"] == "new"
    assert row["retainedRun"] is True and row["stoppedAt"] and row["parentId"] == sid
