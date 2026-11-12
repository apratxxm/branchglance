from __future__ import annotations

import ctypes
import pytest

from token_tracker import macos_process
from token_tracker import agent_links
from token_tracker import claude_events


@pytest.fixture(autouse=True)
def isolate_nesting_state(monkeypatch):
    monkeypatch.setattr(agent_links, "_CLAUDE_PROCESS_LINKS", {})
    monkeypatch.setattr(agent_links, "_CLAUDE_LINK_CONTRADICTIONS", set())
    monkeypatch.setattr(agent_links, "_RECEIPT_CONFLICTS", set())
    monkeypatch.setattr(agent_links, "_CODEX_LINK_CONTRADICTIONS", set())


def test_macos_process_argv_api_keeps_argument_boundaries(monkeypatch):
    argv = ["/usr/bin/codex", "exec", "resume 01234567-89ab-cdef-0123-456789abcdef",
            "prompt with spaces", 'quote " inside', ""]
    raw = (len(argv).to_bytes(4, "little", signed=True)
           + argv[0].encode() + b"\0"
           + b"\0".join(item.encode() for item in argv) + b"\0")

    class FakeLib:
        def sysctl(self, mib, _count, old, oldlen, _new, _newlen):
            if mib[1] == macos_process._KERN_ARGMAX:
                ctypes.cast(old, ctypes.POINTER(ctypes.c_int))[0] = 4096
                ctypes.cast(oldlen, ctypes.POINTER(ctypes.c_size_t))[0] = ctypes.sizeof(ctypes.c_int)
                return 0
            ctypes.memmove(old, raw, len(raw))
            ctypes.cast(oldlen, ctypes.POINTER(ctypes.c_size_t))[0] = len(raw)
            return 0

    monkeypatch.setattr(macos_process, "_lib", lambda: FakeLib())
    found = macos_process.command_line_argv(42)
    assert found == argv
    assert macos_process.command_line(42) == " ".join(argv)


def _filetime(epoch):
    return int((epoch + 11_644_473_600) * 10_000_000)


def _states(child, parent, child_created, parent_created, *, first=900, last=1200, launch=None):
    child_state = {"pid_associations": [{"pid": child, "created": child_created,
                                         "sid": "child", "first": 1000, "last": 1000}]}
    if launch:
        child_state["launcher_associations"] = [{"pid": child, "created": child_created,
                                                   "launcher_codex_thread_id": launch}]
    return {
        ("child", ""): child_state,
        ("parent", ""): {"pid_associations": [{"pid": parent, "created": parent_created,
                                                  "sid": "parent", "first": first, "last": last}]},
    }


def _common(table, states, created, **kwargs):
    return agent_links.scan_claude_process_links(
        claude_states=states, table=table, current_sid=b"sid",
        equal_sid=lambda left, right: left == right, process_sid=lambda _pid: b"sid",
        created=created, command_argv=lambda _pid: None, **kwargs)


@pytest.mark.parametrize("descendant", [
    {"id": "agent", "parentId": "01234567-89ab-cdef-0123-456789abcdef", "status": "running"},
    {"id": "agent", "parentId": "01234567-89ab-cdef-0123-456789abcdef", "status": "unknown"},
    {"id": "agent", "parentId": "01234567-89ab-cdef-0123-456789abcdef", "rolloutReadable": False},
    {"id": "agent", "parentId": "01234567-89ab-cdef-0123-456789abcdef", "rolloutAvailable": False},
])
def test_codex_launcher_gate_refuses_open_unknown_or_unreadable_descendant(monkeypatch, descendant):
    monkeypatch.setattr(agent_links, "_CODEX_ENV_PER_THREAD_VERIFIED", True)
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    p, d, c = map(_filetime, (900, 950, 1000))
    table = {30: (20, "claude.exe"), 20: (10, "zsh"), 10: (1, "codex.exe"), 1: (0, "launchd")}
    states = _states(30, 10, c, p, launch=thread)
    threads = [{"id": thread, "runPid": 10, "runCreated": str(p)}, descendant]
    assert _common(table, states,
                   lambda pid: {30: c, 20: d, 10: p, 1: _filetime(800)}[pid],
                   codex_threads=threads) == []


