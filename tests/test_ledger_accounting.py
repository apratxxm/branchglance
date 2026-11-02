"""Accounting core: normalization, revisions, copies, Codex counters and pricing."""

from __future__ import annotations

import pytest

from test_ledger_support import (
    DAY_END, HOUR_START, append_jsonl, claude_file, claude_response, clock_at, codex_count, codex_file,
    codex_meta, ledger_guard, make_roots, stamp, write_jsonl,
)

from token_tracker import providers
from token_tracker.ledger import Ledger
from token_tracker import ledger_model

ALL = {"Claude": True, "Codex": True}


def open_ledger(tmp_path, at: int = 3600):
    data, claude, codex = make_roots(tmp_path)
    return Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(at)), claude, codex


def rows_by_key(ledger):
    return {(r["provider"], r["modelId"], r["kind"]): r
            for r in ledger.summary(HOUR_START, DAY_END, show=ALL)["rows"]}


def total_requests(ledger) -> int:
    return sum(r["requests"] for r in ledger.summary(HOUR_START, DAY_END, show=ALL)["rows"])


def total_tokens(ledger) -> int:
    return ledger.summary(HOUR_START, DAY_END, show=ALL)["totals"]["totalTokens"]


CLAUDE_LEAD = ("Claude", "claude-haiku-5-5", "lead")


def test_partial_cache_write_breakdown_keeps_cost_incomplete(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "partial-tier")
    tiered = claude_response(10, "response", output=5,
                             breakdown={"ephemeral_5m_input_tokens": 20, "ephemeral_1h_input_tokens": 0})
    unsplit = claude_response(11, "response", output=5, creation=40)
    write_jsonl(path, [tiered, unsplit])
    ledger.ingest(ALL)
    row = rows_by_key(ledger)[CLAUDE_LEAD]
    assert row["cacheWrite5m"] == 20
    assert row["cacheWriteUnknown"] == 20
    assert row["costComplete"] is False


def test_claude_revision_counts_final_output(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [claude_response(10, "msg_1", output=5),
                                            claude_response(12, "msg_1", output=20)])
    ledger.ingest(ALL)
    row = rows_by_key(ledger)[CLAUDE_LEAD]
    assert row["outputTotal"] == 20
    assert row["requests"] == 1


