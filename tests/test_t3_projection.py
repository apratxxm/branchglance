import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from token_tracker import t3_projection as t3
from token_tracker.t3_projection import read_v2 as _read_v2

SCOPE = frozenset({"Claude", "Codex"})

def read_v2(db, since, **kwargs):
    return _read_v2(db, since, allowed=SCOPE, **kwargs)

def poll():
    projection = t3.poll(SCOPE)
    return list(projection.edges), dict(projection.titles)


P = "orchestration_v2_projection_"
PARENT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CHILD = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
NEW = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def fixture_db():
    db = sqlite3.connect(":memory:")
    db.executescript(f"""
      CREATE TABLE {P}threads (thread_id TEXT PRIMARY KEY, title TEXT, active_provider_thread_id TEXT,
        deleted_at TEXT, archived_at TEXT, updated_at TEXT);
      CREATE INDEX threads_recent ON {P}threads(updated_at);
      CREATE TABLE {P}provider_threads (provider_thread_id TEXT PRIMARY KEY, thread_id TEXT,
        provider TEXT, provider_instance_id TEXT, payload_json TEXT);
      CREATE TABLE {P}subagents (thread_id TEXT, run_id TEXT, parent_node_id TEXT, provider TEXT,
        provider_thread_id TEXT, child_thread_id TEXT, provider_instance_id TEXT, origin TEXT);
      CREATE INDEX subs_child ON {P}subagents(child_thread_id);
      CREATE TABLE {P}run_attempts (thread_id TEXT, run_id TEXT, root_node_id TEXT, provider TEXT,
        provider_thread_id TEXT, provider_instance_id TEXT, payload_json TEXT);
      CREATE INDEX attempts_thread ON {P}run_attempts(thread_id,run_id);
    """)
    native = lambda value: json.dumps({"nativeThreadRef": {"nativeId": value}})
    db.executemany(f"INSERT INTO {P}threads VALUES (?,?,?,NULL,NULL,'2026-11-22')", [
        ("parent", "Renamed", "new"), ("child", "Child", "child-binding")])
    db.executemany(f"INSERT INTO {P}provider_threads VALUES (?,?,?,?,?)", [
        ("launch", "parent", "claudeAgent", "p", native(PARENT)),
        ("new", "parent", "codex", "new-instance", native(NEW)),
        ("child-binding", "child", "codex", "c", native(CHILD))])
    db.execute(f"INSERT INTO {P}subagents VALUES (?,?,?,?,?,?,?,?)",
               ("parent", "run", "node", "codex", "child-binding", "child", "c", "app_owned"))
    db.execute(f"INSERT INTO {P}run_attempts VALUES (?,?,?,?,?,?,?)",
               ("parent", "run", "node", "claudeAgent", "launch", "p", json.dumps({"nativeThreadId": PARENT})))
    return db


def test_launch_binding_wins_over_current_provider_and_supports_claude_child():
    db = fixture_db()
    edges, _ = read_v2(db, "2026-11-21")
    assert edges == [(f"Codex:{CHILD}", f"Claude:{PARENT}")]
    db.execute(f"UPDATE {P}subagents SET provider='claudeAgent'")
    db.execute(f"UPDATE {P}provider_threads SET provider='claudeAgent' WHERE provider_thread_id='child-binding'")
    edges, _ = read_v2(db, "2026-11-21")
    assert edges == [(f"Claude:{CHILD}", f"Claude:{PARENT}")]