def test_codex_launcher_exact_gate_allows_verified_root_without_descendant(monkeypatch):
    monkeypatch.setattr(agent_links, "_CODEX_ENV_PER_THREAD_VERIFIED", True)
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    p, d, c = map(_filetime, (900, 950, 1000))
    table = {30: (20, "claude.exe"), 20: (10, "zsh"), 10: (1, "codex.exe"), 1: (0, "launchd")}
    states = _states(30, 10, c, p, launch=thread)
    found = _common(table, states,
                    lambda pid: {30: c, 20: d, 10: p, 1: _filetime(800)}[pid],
                    codex_threads=[{"id": thread, "runPid": 10, "runCreated": str(p)}])
    assert len(found) == 1 and found[0]["parent_provider"] == "Codex"


def test_codex_env_descendant_thread_cannot_replace_host_thread(monkeypatch):
    monkeypatch.setattr(agent_links, "_CODEX_ENV_PER_THREAD_VERIFIED", True)
    root = "01234567-89ab-cdef-0123-456789abcdef"
    descendant = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    p, d, c = map(_filetime, (900, 950, 1000))
    table = {30: (20, "claude.exe"), 20: (10, "zsh"), 10: (1, "codex.exe"), 1: (0, "launchd")}
    states = _states(30, 10, c, p, launch=descendant)
    threads = [{"id": root, "runPid": 10, "runCreated": str(p)},
               {"id": descendant, "parentId": root, "status": "idle",
                "activityStartedAt": 940, "activityEndedAt": 941}]
    assert _common(table, states,
                   lambda pid: {30: c, 20: d, 10: p, 1: _filetime(800)}[pid],
                   codex_threads=threads) == []


def test_codex_descendant_visible_activity_overlap_blocks_link(monkeypatch):
    monkeypatch.setattr(agent_links, "_CODEX_ENV_PER_THREAD_VERIFIED", True)
    root = "01234567-89ab-cdef-0123-456789abcdef"
    child = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    p, d, c = map(_filetime, (900, 950, 1000))
    table = {30: (20, "claude.exe"), 20: (10, "zsh"), 10: (1, "codex.exe"), 1: (0, "launchd")}
    states = _states(30, 10, c, p, launch=root)
    threads = [{"id": root, "runPid": 10, "runCreated": str(p)},
               {"id": child, "parentId": root, "status": "idle",
                "activityStartedAt": 949, "activityEndedAt": 952}]
    assert _common(table, states,
                   lambda pid: {30: c, 20: d, 10: p, 1: _filetime(800)}[pid],
                   codex_threads=threads) == []


def test_gated_app_server_host_uses_thread_row_when_switch_enabled(monkeypatch):
    monkeypatch.setattr(agent_links, "_CODEX_ENV_PER_THREAD_VERIFIED", True)
    root = "01234567-89ab-cdef-0123-456789abcdef"
    p, d, c = map(_filetime, (900, 950, 1000))
    table = {30: (20, "claude.exe"), 20: (10, "zsh"), 10: (1, "codex.exe"), 1: (0, "launchd")}
    states = _states(30, 10, c, p, launch=root)
    found = _common(table, states,
                    lambda pid: {30: c, 20: d, 10: p, 1: _filetime(800)}[pid],
                    codex_threads=[{"id": root, "source": "app-server"}])
    assert len(found) == 1 and found[0]["parent_thread_id"] == root


