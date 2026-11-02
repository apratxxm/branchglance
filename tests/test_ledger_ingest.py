"""Ingestion, cursors, transactions, store integrity, policy and recovery."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from test_ledger_support import (  # ledger_guard is autouse
    DAY_END, HOUR_START, append_jsonl, claude_file, claude_response, clock_at, codex_count, codex_file,
    codex_meta, ledger_guard, make_roots, write_jsonl,
)

from token_tracker import ledger_store as store
from token_tracker import ledger_ingest
from token_tracker.ledger import Ledger
from token_tracker.ledger_store import LedgerSchemaError

ALL = {"Claude": True, "Codex": True}


def test_discovery_never_probes_disabled_provider(tmp_path, monkeypatch):
    ledger, claude, codex = open_ledger(tmp_path)
    original = Path.is_dir

    def guard(path):
        assert not path.is_relative_to(codex), "disabled provider was probed"
        return original(path)

    monkeypatch.setattr(Path, "is_dir", guard)
    ledger.ingest({"Claude": True, "Codex": False})
    ledger.ingest({})


def test_provider_pending_and_timestamps_are_separate(tmp_path):
    ledger, claude, codex = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), claude_lines((10, "msg_a", 5)))
    write_jsonl(codex_file(codex, "T1"), codex_meta("T1", 10))
    ledger.ingest({"Claude": True, "Codex": False}, budget_bytes=1)
    assert store.meta_value(ledger.conn, "observedFrom:Claude") is not None
    assert store.meta_value(ledger.conn, "lastIngestedAt:Claude") is not None
    assert store.meta_value(ledger.conn, "observedFrom:Codex") is None
    assert store.meta_value(ledger.conn, "pendingBytes:Codex") == "0"


def test_invalid_last_usage_is_omitted_without_aborting(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    bad = codex_count(11, input_tokens=10, output_tokens=5, total=15,
                      last={"total_tokens": "bad"})
    write_jsonl(codex_file(codex, "T1"), codex_meta("T1", 10) + [bad])
    ledger.ingest(ALL)
    assert total_requests(ledger) == 0
    assert ledger.conn.execute("SELECT COUNT(*) FROM ledger_omissions WHERE provider='Codex'").fetchone()[0] == 1


def test_oversized_record_is_skipped_in_bounded_chunks_and_gap_is_durable(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, "MAX_RECORD_BYTES", 64)
    monkeypatch.setattr(ledger_ingest, "_READ_CHUNK", 16)
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 200 + b"\n" + b"{}\n")
    result = ledger.ingest(ALL, budget_bytes=8)
    assert result["bytesRead"] == 65
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='oversize'").fetchone()[0] == 1
    for _ in range(30):
        result = ledger.ingest(ALL, budget_bytes=8)
        if not result["pendingBytes"]:
            break
    assert result["pendingBytes"] == 0
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='oversize'").fetchone()[0] == 1


def open_ledger(tmp_path, at: int = 3600):
    data, claude, codex = make_roots(tmp_path)
    return Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(at)), claude, codex


def summary(ledger, show=ALL):
    return ledger.summary(HOUR_START, DAY_END, show=show)


def total_requests(ledger) -> int:
    return sum(row["requests"] for row in summary(ledger)["rows"])


def claude_lines(*specs):
    return [claude_response(seconds, msg, output=output) for seconds, msg, output in specs]


def test_truncation_records_gap_without_recounting(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "s1")
    first, second = claude_lines((10, "msg_a", 5), (20, "msg_b", 7))
    write_jsonl(path, [first, second])
    ledger.ingest(ALL)
    assert total_requests(ledger) == 2

    write_jsonl(path, [first])  # truncated: nothing new, and nothing may be counted twice
    ledger.ingest(ALL)
    assert total_requests(ledger) == 2
    coverage = ledger.coverage()
    assert coverage["truncations"] == 1
    assert coverage["status"] == "partial"
    assert coverage["gaps"][0]["kind"] == "truncation"

    append_jsonl(path, [claude_response(30, "msg_c", output=9)])
    ledger.ingest(ALL)
    assert total_requests(ledger) == 3


def test_replacement_records_gap(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "s1")
    write_jsonl(path, claude_lines((10, "msg_a", 5)))
    ledger.ingest(ALL)
    write_jsonl(path, claude_lines((5, "msg_x", 9), (20, "msg_b", 7)))  # different head, larger file
    ledger.ingest(ALL)
    assert ledger.coverage()["replacements"] == 1
    assert total_requests(ledger) == 3


def test_partial_last_line_waits_for_newline(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "s1")
    first, second = claude_lines((10, "msg_a", 5), (20, "msg_b", 7))
    line1 = json.dumps(first) + "\n"
    line2 = json.dumps(second) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(line1 + line2[:25], encoding="utf-8")
    ledger.ingest(ALL)
    assert total_requests(ledger) == 1
    assert ledger.coverage()["pendingBytes"] > 0
    path.write_text(line1 + line2, encoding="utf-8")
    ledger.ingest(ALL)
    assert total_requests(ledger) == 2


@pytest.mark.parametrize("stage", ["after_facts", "after_cursor"])
def test_crash_inside_transaction_counts_nothing_twice_or_loses(tmp_path, stage):
    ledger, claude, _ = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), claude_lines((10, "msg_a", 5), (20, "msg_b", 7), (30, "msg_c", 9)))

    def crash(name):
        if name == stage:
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        ledger.ingest(ALL, fault=crash)
    assert total_requests(ledger) == 0
    assert ledger.coverage()["observedFrom"] is None
    ledger.ingest(ALL)
    assert total_requests(ledger) == 3
    ledger.ingest(ALL)
    assert total_requests(ledger) == 3


def test_concurrent_readers_see_consistent_snapshots(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [claude_response(i % 3000, f"msg_{i}", output=i + 1) for i in range(300)])
    errors: list[object] = []
    checked = [0]
    stop = threading.Event()

    def reader():
        conn = store.connect(ledger.db_path)
        try:
            while not stop.is_set():
                with store.snapshot(conn):
                    facts, rollups = store.fact_and_rollup_totals(conn)
                if facts != rollups:
                    errors.append((facts, rollups))
                checked[0] += 1
        except Exception as exc:  # any reader failure fails the test
            errors.append(repr(exc))
        finally:
            conn.close()

    readers = [threading.Thread(target=reader) for _ in range(2)]
    for thread in readers:
        thread.start()
    try:
        for _ in range(1000):
            result = ledger.ingest(ALL, budget_bytes=3000)
            if result["pendingBytes"] == 0:
                break
    finally:
        stop.set()
        for thread in readers:
            thread.join()
    assert errors == []
    assert checked[0] > 0
    assert total_requests(ledger) == 300


def test_corrupt_database_is_reported_quarantined_and_rebuilt(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    write_jsonl(claude_file(claude, "s1"), claude_lines((10, "msg_a", 5), (20, "msg_b", 7)))
    data.mkdir(parents=True)
    (data / store.DB_NAME).write_bytes(b"this is not a sqlite database" * 300)
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    health = ledger.health()
    assert health["status"] == "rebuilt"
    assert health["recovery"]["reason"] in {"unreadable", "corrupt"}
    assert (data / health["recovery"]["quarantined"]).exists()
    ledger.ingest(ALL)
    assert total_requests(ledger) == 2


def test_collection_policy_gates_reads(tmp_path):
    ledger, claude, codex = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), claude_lines((10, "msg_a", 5)))
    write_jsonl(codex_file(codex, "T1"), codex_meta("T1", 10) + [codex_count(
        11, input_tokens=10, output_tokens=5, total=15,
        last={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})])
    ledger.ingest({"Claude": False, "Codex": True})
    assert ledger._ingestor.stats["sourceReads"] == 1  # the Codex file only
    rows = summary(ledger)["rows"]
    assert {row["provider"] for row in rows} == {"Codex"}
    assert ledger.coverage()["providersNotCollected"] == ["Claude"]
    ledger.ingest({})  # fail closed: nothing collected, nothing read
    assert ledger._ingestor.stats["sourceReads"] == 1


def test_show_policy_hides_provider_history(tmp_path):
    ledger, claude, codex = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), claude_lines((10, "msg_a", 5)))
    write_jsonl(codex_file(codex, "T1"), codex_meta("T1", 10) + [codex_count(
        11, input_tokens=10, output_tokens=5, total=15,
        last={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})])
    ledger.ingest(ALL)
    shown = summary(ledger, show={"Claude": False, "Codex": True})
    assert {row["provider"] for row in shown["rows"]} == {"Codex"}
    assert shown["totals"]["totalTokens"] == 15


def test_budgeted_passes_converge_to_full_result(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [claude_response(i, f"msg_{i}", output=i + 1) for i in range(60)])
    passes = 0
    while True:
        passes += 1
        result = ledger.ingest(ALL, budget_bytes=1500)
        if result["pendingBytes"] == 0:
            break
        assert passes < 500
    assert passes > 1
    assert total_requests(ledger) == 60


def test_purge_removes_one_provider(tmp_path):
    ledger, claude, codex = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), claude_lines((10, "msg_a", 5)))
    write_jsonl(codex_file(codex, "T1"), codex_meta("T1", 10) + [codex_count(
        11, input_tokens=10, output_tokens=5, total=15,
        last={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})])
    ledger.ingest(ALL)
    ledger.purge("Claude")
    assert {row["provider"] for row in summary(ledger)["rows"]} == {"Codex"}
    assert ledger.coverage()["gapCount"] == 0


def test_rebuild_rollups_reproduces_summary(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [claude_response(10, "msg_1", output=5),
                                            claude_response(12, "msg_1", output=20)])
    ledger.ingest(ALL)
    before = summary(ledger)["rows"]
    ledger.rebuild_rollups()
    assert summary(ledger)["rows"] == before


def test_override_applies_and_clears(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), claude_lines((10, "msg_a", 5)))
    ledger.ingest(ALL)
    assert summary(ledger)["rows"][0]["kind"] == "lead"
    ledger.set_override("Claude", "s1", "build")
    row = summary(ledger)["rows"][0]
    assert row["kind"] == "build"
    assert row["classification"] == {"status": "override", "source": "override"}
    ledger.clear_override("Claude", "s1")
    assert summary(ledger)["rows"][0]["kind"] == "lead"
    with pytest.raises(ValueError):
        ledger.set_override("Claude", "s1", "banana")


def test_migration_hook_runs_forward(tmp_path, monkeypatch):
    next_version = max(store.MIGRATIONS) + 1
    monkeypatch.setitem(store.MIGRATIONS, next_version, ("CREATE TABLE IF NOT EXISTS probe(x INTEGER)",))
    ledger, _, _ = open_ledger(tmp_path)
    assert ledger.health()["schemaVersion"] == next_version
    assert ledger.conn.execute("SELECT name FROM sqlite_master WHERE name='probe'").fetchone() is not None


def test_newer_schema_is_refused(tmp_path):
    ledger, _, _ = open_ledger(tmp_path)
    ledger.conn.execute("UPDATE meta SET value='99' WHERE key='schemaVersion'")
    ledger.close()
    with pytest.raises(LedgerSchemaError):
        open_ledger(tmp_path)


def test_coverage_states(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    assert ledger.coverage()["status"] == "empty"
    write_jsonl(claude_file(claude, "s1"), claude_lines((10, "msg_a", 5)))
    ledger.ingest(ALL)
    coverage = ledger.coverage()
    assert coverage["status"] == "complete"
    assert coverage["observedFrom"] is not None
    assert coverage["pendingBytes"] == 0


def test_replayed_older_codex_source_preserves_active_counter(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    a, b = codex_file(codex, "a"), codex_file(codex, "b")
    first = codex_meta("T1", 1) + [codex_count(2, input_tokens=160, output_tokens=0)]
    second = codex_meta("T1", 3) + [codex_count(4, input_tokens=180, output_tokens=0)]
    write_jsonl(a, first)
    ledger.ingest(ALL)
    write_jsonl(b, second)
    ledger.ingest(ALL)
    # Rewrite the older source, including its already counted event and an unseen reset.
    write_jsonl(a, first + [codex_count(5, input_tokens=100, output_tokens=0)])
    a.write_bytes(b"\n" + a.read_bytes())
    ledger.ingest(ALL)
    append_jsonl(b, [codex_count(6, input_tokens=185, output_tokens=0)])
    ledger.ingest(ALL)
    assert summary(ledger)["totals"]["totalTokens"] == 185
    detail = ledger.conn.execute("SELECT thread_id, interval_start, interval_end FROM ledger_gap_details").fetchone()
    assert detail[0] == "T1" and detail[1] <= detail[2]
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='ambiguous-reset'").fetchone()[0] == 1
    gap = next(gap for gap in ledger.coverage()["gaps"] if gap["kind"] == "ambiguous-reset")
    assert gap["threadId"] == "T1"
    assert gap["intervalStart"] <= gap["intervalEnd"]
    ledger.close()
    ledger = Ledger(tmp_path / "data", claude_root=tmp_path / "claude", codex_root=codex, clock=clock_at(3600))
    assert any(gap["kind"] == "ambiguous-reset" and gap["threadId"] == "T1"
               for gap in ledger.coverage()["gaps"])


def test_prefix_verification_resumes_and_detects_middle_edit_with_append(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "s1")
    filler = {"type": "user", "message": {"content": "x" * 70000}}
    lines = [filler, *claude_lines((10, "msg_a", 5)), filler, *claude_lines((20, "msg_b", 7))]
    write_jsonl(path, lines)
    while ledger.ingest(ALL)["pendingBytes"]:
        pass
    assert total_requests(ledger) == 2
    original = path.read_bytes()
    old = b'"output_tokens": 5'
    assert old in original
    path.write_bytes(original.replace(old, b'"output_tokens": 9', 1))
    append_jsonl(path, claude_lines((30, "msg_c", 3)))
    first = ledger.ingest(ALL, budget_bytes=32768)
    assert first["bytesRead"] <= 32768
    assert first["pendingBytes"] > 0
    assert int(store.meta_value(ledger.conn, "verifyingBytes:Claude")) > 0
    assert total_requests(ledger) == 2
    append_jsonl(path, claude_lines((40, "msg_d", 4)))  # change while verification is pending
    for _ in range(20):
        result = ledger.ingest(ALL, budget_bytes=32768)
        if not result["pendingBytes"]:
            break
    assert result["pendingBytes"] == 0
    assert ledger.coverage()["replacements"] == 1
    assert total_requests(ledger) == 4


def test_verification_honors_small_budget_and_restarts_partial_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, "_HASH_CHUNK", 32)
    ledger, claude, codex = open_ledger(tmp_path)
    path = claude_file(claude, "s1")
    write_jsonl(path, claude_lines((10, "msg_a", 5), (20, "msg_b", 7)))
    while ledger.ingest(ALL)["pendingBytes"]:
        pass
    append_jsonl(path, claude_lines((30, "msg_c", 9)))
    first = ledger.ingest(ALL, budget_bytes=3)
    assert first["bytesRead"] <= 3
    cursor = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))
    assert not cursor["state"]["verify"]["boundary"]
    ledger.close()
    ledger = Ledger(tmp_path / "data", claude_root=claude, codex_root=codex, clock=clock_at(3600))
    second = ledger.ingest(ALL, budget_bytes=3)
    assert second["bytesRead"] <= 3
    cursor = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))
    assert not cursor["state"]["verify"]["boundary"]
    for _ in range(1000):
        result = ledger.ingest(ALL, budget_bytes=3)
        assert result["bytesRead"] <= 3
        if not int(store.meta_value(ledger.conn, "verifyingBytes:Claude") or 0):
            break
    else:
        pytest.fail("verification did not finish")
    while ledger.ingest(ALL)["pendingBytes"]:
        pass
    assert total_requests(ledger) == 3


def test_continuous_appends_make_progress_during_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, "_HASH_CHUNK", 32)
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "continuous")
    write_jsonl(path, [claude_response(1, "first", output=1)])
    ledger.ingest(ALL)
    for index in range(30):
        append_jsonl(path, [claude_response(2 + index, f"next-{index}", output=1)])
        ledger.ingest(ALL, budget_bytes=16)
    assert total_requests(ledger) > 1


@pytest.mark.parametrize("remove", [False, True])
def test_lowered_or_removed_confirmed_usage_is_retained_and_reported(tmp_path, remove):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "rewrite-confirmed")
    write_jsonl(path, [claude_response(1, "confirmed", output=9)])
    ledger.ingest(ALL)
    replacement = [] if remove else [claude_response(1, "confirmed", output=5)]
    write_jsonl(path, replacement + [claude_response(2, "new", output=3)])
    for _ in range(100):
        if not ledger.ingest(ALL, budget_bytes=256)["pendingBytes"]:
            break
    else:
        pytest.fail("rewrite replay did not finish")
    assert summary(ledger)["totals"]["outputTotal"] == 12
    assert any(gap["kind"] == "rewrite-lowered-confirmed" for gap in ledger.coverage()["gaps"])


def test_replacement_replay_ends_before_later_codex_reset(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'T1')
    initial = codex_meta('T1', 1) + [codex_count(2, input_tokens=180, output_tokens=0)]
    write_jsonl(path, initial)
    ledger.ingest(ALL)
    path.write_bytes(b'\n' + path.read_bytes())
    ledger.ingest(ALL)
    ledger.close()
    ledger = Ledger(tmp_path / 'data', claude_root=tmp_path / 'claude', codex_root=codex, clock=clock_at(3600))
    append_jsonl(path, [codex_count(3, input_tokens=40, output_tokens=0), codex_count(4, input_tokens=45, output_tokens=0)])
    ledger.ingest(ALL)
    assert summary(ledger)['totals']['totalTokens'] == 225


@pytest.mark.parametrize('valid_first', [True, False])
def test_matching_older_incomplete_copy_resolves_in_both_orders(tmp_path, valid_first):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, 's1')
    valid = claude_response(20, 'same', output=5)
    missing = claude_response(10, 'same', output=0)
    del missing['message']['usage']
    write_jsonl(path, [valid if valid_first else missing])
    ledger.ingest(ALL)
    append_jsonl(path, [missing if valid_first else valid])
    ledger.ingest(ALL)
    assert ledger.conn.execute('SELECT COUNT(*) FROM ledger_omissions').fetchone()[0] == 0


def test_later_incomplete_revision_remains_unresolved(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, 's1')
    missing = claude_response(30, 'same', output=0)
    del missing['message']['usage']
    write_jsonl(path, [missing, claude_response(20, 'same', output=5)])
    ledger.ingest(ALL)
    assert ledger.conn.execute('SELECT COUNT(*) FROM ledger_omissions').fetchone()[0] == 1


def test_replay_endpoint_persists_until_replacement_interval_finishes(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'T1')
    original = codex_meta('T1', 1) + [codex_count(2, input_tokens=180, output_tokens=0)]
    write_jsonl(path, original)
    ledger.ingest(ALL)
    write_jsonl(path, codex_meta('T1', 1) + [codex_count(2, input_tokens=180, output_tokens=0),
                                              codex_count(3, input_tokens=100, output_tokens=0)])
    path.write_bytes(b'\n' + path.read_bytes())
    endpoint = path.stat().st_size
    for _ in range(1000):
        ledger.ingest(ALL, budget_bytes=1)
        cursor = store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(path))
        if cursor['state'].get('replay_end') is not None:
            break
    assert cursor['state']['replay_end'] == endpoint
    ledger.close()
    append_jsonl(path, [codex_count(4, input_tokens=40, output_tokens=0), codex_count(5, input_tokens=45, output_tokens=0)])
    ledger = Ledger(tmp_path / 'data', claude_root=tmp_path / 'claude', codex_root=codex, clock=clock_at(3600))
    for _ in range(2000):
        result = ledger.ingest(ALL, budget_bytes=100)
        if not result['pendingBytes']:
            break
    assert result['pendingBytes'] == 0
    assert summary(ledger)['totals']['totalTokens'] == 225
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='ambiguous-reset'").fetchone()[0] == 1


def test_incomplete_copy_without_timestamp_remains_unresolved(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, 's1')
    missing = claude_response(10, 'same', output=0)
    del missing['message']['usage']
    del missing['timestamp']
    write_jsonl(path, [missing, claude_response(20, 'same', output=5)])
    ledger.ingest(ALL)
    assert ledger.conn.execute('SELECT COUNT(*) FROM ledger_omissions').fetchone()[0] == 1


def _drain_ledger(ledger, budget=4096):
    for _ in range(1000):
        if not ledger.ingest(ALL, budget_bytes=budget)['pendingBytes']:
            return
    pytest.fail('ingestion did not converge')


def _await_provisional(ledger, expected_output):
    for _ in range(100):
        ledger.ingest(ALL, budget_bytes=1024)
        snap = summary(ledger)
        if snap['totals']['outputTotal'] == expected_output:
            assert snap['coverage']['status'] == 'verifying'
            assert snap['coverage']['verifyingBytes'] > 0
            return
    pytest.fail('appended usage was not visible while verifying')


def test_provisional_claude_revision_rolls_back_without_losing_confirmed_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, _ = open_ledger(tmp_path)
    a, b = claude_file(claude, 'a'), claude_file(claude, 'b')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    confirmed = claude_response(1, 'shared', output=5)
    write_jsonl(a, [confirmed, filler])
    write_jsonl(b, [confirmed])
    _drain_ledger(ledger)
    append_jsonl(a, [claude_response(2, 'shared', output=9)])
    _await_provisional(ledger, 9)
    # Remove only the provisional revision; the independently confirmed copy survives.
    write_jsonl(a, [confirmed, filler, claude_response(3, 'new', output=3)])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['outputTotal'] == 8
    assert total_requests(ledger) == 2
    ledger.rebuild_rollups()
    assert summary(ledger)['totals']['outputTotal'] == 8


def test_clean_provisional_promotion_survives_reopen_and_later_rewrite(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, codex = open_ledger(tmp_path)
    path = claude_file(claude, 'promote')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    first = claude_response(1, 'first', output=5)
    later = claude_response(2, 'later', output=9)
    write_jsonl(path, [first, filler])
    _drain_ledger(ledger)
    append_jsonl(path, [later])
    _await_provisional(ledger, 14)
    _drain_ledger(ledger)
    assert summary(ledger)['coverage']['verifyingBytes'] == 0
    ledger.close()
    ledger = Ledger(tmp_path / 'data', claude_root=claude, codex_root=codex, clock=clock_at(3600))
    write_jsonl(path, [first, filler, claude_response(2, 'later', output=3)])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['outputTotal'] == 14
    assert any(g['kind'] == 'rewrite-lowered-confirmed' for g in ledger.coverage()['gaps'])


def test_rewrite_verified_chunk_while_appending_rolls_back_provisional(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, 'moving')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    first = claude_response(1, 'first', output=5)
    write_jsonl(path, [first, filler])
    _drain_ledger(ledger)
    append_jsonl(path, [claude_response(2, 'discard', output=9)])
    _await_provisional(ledger, 14)
    # Rewrite the beginning after verification has already progressed, and keep appending.
    write_jsonl(path, [first, filler, claude_response(3, 'replacement', output=3)])
    append_jsonl(path, [claude_response(4, 'more', output=2)])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['outputTotal'] == 10
    assert total_requests(ledger) == 3


def test_codex_provisional_rollback_preserves_shared_thread_baseline_and_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, _, codex = open_ledger(tmp_path)
    a, b = codex_file(codex, 'a'), codex_file(codex, 'b')
    filler = {'type': 'unknown', 'payload': {'padding': 'x' * 30000}}
    initial = codex_meta('T1', 1, prompt='kind: review') + [codex_count(2, input_tokens=100, output_tokens=0), filler]
    write_jsonl(a, initial)
    _drain_ledger(ledger)
    write_jsonl(b, codex_meta('T1', 3) + [codex_count(4, input_tokens=160, output_tokens=0)])
    _drain_ledger(ledger)
    tag = codex_meta('T1', 5, prompt='kind: build')[-1]
    append_jsonl(a, [tag, codex_count(6, input_tokens=180, output_tokens=0)])
    for _ in range(100):
        ledger.ingest(ALL, budget_bytes=1024)
        snap = summary(ledger)
        if snap['coverage']['status'] == 'verifying':
            # Codex tails wait for prefix verification, so the appended 180 is not counted yet.
            assert snap['totals']['totalTokens'] == 160
            break
    else:
        pytest.fail('Codex append did not reach verification')
    write_jsonl(a, initial + [{'type': 'unknown', 'payload': {'changed': True}}])
    _drain_ledger(ledger)
    append_jsonl(b, [codex_count(7, input_tokens=165, output_tokens=0)])
    _drain_ledger(ledger)
    snap = summary(ledger)
    assert snap['totals']['totalTokens'] == 165
    assert {row['kind'] for row in snap['rows']} == {'review'}


@pytest.mark.parametrize('stage', ['after_facts', 'after_cursor'])
def test_crash_at_provisional_promotion_is_atomic(tmp_path, monkeypatch, stage):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, codex = open_ledger(tmp_path)
    path = claude_file(claude, 'crash-promotion')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    first = claude_response(1, 'first', output=5)
    later = claude_response(2, 'later', output=9)
    write_jsonl(path, [first, filler])
    _drain_ledger(ledger)
    append_jsonl(path, [later])
    _await_provisional(ledger, 14)
    for _ in range(100):
        if summary(ledger)['coverage']['verifyingBytes'] <= 1024:
            break
        ledger.ingest(ALL, budget_bytes=1024)
    else:
        pytest.fail('verification did not approach promotion')

    def crash(name):
        if name == stage:
            raise RuntimeError('promotion crash')

    with pytest.raises(RuntimeError, match='promotion crash'):
        ledger.ingest(ALL, budget_bytes=1024, fault=crash)
    ledger.close()
    ledger = Ledger(tmp_path / 'data', claude_root=claude, codex_root=codex, clock=clock_at(3600))
    assert summary(ledger)['totals']['outputTotal'] == 14
    _drain_ledger(ledger)
    write_jsonl(path, [first, filler, claude_response(2, 'later', output=3)])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['outputTotal'] == 14
    assert any(g['kind'] == 'rewrite-lowered-confirmed' for g in ledger.coverage()['gaps'])


def _omission_count(ledger) -> int:
    return ledger.conn.execute("SELECT COUNT(*) FROM ledger_omissions").fetchone()[0]


def test_codex_tail_waits_for_prefix_verification_then_counts_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'T1')
    filler = {'type': 'unknown', 'payload': {'padding': 'x' * 30000}}
    write_jsonl(path, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0), filler])
    _drain_ledger(ledger)
    append_jsonl(path, [codex_count(3, input_tokens=180, output_tokens=0)])
    for _ in range(200):
        ledger.ingest(ALL, budget_bytes=1024)
        if not int(store.meta_value(ledger.conn, 'verifyingBytes:Codex') or 0):
            break
        assert summary(ledger)['totals']['totalTokens'] == 100
        assert ledger.conn.execute("SELECT COUNT(*) FROM provisional_facts WHERE provider='Codex'").fetchone()[0] == 0
    else:
        pytest.fail('Codex verification did not finish')
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 180


def test_sol_resumed_codex_baseline_survives_verifying_sibling(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, _, codex = open_ledger(tmp_path)
    a, b = codex_file(codex, 'a'), codex_file(codex, 'b')
    filler = {'type': 'unknown', 'payload': {'padding': 'x' * 30000}}
    write_jsonl(a, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0), filler])
    _drain_ledger(ledger)
    append_jsonl(a, [codex_count(3, input_tokens=180, output_tokens=0)])
    ledger.ingest(ALL, budget_bytes=1024)
    assert ledger.coverage()['status'] == 'verifying'
    write_jsonl(b, codex_meta('T1', 4) + [codex_count(5, input_tokens=185, output_tokens=0)])
    ledger._ingestor.ingest_file('Codex', b, None, None)  # resumed file confirms while A verifies
    assert summary(ledger)['totals']['totalTokens'] == 185
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 185
    baseline = store.load_thread(ledger.conn, 'Codex', 'T1')
    assert baseline['counters']['total_tokens'] == 185
    # Design change (F30): A's tail is withheld as a cross-file earlier lower snapshot, and the gap says so.
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='ambiguous-reset'").fetchone()[0] == 1


@pytest.mark.parametrize('vanish', ['delete', 'move'])
def test_unverified_source_gone_resolves_provisional_usage_with_gap(tmp_path, monkeypatch, vanish):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, 'vanished')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    write_jsonl(path, [claude_response(1, 'first', output=5), filler])
    _drain_ledger(ledger)
    append_jsonl(path, [claude_response(2, 'later', output=9)])
    _await_provisional(ledger, 14)
    if vanish == 'delete':
        path.unlink()
    else:
        path.rename(path.parent / (path.name + '.bak'))  # still on disk, no longer discovered
    _drain_ledger(ledger)
    snap = summary(ledger)
    assert snap['totals']['outputTotal'] == 5
    assert ledger.conn.execute('SELECT COUNT(*) FROM provisional_facts').fetchone()[0] == 0
    assert ledger.conn.execute("SELECT value FROM counters WHERE name='missingUnverified'").fetchone()[0] == 1
    coverage = ledger.coverage()
    assert any(gap['kind'] == 'source-missing-unverified' for gap in coverage['gaps'])
    assert coverage['verifyingBytes'] == 0
    assert coverage['status'] != 'verifying'


def test_provisional_copy_rollback_keeps_other_sources_omission(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, _ = open_ledger(tmp_path)
    a, b = claude_file(claude, 'a'), claude_file(claude, 'b')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    missing = claude_response(10, 'm', output=0)
    del missing['message']['usage']
    write_jsonl(b, [missing])
    write_jsonl(a, [filler])
    _drain_ledger(ledger)
    assert _omission_count(ledger) == 1
    append_jsonl(a, [claude_response(20, 'm', output=5)])
    for _ in range(100):
        ledger.ingest(ALL, budget_bytes=1024)
        assert _omission_count(ledger) == 1
        if ledger.conn.execute("SELECT state FROM sources WHERE path_key=?", (ledger_ingest.path_key(a),)).fetchone() \
                and store.load_source(ledger.conn, 'Claude', ledger_ingest.path_key(a))['state'].get('provisional'):
            break
    else:
        pytest.fail('provisional copy was not staged')
    write_jsonl(a, [filler])  # rewrite removes the provisional copy
    for _ in range(200):
        result = ledger.ingest(ALL, budget_bytes=1024)
        assert _omission_count(ledger) == 1
        if not result['pendingBytes']:
            break
    assert _omission_count(ledger) == 1


def test_provisional_copy_verifying_clears_other_sources_omission(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, _ = open_ledger(tmp_path)
    a, b = claude_file(claude, 'a'), claude_file(claude, 'b')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    missing = claude_response(10, 'm', output=0)
    del missing['message']['usage']
    write_jsonl(b, [missing])
    write_jsonl(a, [filler])
    _drain_ledger(ledger)
    assert _omission_count(ledger) == 1
    append_jsonl(a, [claude_response(20, 'm', output=5)])
    _drain_ledger(ledger)
    assert _omission_count(ledger) == 0
    assert ledger.conn.execute("SELECT COUNT(*) FROM ledger_message_evidence").fetchone()[0] == 1


def test_codex_genuine_reset_with_later_epoch_still_counts(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    a, b = codex_file(codex, 'a'), codex_file(codex, 'b')
    write_jsonl(a, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0)])
    _drain_ledger(ledger)
    write_jsonl(b, codex_meta('T1', 3) + [codex_count(6, input_tokens=40, output_tokens=0)])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 140


def test_codex_stale_lower_snapshot_without_baseline_epoch_keeps_current_behaviour(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    a, b = codex_file(codex, 'a'), codex_file(codex, 'b')
    write_jsonl(a, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0)])
    _drain_ledger(ledger)
    row = store.load_thread(ledger.conn, 'Codex', 'T1')
    row['counter_epoch'] = None
    with store.transaction(ledger.conn) as conn:
        store.save_thread(conn, 'Codex', 'T1', row)
    write_jsonl(b, codex_meta('T1', 1) + [codex_count(1, input_tokens=50, output_tokens=0)])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 150


def test_same_source_reset_after_clock_correction_still_counts(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    write_jsonl(codex_file(codex, 'a'), codex_meta('T1', 1) + [
        codex_count(100, input_tokens=100, output_tokens=0),
        codex_count(90, input_tokens=40, output_tokens=0),
        codex_count(91, input_tokens=80, output_tokens=0),
        codex_count(101, input_tokens=120, output_tokens=0)])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 220


def test_discarded_provisional_source_is_replayed_when_it_reappears(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, 'reappears')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    write_jsonl(path, [claude_response(1, 'first', output=5), filler])
    _drain_ledger(ledger)
    append_jsonl(path, [claude_response(2, 'later', output=9)])
    _await_provisional(ledger, 14)
    hidden = path.with_name(path.name + '.bak')
    path.rename(hidden)
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['outputTotal'] == 5
    hidden.rename(path)
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['outputTotal'] == 14
    assert total_requests(ledger) == 2
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='source-missing-unverified'").fetchone()[0] == 1


def test_interrupted_run_with_staged_usage_reports_verifying(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, 'interrupted')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    write_jsonl(path, [claude_response(1, 'first', output=5), filler])
    _drain_ledger(ledger)
    assert summary(ledger)['coverage']['status'] == 'complete'
    append_jsonl(path, [claude_response(2, 'later', output=9)])

    def interrupted(*args, **kwargs):
        raise RuntimeError('interrupted before the run transaction')

    monkeypatch.setattr(ledger._ingestor, '_pending', interrupted)
    for _ in range(100):
        try:
            ledger.ingest(ALL, budget_bytes=1024)
        except RuntimeError:
            pass
        if ledger.conn.execute('SELECT COUNT(*) FROM provisional_facts').fetchone()[0]:
            break
    else:
        pytest.fail('usage was not staged before the interruption')
    assert summary(ledger)['coverage']['status'] == 'verifying'


def test_codex_verification_finishes_while_file_keeps_growing(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'T1')
    filler = {'type': 'unknown', 'payload': {'padding': 'x' * 30000}}
    write_jsonl(path, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0), filler])
    _drain_ledger(ledger)
    for index in range(30):
        append_jsonl(path, [codex_count(3 + index, input_tokens=101 + index, output_tokens=0)])
        ledger.ingest(ALL, budget_bytes=1024)
    for _ in range(2000):
        if not ledger.ingest(ALL, budget_bytes=1024)['pendingBytes']:
            break
    else:
        pytest.fail('growing file did not converge')
    assert summary(ledger)['totals']['totalTokens'] == 130


def test_codex_tail_progresses_while_file_keeps_growing(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'T1')
    filler = {'type': 'unknown', 'payload': {'padding': 'x' * 30000}}
    write_jsonl(path, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0), filler])
    _drain_ledger(ledger)
    for index in range(100):
        append_jsonl(path, [codex_count(3 + index, input_tokens=101 + index, output_tokens=0)])
        ledger.ingest(ALL, budget_bytes=1024)
    # Each record adds one token. Two tail reads each confirm at least one record, so >= 102 is the floor.
    assert summary(ledger)['totals']['totalTokens'] >= 102


def test_source_below_verify_restart_cap_never_exceeds_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'T1')
    filler = {'type': 'unknown', 'payload': {'padding': 'x' * 30000}}
    write_jsonl(path, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0), filler])
    _drain_ledger(ledger)
    for index in range(ledger_ingest._VERIFY_RESTART_LIMIT - 1):
        append_jsonl(path, [codex_count(3 + index, input_tokens=101 + index, output_tokens=0)])
        result = ledger.ingest(ALL, budget_bytes=1024)
        assert result['bytesRead'] <= 1024


def test_same_source_clock_correction_across_passes_still_counts(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'a')
    write_jsonl(path, codex_meta('T1', 1) + [codex_count(100, input_tokens=100, output_tokens=0)])
    _drain_ledger(ledger)
    append_jsonl(path, [codex_count(90, input_tokens=40, output_tokens=0),
                        codex_count(91, input_tokens=80, output_tokens=0),
                        codex_count(101, input_tokens=120, output_tokens=0)])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 220
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='ambiguous-reset'").fetchone()[0] == 0


def test_sol_cross_file_clock_correction_is_withheld_with_gap(tmp_path):
    # Sol F30: A establishes 100@t100. Resumed B then reports 40@t90 and 80@t91 (earlier, lower, cross-file),
    # and 120@t101. The two earlier lower events are withheld, because a clock correction and a reset look
    # the same from timestamps alone. The baseline stays at 100, so 120@t101 adds 20: total 120.
    ledger, _, codex = open_ledger(tmp_path)
    write_jsonl(codex_file(codex, 'a'), codex_meta('T1', 1) + [codex_count(100, input_tokens=100, output_tokens=0)])
    ledger.ingest(ALL)
    write_jsonl(codex_file(codex, 'b'), codex_meta('T1', 3) + [
        codex_count(90, input_tokens=40, output_tokens=0),
        codex_count(91, input_tokens=80, output_tokens=0),
        codex_count(101, input_tokens=120, output_tokens=0)])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 120
    gaps = ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='ambiguous-reset'").fetchone()[0]
    assert gaps == 1  # one per thread per pass, not one per withheld event
    gap = next(gap for gap in ledger.coverage()['gaps'] if gap['kind'] == 'ambiguous-reset')
    assert gap['threadId'] == 'T1' and gap['intervalStart'] <= gap['intervalEnd']


def test_metadata_only_stamp_churn_stays_within_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'T1')
    filler = {'type': 'unknown', 'payload': {'padding': 'x' * 30000}}
    write_jsonl(path, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0), filler])
    _drain_ledger(ledger)
    base = path.stat().st_mtime_ns
    for index in range(20):
        os.utime(path, ns=(base + (index + 1) * 10**9, base + (index + 1) * 10**9))  # stamp changes, size does not
        result = ledger.ingest(ALL, budget_bytes=1024)
        assert result['bytesRead'] <= 1024
    cursor = store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(path))
    assert cursor['state']['verify']['restarts'] == 0