def test_missing_native_ambiguous_attempt_and_overflow_fail_closed():
    db = fixture_db()
    assert read_v2(db, "2026-11-21", seed_cap=1) is None
    assert _read_v2(db, "2026-11-21", allowed={"Claude"}, seed_cap=1) is None
    db.execute(f"UPDATE {P}provider_threads SET payload_json='{{}}' WHERE provider_thread_id='child-binding'")
    assert read_v2(db, "2026-11-21")[0] == []
    db = fixture_db()
    db.execute(f"INSERT INTO {P}run_attempts SELECT * FROM {P}run_attempts")
    assert read_v2(db, "2026-11-21")[0] == []
    db = fixture_db()
    db.execute(f"UPDATE {P}subagents SET provider_thread_id='pending:child'")
    assert read_v2(db, "2026-11-21")[0] == []
    db = fixture_db()
    db.execute(f"UPDATE {P}subagents SET provider_instance_id=NULL")
    db.execute(f"UPDATE {P}provider_threads SET provider_instance_id=NULL WHERE provider_thread_id='child-binding'")
    assert read_v2(db, "2026-11-21")[0] == []
    db = fixture_db()
    for _ in range(32):
        db.execute(f"INSERT INTO {P}run_attempts SELECT * FROM {P}run_attempts LIMIT 1")
    assert read_v2(db, "2026-11-21", seek_cap=32) is None


def test_dedicated_connection_accepts_builtin_json_index_without_app_functions(tmp_path):
    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as writer:
        writer.execute("CREATE TABLE example (payload TEXT)")
        writer.execute("CREATE INDEX example_json ON example(json_extract(payload, '$.id'))")
        writer.create_function("branchglance_test_only", 0, lambda: 1)
    db = t3._connect(path)
    try:
        assert db.execute("SELECT count(*) FROM example WHERE json_extract(payload, '$.id') = ?", ("x",)).fetchone() == (0,)
        assert not any(row[0] == "branchglance_test_only" for row in db.execute("PRAGMA function_list"))
        assert db.execute("PRAGMA trusted_schema").fetchone() == (1,)
    finally:
        db.close()


def test_titles_prefer_v2_follow_wal_and_clear_on_archive_rebind_and_deletion(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(t3, "database", lambda: (tmp_path / ".t3" / "userdata" / ("statev2.sqlite" if (tmp_path / ".t3" / "userdata" / "statev2.sqlite").exists() else "state.sqlite")))
    monkeypatch.setattr(t3, "POLL_SECONDS", 0)
    monkeypatch.setattr(t3, "MAX_UNCHANGED_SECONDS", 0)
    root = tmp_path / ".t3" / "userdata"
    root.mkdir(parents=True)
    old = root / "state.sqlite"
    with sqlite3.connect(old) as db:
        db.execute("CREATE TABLE projection_threads(thread_id TEXT,title TEXT,deleted_at TEXT,archived_at TEXT)")
        db.execute("CREATE TABLE provider_session_runtime(thread_id TEXT,provider_name TEXT,resume_cursor_json TEXT)")
        db.execute("INSERT INTO projection_threads VALUES ('old','Old title',NULL,NULL)")
        db.execute("INSERT INTO provider_session_runtime VALUES ('old','claudeAgent',?)", (json.dumps({"resume": PARENT}),))
    old_writer = sqlite3.connect(old)
    old_writer.execute("PRAGMA journal_mode=WAL")
    old_writer.execute("UPDATE projection_threads SET title=title")
    old_writer.commit()
    assert poll()[1] == {PARENT: "Old title"}
    v2 = root / "statev2.sqlite"
    source = fixture_db()
    source.execute(f"UPDATE {P}threads SET updated_at=?", (datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),))
    source.execute(f"UPDATE {P}threads SET active_provider_thread_id='launch' WHERE thread_id='parent'")
    source.commit()
    with sqlite3.connect(v2) as db:
        source.backup(db)
    db.close()
    source.close()
    writer = sqlite3.connect(v2)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(f"UPDATE {P}threads SET title=title WHERE thread_id='parent'")
        writer.commit()
        assert poll()[1] == {PARENT: "Renamed"}
        writer.execute(f"UPDATE {P}threads SET title='WAL title' WHERE thread_id='parent'")
        writer.commit()
        assert poll()[1] == {PARENT: "WAL title"}
        writer.execute(f"UPDATE {P}threads SET updated_at='2000-01-01', title='Older title' WHERE thread_id='parent'")
        writer.commit()
        assert poll()[1] == {PARENT: "Older title"}
        writer.execute(f"UPDATE {P}threads SET archived_at='yes' WHERE thread_id='parent'")
        writer.commit()
        assert poll()[1] == {}
        writer.execute(f"UPDATE {P}threads SET archived_at=NULL, deleted_at='yes' WHERE thread_id='parent'")
        writer.commit()
        assert poll()[1] == {}
        writer.execute(f"UPDATE {P}threads SET deleted_at=NULL, active_provider_thread_id='new' WHERE thread_id='parent'")
        writer.commit()
        assert poll()[1] == {}
    finally:
        writer.close()
    v2.unlink()
    assert poll()[1] == {PARENT: "Old title"}
    old_writer.close()
    old.unlink()
    assert poll()[1] == {}