@pytest.mark.parametrize("descendant_rollout", [None, "delayed", "unreadable-json", "missing"])
def test_v5_switch_off_never_uses_codex_environment_or_rollout_state(monkeypatch, descendant_rollout):
    monkeypatch.setattr(agent_links, "_CODEX_ENV_PER_THREAD_VERIFIED", False)
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    p, d, c = map(_filetime, (900, 950, 1000))
    table = {30: (20, "claude.exe"), 20: (10, "zsh"), 10: (1, "codex.exe"), 1: (0, "launchd")}
    states = _states(30, 10, c, p, launch=thread)
    threads = [{"id": thread, "runPid": 10, "runCreated": str(p)}]
    if descendant_rollout is not None:
        threads.append({"id": "agent", "parentId": thread,
                        "rolloutState": descendant_rollout})
    assert _common(table, states,
                   lambda pid: {30: c, 20: d, 10: p, 1: _filetime(800)}[pid],
                   codex_threads=threads) == []


def test_claude_launcher_pid_reuse_before_first_tick_read_is_rejected():
    parent_created, direct_created, child_created = map(_filetime, (900, 950, 1000))
    table = {30: (20, "claude.exe"), 20: (10, "zsh"), 10: (1, "claude.exe"), 1: (0, "launchd")}
    states = _states(30, 10, child_created, parent_created)
    reads = {}

    def created(pid):
        reads[pid] = reads.get(pid, 0) + 1
        value = {30: child_created, 20: direct_created, 10: parent_created, 1: _filetime(800)}[pid]
        return value + (1 if pid == 10 and reads[pid] > 1 else 0)

    assert _common(table, states, created) == []


def test_process_born_after_first_snapshot_time_is_rejected():
    parent_created, direct_created, child_created = map(_filetime, (900, 950, 1000))
    table = {30: (20, "claude.exe"), 20: (10, "zsh"), 10: (1, "claude.exe"), 1: (0, "launchd")}
    states = _states(30, 10, child_created, parent_created)
    assert _common(table, states,
                   lambda pid: {30: child_created, 20: direct_created, 10: parent_created,
                                1: _filetime(800)}[pid],
                   snapshot_time=child_created - 1) == []


def test_foreign_owner_ancestor_mid_chain_blocks_link():
    parent_created, direct_created, child_created = map(_filetime, (900, 950, 1000))
    table = {30: (20, "claude.exe"), 20: (10, "zsh"), 10: (1, "claude.exe"), 1: (0, "launchd")}
    states = _states(30, 10, child_created, parent_created)
    assert agent_links.scan_claude_process_links(
        claude_states=states, table=table, current_sid=b"sid",
        equal_sid=lambda left, right: left == right,
        process_sid=lambda pid: b"other" if pid == 20 else b"sid",
        created=lambda pid: {30: child_created, 20: direct_created, 10: parent_created,
                             1: _filetime(800)}[pid], command_argv=lambda _pid: None) == []


def test_unreadable_node_host_between_agent_processes_stops_walk():
    parent_created, child_created = map(_filetime, (900, 1000))
    table = {30: (20, "claude.exe"), 20: (10, "node.exe"),
             10: (1, "claude.exe"), 1: (0, "launchd")}
    states = _states(30, 10, child_created, parent_created)
    assert agent_links.scan_claude_process_links(
        claude_states=states, table=table, current_sid=b"sid",
        equal_sid=lambda left, right: left == right, process_sid=lambda _pid: b"sid",
        created=lambda pid: {30: child_created, 20: _filetime(950), 10: parent_created,
                             1: _filetime(800)}[pid], command_argv=lambda _pid: None) == []


def test_receipt_inherited_through_codex_cannot_claim_claude_parent():
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    parent_created, child_created = _filetime(900), _filetime(1000)
    table = {30: (20, "codex.exe"), 20: (10, "codex.exe"), 10: (1, "launchd"),
             1: (0, "launchd")}
    receipt = {"thread_id": thread, "codex_pid": 30, "codex_created": child_created,
               "claude_session_id": "parent", "recorded_at": 1000, "method": "receipt"}
    states = {("parent", ""): {"pid_associations": [{"pid": 10, "created": parent_created,
                                                        "sid": "parent", "first": 900, "last": 1200}]}}
    assert not agent_links.validate_receipt_process(
        receipt, table=table, current_sid=b"sid", claude_states=states,
        process_sid=lambda _pid: b"sid", equal_sid=lambda left, right: left == right,
        created=lambda pid: {30: child_created, 20: _filetime(950), 10: parent_created,
                             1: _filetime(800)}[pid],
        command_line=lambda _pid: f"codex.exe exec resume {thread}")