def test_copied_response_in_two_files_counts_once(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    copy = claude_response(10, "msg_1", output=20)
    write_jsonl(claude_file(claude, "s1"), [copy])
    write_jsonl(claude_file(claude, "s2"), [copy])
    ledger.ingest(ALL)
    row = rows_by_key(ledger)[CLAUDE_LEAD]
    assert row["requests"] == 1
    assert row["outputTotal"] == 20


def test_claude_cache_tiers_and_cost_match_reducer(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [claude_response(
        10, "msg_c", output=100, input_tokens=300, cache_read=2000,
        breakdown={"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 500})])
    ledger.ingest(ALL)
    row = rows_by_key(ledger)[CLAUDE_LEAD]
    assert row["inputTotal"] == 3800
    assert row["cachedRead"] == 2000
    assert row["cacheWrite5m"] == 1000
    assert row["cacheWrite1h"] == 500
    assert row["cacheWriteUnknown"] == 0
    assert row["outputTotal"] == 100
    assert row["totalTokens"] == 3900
    assert row["costComplete"] is True
    expected = providers._costs(
        {"total_tokens": 3900},
        {"claude-haiku-5-5": {"input_tokens": 300, "cached_tokens": 2000, "cache_5m_tokens": 1000,
                              "cache_1h_tokens": 500, "output_tokens": 100, "total_tokens": 3900}},
        providers.CLAUDE_PRICING, claude=True)["apiEquivalentCost"]
    assert row["knownCostUsd"] == pytest.approx(expected, abs=1e-4)


def test_unknown_cache_tier_is_null_not_zero(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [claude_response(10, "msg_u", output=5, creation=40)])
    ledger.ingest(ALL)
    row = rows_by_key(ledger)[CLAUDE_LEAD]
    assert row["cacheWrite5m"] is None
    assert row["cacheWriteUnknown"] == 40
    assert row["costComplete"] is False


def test_claude_revision_reconciles_unsplit_cache_writes(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "s1")
    unsplit = claude_response(10, "msg_u", output=5, creation=40)
    split = claude_response(12, "msg_u", output=5,
                            breakdown={"ephemeral_5m_input_tokens": 40, "ephemeral_1h_input_tokens": 0})
    write_jsonl(path, [unsplit])
    ledger.ingest(ALL)
    assert total_tokens(ledger) == 55
    append_jsonl(path, [split, unsplit])  # A later copied older revision must not restore the overlap.
    ledger.ingest(ALL)
    row = rows_by_key(ledger)[CLAUDE_LEAD]
    assert row["requests"] == 1
    assert row["inputTotal"] == 50
    assert row["totalTokens"] == 55
    assert row["cacheWrite5m"] == 40
    assert row["cacheWrite1h"] == 0
    assert row["cacheWriteUnknown"] == 0
    assert row["costComplete"] is True


def test_unpriced_model_is_reported_not_zeroed(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [claude_response(10, "msg_x", output=50, model="claude-mystery-1",
                                                           cache_read=0, creation=0)])
    ledger.ingest(ALL)
    row = rows_by_key(ledger)[("Claude", "claude-mystery-1", "lead")]
    assert row["knownCostUsd"] is None
    assert row["unpricedTokens"] == row["totalTokens"] > 0
    assert row["costComplete"] is False


def test_api_ratio_needs_a_plan_fee(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [claude_response(
        10, "msg_c", output=100, input_tokens=300, cache_read=2000,
        breakdown={"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 500})])
    ledger.ingest(ALL)
    without = ledger.summary(HOUR_START, DAY_END, show=ALL)
    assert without["apiRatio"]["value"] is None
    assert without["planConsumedPercent"] is None
    with_fee = ledger.summary(HOUR_START, DAY_END, show=ALL, plan_fee_usd=0.0001)
    assert with_fee["apiRatio"]["value"] == pytest.approx(3.25, abs=0.01)


def test_codex_resume_one_thread_counts_counter_advance(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    first = codex_meta("T1", 10) + [codex_count(11, input_tokens=80, output_tokens=20, total=100, last={
        "input_tokens": 80, "output_tokens": 20, "total_tokens": 100})]
    second = codex_meta("T1", 100) + [codex_count(101, input_tokens=120, output_tokens=40, total=160, last={
        "input_tokens": 15, "output_tokens": 5, "total_tokens": 20})]
    write_jsonl(codex_file(codex, "T1-a"), first)
    write_jsonl(codex_file(codex, "T1-b"), second)
    ledger.ingest(ALL)
    assert total_tokens(ledger) == 160  # 100 + (160 - 100), not last=20 and not a double count
    assert total_requests(ledger) == 2


def test_codex_equal_counter_duplicate_gives_zero(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    last = {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100}
    write_jsonl(codex_file(codex, "T2"), codex_meta("T2", 10) + [
        codex_count(11, input_tokens=80, output_tokens=20, total=100, last=last),
        codex_count(12, input_tokens=80, output_tokens=20, total=100, last=last)])
    ledger.ingest(ALL)
    assert total_tokens(ledger) == 100
    assert total_requests(ledger) == 1


def test_codex_reset_and_compaction(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    write_jsonl(codex_file(codex, "T3"), codex_meta("T3", 10) + [
        codex_count(11, input_tokens=80, output_tokens=20, total=100,
                    last={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100}),
        {"type": "compacted", "timestamp": stamp(12), "payload": {}},
        # Counter rewritten lower: the request's own usage is the new usage.
        codex_count(13, input_tokens=30, output_tokens=10, total=40,
                    last={"input_tokens": 30, "output_tokens": 10, "total_tokens": 40})])
    ledger.ingest(ALL)
    assert total_tokens(ledger) == 140


def test_codex_cached_and_reasoning_total(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    last = {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20,
            "reasoning_output_tokens": 5, "total_tokens": 120}
    write_jsonl(codex_file(codex, "T4"), codex_meta("T4", 10) + [
        codex_count(11, input_tokens=100, cached=80, output_tokens=20, reasoning=5, total=120, last=last)])
    ledger.ingest(ALL)
    row = rows_by_key(ledger)[("Codex", "gpt-6.1-terra", "lead")]
    assert row["inputTotal"] == 100
    assert row["cachedRead"] == 80
    assert row["outputTotal"] == 20
    assert row["reasoningOutput"] == 5
    assert row["totalTokens"] == 120


def test_codex_normalization_helper_matches_fixture():
    fields = ledger_model.codex_usage(
        {"input_tokens": 100, "cached_input_tokens": 80, "cache_write_input_tokens": 0,
         "output_tokens": 20, "reasoning_output_tokens": 5, "total_tokens": 120},
        frozenset({"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"}))
    assert fields["input_total"] == 100 and fields["total"] == 120
    assert fields["cache_write_unknown"] is None

def test_recovery_gap_survives_reopen_without_sources(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    data.mkdir(parents=True, exist_ok=True)
    (data / "work-ledger.sqlite3").write_bytes(b"not a sqlite database")
    from token_tracker import ledger_store as store
    # Use the actual configured database name if it differs.
    bad = data / "work-ledger.sqlite3"
    if bad.name != store.DB_NAME:
        bad.rename(data / store.DB_NAME)
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    assert ledger.coverage()["status"] == "partial"
    assert ledger.coverage()["gaps"][0]["kind"] == "corruption-recovery"
    ledger.close()
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    assert ledger.coverage()["status"] == "partial"
    assert ledger.coverage()["gaps"][0]["kind"] == "corruption-recovery"
    ledger.close()


def test_recovery_intent_survives_failure_before_gap_commit(tmp_path, monkeypatch):
    from token_tracker import ledger_store as store
    data, claude, codex = make_roots(tmp_path)
    data.mkdir(parents=True, exist_ok=True)
    (data / store.DB_NAME).write_bytes(b"not a database")
    add_gap = store.add_gap
    with monkeypatch.context() as patch:
        def interrupted(*args, **kwargs):
            raise RuntimeError("crash before gap commit")
        patch.setattr(store, "add_gap", interrupted)
        with pytest.raises(RuntimeError, match="crash before gap commit"):
            Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    assert store.add_gap is add_gap
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    assert ledger.coverage()["status"] == "partial"
    assert [g["kind"] for g in ledger.coverage()["gaps"]] == ["corruption-recovery"]
    ledger.close()


def test_recovery_intent_precedes_quarantine_and_is_reconciled(tmp_path, monkeypatch):
    from token_tracker import ledger_store as store
    data, claude, codex = make_roots(tmp_path)
    data.mkdir(parents=True, exist_ok=True)
    (data / store.DB_NAME).write_bytes(b"not a database")
    with monkeypatch.context() as patch:
        def crash(*args):
            raise RuntimeError("crash before quarantine")
        patch.setattr(Ledger, "_quarantine", crash)
        with pytest.raises(RuntimeError, match="before quarantine"):
            Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    assert (data / (store.DB_NAME + ".recovery-intent")).exists()
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    assert ledger.coverage()["gapCount"] == 1
    ledger.close()


def test_recovery_intent_after_gap_commit_does_not_duplicate_gap(tmp_path, monkeypatch):
    from token_tracker import ledger_store as store
    data, claude, codex = make_roots(tmp_path)
    data.mkdir(parents=True, exist_ok=True)
    (data / store.DB_NAME).write_bytes(b"not a database")
    with monkeypatch.context() as patch:
        def crash(*args):
            raise RuntimeError("crash before intent cleanup")
        patch.setattr(Ledger, "_clear_recovery_intent", crash)
        with pytest.raises(RuntimeError, match="before intent cleanup"):
            Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    assert ledger.coverage()["gapCount"] == 1
    assert ledger.coverage()["status"] == "partial"
    ledger.close()


def test_show_policy_hides_other_provider_coverage(tmp_path):
    from token_tracker import ledger_store as store
    ledger, _, _ = open_ledger(tmp_path)
    with store.transaction(ledger.conn) as conn:
        store.add_gap(conn, "Claude", "truncation", 100, 10)
        store.bump_counter(conn, "Claude", "readFailures")
        store.meta_set(conn, "pendingBytes:Claude", 42)
        store.meta_set(conn, "observedFrom:Claude", 100)
        store.meta_set(conn, "collected", '["Claude"]')
    coverage = ledger.summary(HOUR_START, DAY_END, show={"Codex": True, "Claude": False})["coverage"]
    assert coverage["gaps"] == []
    assert coverage["gapCount"] == coverage["readFailures"] == coverage["pendingBytes"] == 0
    assert coverage["observedFrom"] is None
    assert "Claude" not in coverage["providersNotCollected"]
    ledger.close()


def test_missing_usage_resolves_only_for_matching_message_id(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "omissions")
    first = claude_response(10, "one", output=1)
    second = claude_response(10, "two", output=1)
    del first["message"]["usage"]
    del second["message"]["usage"]
    write_jsonl(path, [first, second])
    ledger.ingest(ALL)
    assert ledger.coverage()["status"] == "partial"
    append_jsonl(path, [claude_response(20, "one", output=4)])
    ledger.ingest(ALL)
    assert ledger.coverage()["status"] == "partial"
    append_jsonl(path, [claude_response(30, "two", output=7)])
    ledger.ingest(ALL)
    assert ledger.coverage()["status"] == "complete"


def test_anonymous_missing_usage_is_not_resolved_by_matching_timestamp(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "anonymous")
    missing = claude_response(10, "missing", output=1)
    del missing["message"]["id"]
    del missing["message"]["usage"]
    unrelated = claude_response(10, "unrelated", output=5)
    del unrelated["message"]["id"]
    write_jsonl(path, [missing, unrelated])
    ledger.ingest(ALL)
    assert ledger.coverage()["status"] == "partial"


def test_malformed_record_remains_partial_until_source_replacement(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "malformed")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{malformed}\n', encoding="utf-8")
    ledger.ingest(ALL)
    assert ledger.coverage()["status"] == "partial"
    write_jsonl(path, [claude_response(20, "corrected", output=7)])
    ledger.ingest(ALL)
    # The malformed omission is resolved, but the replacement gap is retained.
    coverage = ledger.coverage()
    assert coverage["status"] == "partial"
    assert coverage["gaps"][0]["kind"] in {"replacement", "truncation"}
    assert ledger.conn.execute("SELECT COUNT(*) FROM ledger_omissions").fetchone()[0] == 0


def test_provider_purge_removes_gap_details_and_unknown_kind_counts(tmp_path):
    from token_tracker import ledger_store as store
    ledger, _, _ = open_ledger(tmp_path)
    with store.transaction(ledger.conn) as conn:
        store.add_gap(conn, "Claude", "ambiguous-reset", 100, 0)
        gap_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO ledger_gap_details VALUES(?, ?, ?, ?)", (gap_id, "private-thread", 100, 200))
        conn.execute("INSERT INTO ledger_message_evidence VALUES(?, ?, ?)", ("Claude", "message", 100))
    ledger.purge("Claude")
    assert ledger.conn.execute("SELECT COUNT(*) FROM ledger_gap_details").fetchone()[0] == 0
    assert ledger.conn.execute("SELECT COUNT(*) FROM ledger_message_evidence").fetchone()[0] == 0


def test_unvisited_changed_source_keeps_coverage_verifying(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    first, second = claude_file(claude, "a"), claude_file(claude, "b")
    write_jsonl(first, [claude_response(10, "a", output=5)])
    write_jsonl(second, [claude_response(10, "b", output=5)])
    ledger.ingest(ALL)
    append_jsonl(first, [claude_response(20, "c", output=5)])
    write_jsonl(second, [claude_response(10, "b", output=9)])
    ledger.ingest(ALL, budget_bytes=1)
    assert ledger.coverage()["status"] == "verifying"
    assert ledger.coverage()["verifyingBytes"] > 0


def test_same_size_middle_rewrite_replays_response_revision(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "middle")
    filler = {"type": "user", "message": {"content": "x" * 1000}}
    write_jsonl(path, [filler, claude_response(10, "one", output=5), filler])
    ledger.ingest(ALL)
    original = path.read_bytes()
    changed = original.replace(b'"output_tokens": 5', b'"output_tokens": 9')
    assert len(changed) == len(original) and changed[:512] == original[:512]
    path.write_bytes(changed)
    while ledger.ingest(ALL, budget_bytes=256)["pendingBytes"]:
        pass
    assert ledger.summary(HOUR_START, DAY_END, show=ALL)["totals"]["outputTotal"] == 9
    assert ledger.coverage()["replacements"] == 1


def test_provisional_revision_rolls_back_when_tail_is_rewritten(tmp_path, monkeypatch):
    from token_tracker import ledger_ingest
    monkeypatch.setattr(ledger_ingest, "_HASH_CHUNK", 32)
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "provisional-revision")
    filler = {"type": "user", "message": {"content": "x" * 4096}}
    write_jsonl(path, [filler, claude_response(1, "same", output=5)])
    ledger.ingest(ALL)
    append_jsonl(path, [claude_response(2, "same", output=9)])
    for _ in range(20):
        ledger.ingest(ALL, budget_bytes=16)
        if rows_by_key(ledger)[CLAUDE_LEAD]["outputTotal"] == 9:
            break
    assert rows_by_key(ledger)[CLAUDE_LEAD]["outputTotal"] == 9
    assert ledger.coverage()["status"] == "verifying"
    old = path.read_bytes()
    path.write_bytes(old.replace(b'"output_tokens": 9', b'"output_tokens": 3'))
    for _ in range(500):
        if not ledger.ingest(ALL, budget_bytes=32)["pendingBytes"]:
            break
    else:
        pytest.fail("rewrite replay did not finish")
    assert rows_by_key(ledger)[CLAUDE_LEAD]["outputTotal"] == 5
    assert ledger.coverage()["replacements"] == 1


def test_provisional_fact_and_verifying_coverage_survive_restart(tmp_path, monkeypatch):
    from token_tracker import ledger_ingest
    monkeypatch.setattr(ledger_ingest, "_HASH_CHUNK", 32)
    ledger, claude, codex = open_ledger(tmp_path)
    path = claude_file(claude, "provisional-restart")
    filler = {"type": "user", "message": {"content": "x" * 4096}}
    write_jsonl(path, [filler, claude_response(1, "first", output=5)])
    ledger.ingest(ALL)
    append_jsonl(path, [claude_response(2, "next", output=9)])
    for _ in range(20):
        ledger.ingest(ALL, budget_bytes=16)
        if total_requests(ledger) == 2:
            break
    assert total_requests(ledger) == 2
    assert ledger.coverage()["status"] == "verifying"
    ledger.close()
    ledger = Ledger(tmp_path / "data", claude_root=claude, codex_root=codex, clock=clock_at(3600))
    assert total_requests(ledger) == 2
    assert ledger.coverage()["status"] == "verifying"
    for _ in range(1000):
        if not ledger.ingest(ALL, budget_bytes=128)["pendingBytes"]:
            break
    else:
        pytest.fail("provisional promotion did not finish")
    assert total_requests(ledger) == 2
    assert ledger.coverage()["status"] == "complete"


def _staging_fact(output):
    return {"provider": "Claude", "fact_key": "response", "thread_id": "thread", "model": "model",
            "price_tier": "standard", "hour": 0, "kind": "lead", "rule": "default", "slug": None,
            "classification_version": 1, "segment_turn": None, "merge": True,
            "fields": {"input_total": 0, "output_total": output, "total": output,
                       "cached_read": None, "cache_write_5m": None, "cache_write_1h": None,
                       "cache_write_unknown": None, "reasoning_output": None}}


def test_staged_revision_discard_and_promotion():
    import sqlite3
    from token_tracker import ledger_store as store

    conn = sqlite3.connect(":memory:", isolation_level=None)
    store.migrate(conn)
    with store.transaction(conn):
        store.upsert_fact(conn, _staging_fact(5), merge=True, overrides={})
        store.stage_observation(conn, "source", 1, 1, _staging_fact(9))
    assert store.summary_rows(conn, 0, 3600)[0]["total"] == 9
    assert conn.execute("SELECT total FROM facts").fetchone()[0] == 5
    with store.transaction(conn):
        store.discard_epoch(conn, "Claude", "source", 1, 1)
    assert store.summary_rows(conn, 0, 3600)[0]["total"] == 5
    with store.transaction(conn):
        store.stage_observation(conn, "source", 1, 2, _staging_fact(9))
        assert store.promote_epoch(conn, "Claude", "source", 1, 2)
    assert store.summary_rows(conn, 0, 3600)[0]["total"] == 9
    assert conn.execute("SELECT total FROM facts").fetchone()[0] == 9
    conn.close()


def test_staged_duplicate_support_and_transaction_rollback():
    import sqlite3
    from token_tracker import ledger_store as store

    conn = sqlite3.connect(":memory:", isolation_level=None)
    store.migrate(conn)
    with store.transaction(conn):
        store.stage_observation(conn, "source-a", 1, 1, _staging_fact(9))
        store.stage_observation(conn, "source-b", 1, 1, _staging_fact(9))
        store.discard_epoch(conn, "Claude", "source-a", 1, 1)
    row = store.summary_rows(conn, 0, 3600)[0]
    assert (row["requests"], row["total"]) == (1, 9)
    with pytest.raises(RuntimeError), store.transaction(conn):
        store.promote_epoch(conn, "Claude", "source-b", 1, 1)
        raise RuntimeError("rollback")
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    assert store.summary_rows(conn, 0, 3600)[0]["total"] == 9
    with store.transaction(conn):
        store.purge_provider(conn, "Claude")
    assert store.summary_rows(conn, 0, 3600) == []
    conn.close()