def test_reparse_source_rejected_and_transient_connection_keeps_title(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(t3, "database", lambda: tmp_path / ".t3" / "userdata" / "state.sqlite")
    monkeypatch.setattr(t3, "POLL_SECONDS", 0)
    monkeypatch.setattr(t3, "MAX_UNCHANGED_SECONDS", 0)
    root = tmp_path / ".t3" / "userdata"
    root.mkdir(parents=True)
    dbpath = root / "state.sqlite"
    with sqlite3.connect(dbpath) as db:
        db.execute("CREATE TABLE projection_threads(thread_id TEXT,title TEXT,deleted_at TEXT,archived_at TEXT)")
        db.execute("CREATE TABLE provider_session_runtime(thread_id TEXT,provider_name TEXT,resume_cursor_json TEXT)")
        db.execute("INSERT INTO projection_threads VALUES ('a','Title',NULL,NULL)")
        db.execute("INSERT INTO provider_session_runtime VALUES ('a','claudeAgent',?)", (json.dumps({"resume": PARENT}),))
    writer = sqlite3.connect(dbpath, timeout=0.1)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("UPDATE projection_threads SET title=title")
    writer.commit()
    assert poll()[1] == {PARENT: "Title"}
    try:
        writer.execute("BEGIN EXCLUSIVE")
        assert poll()[1] == {PARENT: "Title"}
    finally:
        writer.rollback()
        writer.close()
    with monkeypatch.context() as m:
        m.setattr(t3.product, "info_is_reparse", lambda info: True)
        assert poll()[0] == []


@pytest.fixture
def projection_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    root = tmp_path / ".t3" / "userdata"
    root.mkdir(parents=True)
    path = root / "statev2.sqlite"
    monkeypatch.setattr(t3, "database", lambda: path)
    monkeypatch.setattr(t3, "POLL_SECONDS", 0)
    source = fixture_db()
    source.execute(f"UPDATE {P}threads SET updated_at=?", (datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),))
    source.execute(f"UPDATE {P}threads SET active_provider_thread_id='launch' WHERE thread_id='parent'")
    source.commit()
    target = sqlite3.connect(path)
    source.backup(target)
    target.close()  # an open connection would lock the file this fixture hands out
    source.close()
    return path


@pytest.fixture
def running_projection_file(projection_file):
    writer = sqlite3.connect(projection_file)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(f"UPDATE {P}threads SET title=title WHERE thread_id='parent'")
    writer.commit()
    yield projection_file
    writer.close()


def test_structure_stamp_tracks_projection_not_sqlite_writes(projection_file, monkeypatch):
    from token_tracker import providers
    def stamp():
        token = providers._T3_STAMP_PROJECTION.set(t3.poll(SCOPE))
        try:
            return providers._structure_stamp(())
        finally:
            providers._T3_STAMP_PROJECTION.reset(token)
    monkeypatch.setattr(t3, "MAX_UNCHANGED_SECONDS", 0)
    writer = sqlite3.connect(projection_file)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(f"UPDATE {P}threads SET title=title WHERE thread_id='parent'")
        writer.commit()
        poll()
        first = stamp()
        writer.execute(f"UPDATE {P}threads SET updated_at=updated_at || ' ' WHERE thread_id='child'")
        writer.commit()
        poll()
        assert stamp() == first
        writer.execute(f"UPDATE {P}threads SET title='changed' WHERE thread_id='parent'")
        writer.commit()
        poll()
        second = stamp()
        assert second != first
        writer.execute(f"UPDATE {P}subagents SET origin='other'")
        writer.commit()
        poll()
        assert stamp() != second
    finally:
        writer.close()