def test_hook_timestamps_going_backwards_mark_process_association_ambiguous():
    state = claude_events.new_file_state()
    base = {"event": "UserPromptSubmit", "session_id": "session",
            "claude_pid": 10, "claude_pid_created": 100}
    claude_events.reduce_event(state, {**base, "ts": 2.0})
    claude_events.reduce_event(state, {**base, "ts": 1.0})
    association, = state["process_sessions"].values()
    assert association["ambiguous"] is True
    assert agent_links._association_map({("session", ""): state}).get((10, 100), ()) == ()


def _apply_cached_claude_link(tmp_path, monkeypatch, *, states=None, native_parent=False):
    child = {"id": "child", "status": "running"}
    codex_row = {"id": "01234567-89ab-cdef-0123-456789abcdef"}
    if native_parent:
        child.update(parentId="native-parent", parentProvider="Claude")
    providers = [{"name": "Claude", "chats": [child]}, {"name": "Codex", "chats": [codex_row]}]
    monkeypatch.setattr(agent_links, "_LAST_FULL_SCAN_AT", 1000)
    agent_links.apply_exact_links(providers, now=1001, registry_path=tmp_path / "registry.json",
                                  permission_check=lambda _: True,
                                  registry_reader=lambda _: [], registry_writer=lambda *a, **k: True,
                                  claude_states=states or {})
    return child


def test_process_edge_survives_parent_exit_and_resume_under_same_thread(tmp_path, monkeypatch):
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    link = {"child_session_id": "child", "child_pid": 30, "child_created": _filetime(1000),
            "parent_session_id": None, "parent_thread_id": thread, "parent_provider": "Codex",
            "parent_pid": 10, "parent_created": _filetime(900),
            "direct_child_pid": 20, "direct_child_created": _filetime(950), "method": "process"}
    monkeypatch.setattr(agent_links, "_CLAUDE_PROCESS_LINKS", {"child": link})
    child = _apply_cached_claude_link(tmp_path, monkeypatch)
    assert child["parentId"] == thread and child["parentProvider"] == "Codex"
    assert child["parentIncarnation"]["pid"] == 10


def test_delayed_sessionstart_revokes_frozen_claude_process_edge(tmp_path, monkeypatch):
    parent_created, child_created = _filetime(900), _filetime(1000)
    link = {"child_session_id": "child", "child_pid": 30, "child_created": child_created,
            "parent_session_id": "parent", "parent_thread_id": None, "parent_provider": "Claude",
            "parent_pid": 10, "parent_created": parent_created,
            "direct_child_pid": 20, "direct_child_created": _filetime(950), "method": "process"}
    monkeypatch.setattr(agent_links, "_CLAUDE_PROCESS_LINKS", {"child": link})
    states = {
        ("child", ""): {"pid_associations": [{"pid": 30, "created": child_created,
                                               "sid": "child", "first": 1000, "last": 1000}]},
        ("parent", ""): {"pid_associations": [{"pid": 10, "created": parent_created,
                                                "sid": "parent", "first": 900, "last": 1200}]},
        ("later", ""): {"pid_associations": [{"pid": 10, "created": parent_created,
                                               "sid": "later", "first": 950, "last": 1300}]},
    }
    child = _apply_cached_claude_link(tmp_path, monkeypatch, states=states)
    assert "parentId" not in child
    assert "child" not in agent_links._CLAUDE_PROCESS_LINKS


def test_native_parent_appearing_revokes_cached_process_edge(tmp_path, monkeypatch):
    link = {"child_session_id": "child", "child_pid": 30, "child_created": _filetime(1000),
            "parent_session_id": "parent", "parent_provider": "Claude", "parent_pid": 10,
            "parent_created": _filetime(900), "direct_child_pid": 20,
            "direct_child_created": _filetime(950), "method": "process"}
    monkeypatch.setattr(agent_links, "_CLAUDE_PROCESS_LINKS", {"child": link})
    child = _apply_cached_claude_link(tmp_path, monkeypatch, native_parent=True)
    assert child["parentId"] == "native-parent"
    assert "child" not in agent_links._CLAUDE_PROCESS_LINKS