@pytest.mark.parametrize("change", ["commit", "checkpoint", "create_wal"])
def test_sqlite_family_can_change_during_read(projection_file, monkeypatch, change):
    monkeypatch.setattr(t3, "MAX_UNCHANGED_SECONDS", 0)
    writer = sqlite3.connect(projection_file)
    if change != "create_wal":
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(f"UPDATE {P}threads SET title='before' WHERE thread_id='parent'")
        writer.commit()
    original = t3.read_v2
    def changed(db, since, **kwargs):
        if change == "commit":
            result = original(db, since, **kwargs)
        if change == "create_wal":
            writer.execute("PRAGMA journal_mode=WAL")
        if change == "checkpoint":
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        else:
            writer.execute(f"UPDATE {P}threads SET title='after' WHERE thread_id='parent'")
            writer.commit()
        return result if change == "commit" else original(db, since, **kwargs)
    monkeypatch.setattr(t3, "read_v2", changed)
    try:
        edges, titles = poll()
        if change == "create_wal":
            assert (edges, titles) == ([], {})
            return
        assert edges == [(f"Codex:{CHILD}", f"Claude:{PARENT}")]
        assert titles[PARENT] == "before"
    finally:
        writer.close()


def test_unchanged_read_skips_queries_then_refreshes_and_expires(running_projection_file, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(t3.time, "monotonic", lambda: now[0])
    connect = t3._connect
    calls = []
    def tracked(path, **kwargs):
        calls.append(path)
        return connect(path, **kwargs)
    monkeypatch.setattr(t3, "_connect", tracked)
    first = poll()
    now[0] += 1
    assert poll() == first and len(calls) == 1
    now[0] += 4
    assert poll() == first and len(calls) == 2
    with sqlite3.connect(running_projection_file) as writer:
        writer.execute(f"DROP INDEX attempts_thread")
    now[0] += 1
    assert poll() == first
    now[0] += 59
    assert poll() == ([], {})


def test_initial_unavailable_read_obeys_poll_floor(projection_file, monkeypatch):
    now = [100.0]
    calls = []
    monkeypatch.setattr(t3.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(t3, "POLL_SECONDS", 1)
    def unavailable(path, **kwargs):
        calls.append(path)
        raise PermissionError("unavailable")
    monkeypatch.setattr(t3, "_checked_files", unavailable)
    assert poll() == ([], {})
    now[0] += 0.5
    assert poll() == ([], {}) and len(calls) == 1
    now[0] += 0.5
    assert poll() == ([], {}) and len(calls) == 2


def test_recheck_rejects_main_identity_swap_and_redirected_sources(projection_file, monkeypatch):
    snapshots = t3._checked_files(projection_file)
    if True:
        main = next(info for path, info in snapshots if path == projection_file)
        original_lstat = Path.lstat
        class Swapped:
            def __init__(self, info):
                self._info = info
                self.st_ino = info.st_ino + 1
            def __getattr__(self, name):
                return getattr(self._info, name)
        monkeypatch.setattr(Path, "lstat", lambda path: Swapped(main) if path == projection_file else original_lstat(path))
        with pytest.raises(OSError):
            t3._recheck(snapshots)
    monkeypatch.setattr(Path, "lstat", original_lstat)
    wal = Path(str(projection_file) + "-wal")
    wal.write_bytes(b"sidecar")
    original_reparse = t3.product.info_is_reparse
    for item in (projection_file.parent, projection_file, wal):
        identity = (item.lstat().st_dev, item.lstat().st_ino)
        monkeypatch.setattr(t3.product, "info_is_reparse",
                            lambda info, identity=identity: (info.st_dev, info.st_ino) == identity or original_reparse(info))
        with pytest.raises((OSError, t3.claude_hook_install.HookInstallError)):
            t3._checked_files(projection_file)


def test_consent_blocks_path_access_and_denied_provider_payloads(monkeypatch):
    monkeypatch.setattr(t3, "database", lambda: pytest.fail("empty consent touched the filesystem"))
    assert not t3.poll(set()).edges and not t3.poll(set()).titles
    db = fixture_db()
    denied = db.execute(f"SELECT payload_json FROM {P}provider_threads WHERE provider_thread_id='launch'").fetchone()[0]
    payload = t3._payload
    monkeypatch.setattr(t3, "_payload", lambda raw: pytest.fail("denied payload read") if raw == denied else payload(raw))
    edges, titles = _read_v2(db, "2026-11-21", allowed={"Codex"})
    assert not edges and not titles
    monkeypatch.setattr(t3, "_payload", payload)
    db.execute(f"UPDATE {P}threads SET active_provider_thread_id='launch' WHERE thread_id='parent'")
    statements = []
    db.set_trace_callback(statements.append)
    edges, titles = _read_v2(db, "2026-11-21", allowed={"Claude"})
    assert not edges and titles == {PARENT: "Renamed"}
    assert any("length(title)" in statement and "'parent'" in statement for statement in statements)
    assert not any("length(title)" in statement and "'child'" in statement for statement in statements)


def test_sql_never_returns_denied_bindings_or_oversized_values():
    db = fixture_db()
    db.execute(f"UPDATE {P}provider_threads SET provider_thread_id='denied-binding', provider_instance_id='denied-instance' WHERE provider_thread_id='launch'")
    db.execute(f"UPDATE {P}run_attempts SET provider_thread_id='denied-binding', provider_instance_id='denied-instance'")
    db.execute(f"UPDATE {P}provider_threads SET payload_json=? WHERE provider_thread_id='child-binding'", ("x" * 70000,))
    seen = []
    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor
        def fetchall(self):
            rows = self.cursor.fetchall()
            seen.extend(value for row in rows for value in row)
            return rows
        def fetchone(self):
            row = self.cursor.fetchone()
            if row:
                seen.extend(row)
            return row
    class Traced:
        def execute(self, sql, args=()):
            return Cursor(db.execute(sql, args))
    edges, titles = _read_v2(Traced(), "2026-11-21", allowed={"Codex"})
    assert not edges and not titles
    assert "denied-binding" not in seen and "denied-instance" not in seen
    seen.clear()
    _read_v2(Traced(), "2026-11-21", allowed=SCOPE)
    assert not any(isinstance(value, str) and len(value) > 65536 for value in seen)


def test_publication_is_immutable_normalized_and_cycle_order_independent():
    pairs = [("Codex:a", "Claude:b"), ("Claude:b", "Codex:a"),
             ("Codex:c", "Claude:d"), ("Codex:c", "Claude:e"), ("Codex:z", "Claude:q")]
    first = t3._publish(pairs, {PARENT: "hello /private/path world"})
    second = t3._publish(list(reversed(pairs)), {PARENT: "hello world"})
    assert first == second
    assert dict(first.edges) == {"Codex:z": "Claude:q"}
    assert dict(first.titles) == {PARENT: "hello world"}
    with pytest.raises(AttributeError):
        first.edges = ()


def test_overlay_titles_are_immutable_per_caller():
    projection = t3._publish(titles={PARENT: "Parent", CHILD: "Child"})
    narrow = projection.overlay()
    narrow["titles"].pop(f"Claude:{CHILD}")
    assert projection.overlay()["titles"][f"Claude:{CHILD}"] == "Child"
    assert len(projection.titles) == 2


def test_poll_owner_contention_is_nonblocking_and_scope_isolated(running_projection_file, monkeypatch):
    monkeypatch.setattr(t3, "POLL_SECONDS", 0)
    ready, release = threading.Event(), threading.Event()
    original = t3._connect
    def slow(path, **kwargs):
        ready.set()
        assert release.wait(2)
        return original(path, **kwargs)
    monkeypatch.setattr(t3, "_connect", slow)
    thread = threading.Thread(target=lambda: t3.poll(SCOPE))
    thread.start()
    try:
        assert ready.wait(2)
        start = time.perf_counter()
        assert not t3.poll({"Codex"}, live=True).edges
        assert time.perf_counter() - start < .05
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()


def test_absent_sidecars_never_create_files_or_read(projection_file, monkeypatch):
    monkeypatch.setattr(t3, "_connect", lambda *args, **kwargs: pytest.fail("opened without sidecars"))
    assert not Path(str(projection_file) + "-wal").exists()
    assert not t3.poll(SCOPE).edges
    assert not Path(str(projection_file) + "-wal").exists()
    assert not Path(str(projection_file) + "-shm").exists()


def test_cached_projection_and_digest_are_scoped_to_consent(running_projection_file):
    claude = t3.poll({"Claude"})
    codex = t3.poll({"Codex"})
    assert dict(claude.titles) == {PARENT: "Renamed"}
    assert not codex.titles and not claude.edges and not codex.edges
    assert claude.scope == ("Claude",) and codex.scope == ("Codex",)
    assert claude.digest != codex.digest


def test_publication_generation_and_sequence_follow_scope_policy_and_content(running_projection_file, monkeypatch):
    monkeypatch.setattr(t3, "MAX_UNCHANGED_SECONDS", 0)
    first = t3.poll(SCOPE, generation="policy-a")
    assert first.instance and first.generation > 0 and first.seq > 0
    assert t3.poll(SCOPE, generation="policy-a") == first
    with sqlite3.connect(running_projection_file) as writer:
        writer.execute(f"UPDATE {P}threads SET title='renamed again' WHERE thread_id='parent'")
    changed = t3.poll(SCOPE, generation="policy-a")
    assert changed.instance == first.instance and changed.generation == first.generation and changed.seq == first.seq + 1
    other = t3.poll(SCOPE, generation="policy-b")
    assert other.instance == changed.instance and other.generation > changed.generation
    scoped = t3.poll({"Claude"}, generation="policy-b")
    assert scoped.instance == other.instance and scoped.generation > other.generation


def test_process_restart_receives_a_new_instance():
    code = "from token_tracker.t3_projection import empty; print(empty(set(), 'policy').instance)"
    first = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True, timeout=5).stdout.strip()
    second = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True, timeout=5).stdout.strip()
    assert first and second and first != second