def test_child_process_identity_change_revokes_cached_process_edge(tmp_path, monkeypatch):
    link = {"child_session_id": "child", "child_pid": 30, "child_created": _filetime(1000),
            "parent_session_id": "parent", "parent_provider": "Claude", "parent_pid": 10,
            "parent_created": _filetime(900), "direct_child_pid": 20,
            "direct_child_created": _filetime(950), "method": "process"}
    monkeypatch.setattr(agent_links, "_CLAUDE_PROCESS_LINKS", {"child": link})
    states = {("child", ""): {"pid": 31, "pid_created": _filetime(1100),
                                "pid_associations": [
                                    {"pid": 30, "created": _filetime(1000), "sid": "child",
                                     "first": 1000, "last": 1000},
                                    {"pid": 31, "created": _filetime(1100), "sid": "child",
                                     "first": 1100, "last": 1100}]}}
    child = _apply_cached_claude_link(tmp_path, monkeypatch, states=states)
    assert "parentId" not in child
    assert "child" not in agent_links._CLAUDE_PROCESS_LINKS


def test_app_server_codex_launcher_is_unlinked_while_switch_off(monkeypatch):
    monkeypatch.setattr(agent_links, "_CODEX_ENV_PER_THREAD_VERIFIED", False)
    root = "01234567-89ab-cdef-0123-456789abcdef"
    p, d, c = map(_filetime, (900, 950, 1000))
    table = {30: (20, "claude.exe"), 20: (10, "zsh"), 10: (1, "codex.exe"), 1: (0, "launchd")}
    states = _states(30, 10, c, p, launch=root)
    assert _common(table, states,
                   lambda pid: {30: c, 20: d, 10: p, 1: _filetime(800)}[pid],
                   codex_threads=[{"id": root, "source": "app-server"}]) == []


def test_three_level_chain_claude_codex_claude(monkeypatch):
    monkeypatch.setattr(agent_links, "_CODEX_ENV_PER_THREAD_VERIFIED", True)
    outer = "outer-session"
    thread = "01234567-89ab-cdef-0123-456789abcdef"
    outer_created, codex_created, child_created = map(_filetime, (900, 1000, 1100))
    table = {40: (30, "claude.exe"), 30: (20, "zsh"), 20: (15, "codex.exe"),
             15: (10, "bash"), 10: (1, "claude.exe"), 1: (0, "launchd")}
    states = {
        ("child", ""): {"pid_associations": [{"pid": 40, "created": child_created,
                                               "sid": "child", "first": 1100, "last": 1100}],
                          "launcher_associations": [{"pid": 40, "created": child_created,
                                                     "launcher_codex_thread_id": thread}]},
        (outer, ""): {"pid_associations": [{"pid": 10, "created": outer_created,
                                             "sid": outer, "first": 900, "last": 1200}]},
    }
    created = lambda pid: {40: child_created, 30: _filetime(1050), 20: codex_created,
                           15: _filetime(950), 10: outer_created, 1: _filetime(800)}[pid]
    codex_link = agent_links.scan_codex_processes(
        table=table, current_sid=b"sid", equal_sid=lambda a, b: a == b,
        process_sid=lambda _pid: b"sid", created=created,
        command_line=lambda _pid: f"codex.exe exec resume {thread}", claude_states=states)
    assert codex_link[0]["claude_session_id"] == outer
    codex_threads = [{"id": thread, "runPid": 20, "runCreated": str(codex_created),
                      "parentId": outer, "parentProvider": "Claude"}]
    claude_link = _common(table, states, created, codex_threads=codex_threads)
    assert len(claude_link) == 1
    assert claude_link[0]["parent_provider"] == "Codex"
    assert claude_link[0]["parent_thread_id"] == thread