def test_inflight_full_keeps_its_captured_generation(running_projection_file, monkeypatch):
    ready, release, captured = threading.Event(), threading.Event(), []
    original = t3._connect
    def paused(path, **kwargs):
        ready.set()
        assert release.wait(2)
        return original(path, **kwargs)
    monkeypatch.setattr(t3, "_connect", paused)
    thread = threading.Thread(target=lambda: captured.append(t3.poll(SCOPE, generation="old")))
    thread.start()
    try:
        assert ready.wait(2)
        current = t3.empty({"Codex"}, "new")
    finally:
        release.set()
        thread.join(3)
    assert captured and captured[0].instance == current.instance
    assert captured[0].generation < current.generation


def test_deadline_and_cancellation_during_python_parsing_keep_previous(running_projection_file, monkeypatch):
    monkeypatch.setattr(t3, "MAX_UNCHANGED_SECONDS", 0)
    first = t3.poll(SCOPE)
    assert first.edges
    original = t3._payload
    def slow(raw):
        time.sleep(.03)
        return original(raw)
    with monkeypatch.context() as m:
        m.setattr(t3, "_payload", slow)
        assert t3.poll(SCOPE, live=True) == first
    calls = [0]
    def cancelled():
        calls[0] += 1
        return calls[0] >= 15
    assert t3.poll(SCOPE, cancelled=cancelled) == first
    assert calls[0] >= 15


def test_checkpointed_result_survives_absent_sidecars_until_main_changes(projection_file, monkeypatch):
    writer = sqlite3.connect(projection_file)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(f"UPDATE {P}threads SET title=title WHERE thread_id='parent'")
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    first = t3.poll(SCOPE)
    assert first.edges
    cache = next(iter(t3._CACHE.values()))
    assert cache["checkpoint"] is not None
    writer.close()
    assert not Path(str(projection_file) + "-wal").exists()
    cache["successful_at"] -= 120
    monkeypatch.setattr(t3, "_connect", lambda *args, **kwargs: pytest.fail("offline read"))
    assert t3.poll(SCOPE) == first
    info = projection_file.stat()
    os.utime(projection_file, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    cleared = t3.poll(SCOPE)
    assert not cleared.edges and not cleared.titles and cleared.seq == first.seq + 1


def test_later_present_sidecars_revoke_checkpoint_retention(projection_file):
    writer = sqlite3.connect(projection_file)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(f"UPDATE {P}threads SET title=title WHERE thread_id='parent'")
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    first = t3.poll(SCOPE)
    assert first.edges and next(iter(t3._CACHE.values()))["checkpoint"] is not None
    assert t3.poll(SCOPE) == first
    writer.close()
    assert not t3.poll(SCOPE).edges


def test_one_present_sidecar_revokes_offline_checkpoint(projection_file, monkeypatch):
    writer = sqlite3.connect(projection_file)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(f"UPDATE {P}threads SET title=title WHERE thread_id='parent'")
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    first = t3.poll(SCOPE)
    assert first.edges and next(iter(t3._CACHE.values()))["checkpoint"] is not None
    writer.close()
    Path(str(projection_file) + "-shm").touch()
    monkeypatch.setattr(t3, "_connect", lambda *args, **kwargs: pytest.fail("read with missing WAL"))
    assert not t3.poll(SCOPE).edges


def test_expired_projection_is_cleared_even_when_cancelled(running_projection_file):
    first = t3.poll(SCOPE)
    assert first.edges
    next(iter(t3._CACHE.values()))["successful_at"] -= 61
    cleared = t3.poll(SCOPE, cancelled=lambda: True)
    assert not cleared.edges and cleared.seq == first.seq + 1


def test_verified_offline_checkpoint_survives_contender_and_cancellation(projection_file, monkeypatch):
    writer = sqlite3.connect(projection_file)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(f"UPDATE {P}threads SET title=title WHERE thread_id='parent'")
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    first = t3.poll(SCOPE)
    writer.close()
    assert t3.poll(SCOPE) == first
    cache = next(iter(t3._CACHE.values()))
    assert cache["offline"] is True
    cache["successful_at"] -= 120
    assert t3.poll(SCOPE, cancelled=lambda: True) == first
    assert t3._OWNER.acquire(blocking=False)
    try:
        assert t3.poll(SCOPE, live=True) == first
    finally:
        t3._OWNER.release()
    info = projection_file.stat()
    os.utime(projection_file, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    cleared = t3.poll(SCOPE)
    assert not cleared.edges and cleared.seq == first.seq + 1


def test_live_connection_uses_short_timeout_and_cancellation(projection_file):
    db = t3._connect(projection_file, live=True, cancelled=lambda: True)
    try:
        assert db.execute("PRAGMA busy_timeout").fetchone() == (20,)
        with pytest.raises(sqlite3.OperationalError, match="interrupt"):
            db.execute("WITH RECURSIVE x(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM x WHERE n<100000) SELECT sum(n) FROM x").fetchone()
    finally:
        db.close()


def test_cancelled_poll_does_not_touch_the_source(monkeypatch):
    monkeypatch.setattr(t3, "database", lambda: pytest.fail("cancelled poll accessed T3"))
    assert not t3.poll(SCOPE, cancelled=lambda: True).edges


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_inspection_adds_no_lock_to_the_database_or_its_sidecars(projection_file, monkeypatch):
    # SQLite itself denies delete access while any connection is open, T3's own included. The inspection
    # around it must not: every file stays deletable and renameable outside SQLite's own read.
    import ctypes
    from ctypes import wintypes
    create = ctypes.windll.kernel32.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    checks = []

    def assert_unlocked(path):
        moved = path.with_name(path.name + ".moved")
        handle = create(str(path), 0x10000, 7, None, 3, 0x80, None)
        assert handle != wintypes.HANDLE(-1).value, path.name
        ctypes.windll.kernel32.CloseHandle(handle)
        path.rename(moved)
        moved.rename(path)
        checks.append(path)

    original = t3._connect

    def checked(path, **kwargs):
        assert_unlocked(projection_file)
        return original(path, **kwargs)

    sidecars = [Path(str(projection_file) + suffix) for suffix in ("-wal", "-shm")]
    for sidecar in sidecars:
        sidecar.touch()
    monkeypatch.setattr(t3, "_connect", checked)
    assert t3.poll(SCOPE).edges
    assert_unlocked(projection_file)
    snapshots = t3._checked_files(projection_file)
    for path in (projection_file, *sidecars):
        assert_unlocked(path)
    t3._recheck(snapshots)
    for path in (projection_file, *sidecars):
        assert_unlocked(path)


def _verified_offline(projection_file):
    writer = sqlite3.connect(projection_file)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(f"UPDATE {P}threads SET title=title WHERE thread_id='parent'")
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    first = t3.poll(SCOPE)
    writer.close()
    assert first.edges and t3.poll(SCOPE) == first
    assert next(iter(t3._CACHE.values()))["offline"] is True
    return first


@pytest.mark.parametrize("raises", [
    lambda: t3._SourceRejected("redirected T3 file"),
    lambda: t3.claude_hook_install.HookInstallError("settings path contains a symlink or reparse point"),
])
def test_owner_drops_an_offline_checkpoint_when_the_source_is_rejected(projection_file, monkeypatch, raises):
    first = _verified_offline(projection_file)
    def rejected(*args, **kwargs):
        raise raises()
    monkeypatch.setattr(t3, "_checked_files", rejected)
    cleared = t3.poll(SCOPE)
    assert not cleared.edges and not cleared.titles and cleared.seq == first.seq + 1


def test_owner_keeps_an_offline_checkpoint_on_a_transient_failure(projection_file, monkeypatch):
    first = _verified_offline(projection_file)
    next(iter(t3._CACHE.values()))["successful_at"] -= 120
    def unreadable(*args, **kwargs):
        raise t3.claude_hook_install.HookInstallError("cannot validate settings path chain") from PermissionError()
    monkeypatch.setattr(t3, "_checked_files", unreadable)
    assert t3.poll(SCOPE) == first
    monkeypatch.setattr(t3, "_checked_files", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    assert t3.poll(SCOPE) == first


def test_redirected_database_file_is_rejected_conclusively(projection_file, monkeypatch):
    _verified_offline(projection_file)
    real = t3.product.info_is_reparse
    monkeypatch.setattr(t3.product, "info_is_reparse", lambda info: True)
    with pytest.raises((t3._SourceRejected, t3.claude_hook_install.HookInstallError)):
        t3._checked_files(projection_file)
    assert not t3.poll(SCOPE).edges
    monkeypatch.setattr(t3.product, "info_is_reparse", real)


@pytest.mark.parametrize("change", ["replaced", "redirected sidecar"])
def test_owner_drops_an_offline_checkpoint_rejected_on_reinspection(projection_file, monkeypatch, change):
    first = _verified_offline(projection_file)
    original = t3._recheck
    def recheck(snapshots, *, check=lambda: None):
        if change == "replaced":
            other = projection_file.with_name("other.sqlite")
            other.write_bytes(projection_file.read_bytes())
            os.replace(other, projection_file)
        else:
            os.mkdir(str(projection_file) + "-wal")  # wrong file type, like a redirect
        return original(snapshots, check=check)
    monkeypatch.setattr(t3, "_recheck", recheck)
    cleared = t3.poll(SCOPE)
    assert not cleared.edges and cleared.seq == first.seq + 1


def test_owner_keeps_an_offline_checkpoint_when_reinspection_fails_transiently(projection_file, monkeypatch):
    first = _verified_offline(projection_file)
    monkeypatch.setattr(t3, "_recheck", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    assert t3.poll(SCOPE) == first
