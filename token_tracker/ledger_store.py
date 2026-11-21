"""SQLite store for the work ledger (stdlib sqlite3, WAL).

One writer (the widget process) runs each ingest pass in BEGIN IMMEDIATE. Facts,
dedup keys, thread counters, gaps and the source cursor commit together, so a crash
applies all of them or none. Readers take WAL snapshots and never block the writer.

Rollups (hourly, UTC epoch hour) are derived from facts and can be rebuilt at any time.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from . import ledger_model as model_mod

DB_NAME = "ledger.sqlite3"

# Value columns carried by hourly rollups. Missing-count columns mark nullable breakdowns.
VALUE_COLUMNS = ("input_total", "output_total", "total", "cached_read", "cache_write_5m", "cache_write_1h",
                 "cache_write_unknown", "reasoning_output", "miss_cached", "miss_w5", "miss_w1", "miss_wu",
                 "miss_reasoning")
# nullable field -> (value column, missing-count column)
NULLABLE_COLUMNS = {
    "cached_read": ("cached_read", "miss_cached"),
    "cache_write_5m": ("cache_write_5m", "miss_w5"),
    "cache_write_1h": ("cache_write_1h", "miss_w1"),
    "cache_write_unknown": ("cache_write_unknown", "miss_wu"),
    "reasoning_output": ("reasoning_output", "miss_reasoning"),
}
FACT_COLUMNS = ("input_total", "output_total", "total", "cached_read", "cache_write_5m", "cache_write_1h",
                "cache_write_unknown", "reasoning_output")

MIGRATIONS_META_TABLE = "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
_SCHEMA_V1 = (
    MIGRATIONS_META_TABLE,
    """CREATE TABLE IF NOT EXISTS sources(
        provider TEXT NOT NULL, path_key TEXT NOT NULL, generation INTEGER NOT NULL,
        byte_offset INTEGER NOT NULL, head_key TEXT, state TEXT NOT NULL,
        PRIMARY KEY(provider, path_key))""",
    """CREATE TABLE IF NOT EXISTS threads(
        provider TEXT NOT NULL, thread_id TEXT NOT NULL, counters TEXT NOT NULL,
        PRIMARY KEY(provider, thread_id))""",
    """CREATE TABLE IF NOT EXISTS facts(
        provider TEXT NOT NULL, fact_key TEXT NOT NULL, thread_id TEXT NOT NULL, model TEXT NOT NULL,
        price_tier TEXT NOT NULL, hour INTEGER NOT NULL, kind TEXT NOT NULL, rule TEXT NOT NULL,
        slug TEXT, classification_version INTEGER NOT NULL, segment_turn TEXT,
        input_total INTEGER NOT NULL, output_total INTEGER NOT NULL, total INTEGER NOT NULL,
        cached_read INTEGER, cache_write_5m INTEGER, cache_write_1h INTEGER, cache_write_unknown INTEGER,
        reasoning_output INTEGER, revisions INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(provider, fact_key))""",
    "CREATE INDEX IF NOT EXISTS facts_hour ON facts(hour)",
    """CREATE TABLE IF NOT EXISTS hourly(
        hour INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, price_tier TEXT NOT NULL,
        kind TEXT NOT NULL, rule TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
        input_total INTEGER NOT NULL DEFAULT 0, output_total INTEGER NOT NULL DEFAULT 0,
        total INTEGER NOT NULL DEFAULT 0, cached_read INTEGER NOT NULL DEFAULT 0,
        cache_write_5m INTEGER NOT NULL DEFAULT 0, cache_write_1h INTEGER NOT NULL DEFAULT 0,
        cache_write_unknown INTEGER NOT NULL DEFAULT 0, reasoning_output INTEGER NOT NULL DEFAULT 0,
        miss_cached INTEGER NOT NULL DEFAULT 0, miss_w5 INTEGER NOT NULL DEFAULT 0,
        miss_w1 INTEGER NOT NULL DEFAULT 0, miss_wu INTEGER NOT NULL DEFAULT 0,
        miss_reasoning INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(hour, provider, model, price_tier, kind, rule))""",
    "CREATE TABLE IF NOT EXISTS overrides(provider TEXT NOT NULL, thread_id TEXT NOT NULL, kind TEXT NOT NULL, "
    "PRIMARY KEY(provider, thread_id))",
    "CREATE TABLE IF NOT EXISTS gaps(id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, "
    "kind TEXT NOT NULL, at_epoch INTEGER NOT NULL, bytes_before INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS counters(provider TEXT NOT NULL, name TEXT NOT NULL, value INTEGER NOT NULL, "
    "PRIMARY KEY(provider, name))",
)

# Schema migrations, keyed by the version they produce. Add a new key to migrate forward.
_SCHEMA_V2 = (
    """CREATE TABLE IF NOT EXISTS provisional_facts(
        provider TEXT NOT NULL, source_key TEXT NOT NULL, generation INTEGER NOT NULL,
        epoch INTEGER NOT NULL, fact_key TEXT NOT NULL, observation TEXT NOT NULL,
        PRIMARY KEY(provider, source_key, generation, epoch, fact_key))""",
    "CREATE INDEX IF NOT EXISTS provisional_facts_key ON provisional_facts(provider, fact_key)",
    # Which confirmed facts each source file contributed, with the largest total it reported in its
    # current generation and in the previous one. A rewrite audit reads this to find lowered or removed usage.
    """CREATE TABLE IF NOT EXISTS source_facts(
        provider TEXT NOT NULL, source_key TEXT NOT NULL, fact_key TEXT NOT NULL, generation INTEGER NOT NULL,
        total INTEGER, prev_total INTEGER,
        PRIMARY KEY(provider, source_key, fact_key))""",
)
def _clear_codex_ledger(conn: sqlite3.Connection) -> None:
    """Schema 3: Codex copied-history attribution was wrong, so stored Codex data is cleared and re-ingested.

    User kind overrides are decisions, not derived data, so they survive the clear.
    """
    overrides = conn.execute("SELECT provider, thread_id, kind FROM overrides WHERE provider='Codex'").fetchall()
    purge_provider(conn, "Codex")
    conn.execute("DELETE FROM meta WHERE key LIKE '%:Codex'")
    conn.executemany("INSERT INTO overrides(provider, thread_id, kind) VALUES(?, ?, ?)", overrides)
    meta_set(conn, "revision", int(meta_value(conn, "revision") or 0) + 1)


def _migrate_claude_prompt_tiers(conn: sqlite3.Connection) -> None:
    """Schema 4: retain Claude prompt evidence and derive Haiku 5.5 pricing tiers."""
    conn.execute("ALTER TABLE facts ADD COLUMN prompt_peak INTEGER")
    # Legacy facts lost raw-counter provenance; their stored input_total is the documented estimate.
    conn.execute("UPDATE facts SET prompt_peak=input_total WHERE provider=?", (model_mod.CLAUDE,))
    rows = conn.execute("SELECT fact_key, model, prompt_peak FROM facts WHERE provider=?",
                         (model_mod.CLAUDE,)).fetchall()
    for fact_key, model, prompt_peak in rows:
        tier = model_mod.claude_price_tier(model, prompt_peak)
        conn.execute("UPDATE facts SET price_tier=? WHERE provider=? AND fact_key=?",
                     (tier, model_mod.CLAUDE, fact_key))
    # Staged observations written by schema 3 have no prompt_evidence key at all; give them the same
    # stored-input-total estimate. An explicit null (written by this schema) means invalid counters and stays.
    staged = conn.execute("SELECT rowid, observation FROM provisional_facts WHERE provider=?",
                          (model_mod.CLAUDE,)).fetchall()
    for rowid, text in staged:
        observation = json.loads(text)
        if "prompt_evidence" in observation:
            continue
        fields = observation.get("fields") or {}
        observation["prompt_evidence"] = fields.get("input_total")
        observation["price_tier"] = model_mod.claude_price_tier(observation.get("model"), observation["prompt_evidence"])
        conn.execute("UPDATE provisional_facts SET observation=? WHERE rowid=?", (json.dumps(observation), rowid))
    rebuild_rollups(conn, load_overrides(conn))
    meta_set(conn, "revision", int(meta_value(conn, "revision") or 0) + 1)


# A migration step is SQL text or a callable taking the connection (for steps that need Python).
MIGRATIONS: dict[int, tuple[Any, ...]] = {
    1: _SCHEMA_V1, 2: _SCHEMA_V2, 3: (_clear_codex_ledger,), 4: (_migrate_claude_prompt_tiers,)
}


class LedgerSchemaError(RuntimeError):
    """The database was written by a newer schema than this build understands."""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL").fetchone()
    except BaseException:
        conn.close()  # a corrupt file fails here; close so the caller can move it aside
        raise
    return conn


def connect_readonly(path: Path) -> sqlite3.Connection:
    """A reader that never creates, migrates or recovers the database: mode=ro, no journal-mode pragma."""
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10, isolation_level=None)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@contextmanager
def snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A read-only WAL snapshot: every query inside sees the same committed state."""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")


def quick_check(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA quick_check").fetchone()
    if not row or row[0] != "ok":
        raise sqlite3.DatabaseError("ledger quick_check failed")


def migrate(conn: sqlite3.Connection) -> int:
    target = max(MIGRATIONS)
    with transaction(conn):
        conn.execute(MIGRATIONS_META_TABLE)
        current = int(meta_value(conn, "schemaVersion") or 0)
        if current > target:
            raise LedgerSchemaError(f"ledger schema {current} is newer than supported {target}")
        for version in sorted(v for v in MIGRATIONS if current < v <= target):
            for statement in MIGRATIONS[version]:
                if callable(statement):
                    statement(conn)
                else:
                    conn.execute(statement)
            meta_set(conn, "schemaVersion", version)
    return target


def meta_value(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))


def bump_counter(conn: sqlite3.Connection, provider: str, name: str, amount: int = 1) -> None:
    conn.execute("INSERT INTO counters(provider, name, value) VALUES(?, ?, ?) "
                 "ON CONFLICT(provider, name) DO UPDATE SET value = value + excluded.value",
                 (provider, name, amount))


def add_gap(conn: sqlite3.Connection, provider: str, kind: str, at_epoch: int, bytes_before: int) -> None:
    conn.execute("INSERT INTO gaps(provider, kind, at_epoch, bytes_before) VALUES(?, ?, ?, ?)",
                 (provider, kind, int(at_epoch), int(bytes_before)))


def load_source(conn: sqlite3.Connection, provider: str, path_key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT generation, byte_offset, head_key, state FROM sources WHERE provider=? AND path_key=?",
                       (provider, path_key)).fetchone()
    if row is None:
        return None
    return {"generation": row[0], "byte_offset": row[1], "head_key": row[2], "state": json.loads(row[3])}


def save_source(conn: sqlite3.Connection, provider: str, path_key: str, *, generation: int, byte_offset: int,
                head_key: str | None, state: Mapping[str, Any]) -> None:
    conn.execute("INSERT INTO sources(provider, path_key, generation, byte_offset, head_key, state) "
                 "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(provider, path_key) DO UPDATE SET "
                 "generation=excluded.generation, byte_offset=excluded.byte_offset, "
                 "head_key=excluded.head_key, state=excluded.state",
                 (provider, path_key, generation, byte_offset, head_key, json.dumps(dict(state), sort_keys=True)))


def load_thread(conn: sqlite3.Connection, provider: str, thread_id: str) -> dict[str, int] | None:
    row = conn.execute("SELECT counters FROM threads WHERE provider=? AND thread_id=?",
                       (provider, thread_id)).fetchone()
    return json.loads(row[0]) if row else None


def save_thread(conn: sqlite3.Connection, provider: str, thread_id: str, counters: Mapping[str, int]) -> None:
    conn.execute("INSERT INTO threads(provider, thread_id, counters) VALUES(?, ?, ?) "
                 "ON CONFLICT(provider, thread_id) DO UPDATE SET counters=excluded.counters",
                 (provider, thread_id, json.dumps(dict(counters), sort_keys=True)))


def load_overrides(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    return {(row[0], row[1]): row[2] for row in conn.execute("SELECT provider, thread_id, kind FROM overrides")}


def effective_class(overrides: Mapping[tuple[str, str], str], provider: str, thread_id: str,
                    kind: str, rule: str) -> tuple[str, str]:
    override = overrides.get((provider, thread_id))
    return (override, "override") if override else (kind, rule)


def vector(fields: Mapping[str, Any]) -> dict[str, int]:
    """Rollup contribution of one normalized record: totals plus value and missing counts."""
    out: dict[str, int] = {"input_total": int(fields["input_total"]), "output_total": int(fields["output_total"]),
                           "total": int(fields["total"])}
    for name, (value_column, miss_column) in NULLABLE_COLUMNS.items():
        value = fields.get(name)
        out[value_column] = int(value or 0)
        out[miss_column] = 1 if value is None else 0
    return out


def _merge_nullable(old: int | None, new: int | None) -> int | None:
    if old is None:
        return new
    if new is None:
        return old
    return max(old, new)


def merge_fields(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """Revision merge for a copied or re-reported response.

    Claude writes the same response several times while it streams, and copies
    it into resumed transcripts, so the most complete counts win.
    """
    merged: dict[str, Any] = {name: _merge_nullable(old[name], new[name]) for name in NULLABLE_COLUMNS}
    # An unsplit write count overlaps the tiered counts, rather than adding to
    # them. Keep only the amount that remains unaccounted for by known tiers.
    if merged["cache_write_unknown"] is not None:
        tier_names = ("cache_write_5m", "cache_write_1h")
        known = sum(int(merged[name] or 0) for name in tier_names)
        reported = max(sum(int(record[name] or 0) for name in (*tier_names, "cache_write_unknown"))
                       for record in (old, new))
        merged["cache_write_unknown"] = max(0, reported - known)
    subsets = sum(int(merged[name] or 0) for name in ("cached_read", "cache_write_5m", "cache_write_1h",
                                                       "cache_write_unknown"))
    input_total = max(int(old["input_total"]), int(new["input_total"]), subsets)
    output_total = max(int(old["output_total"]), int(new["output_total"]))
    merged.update(input_total=input_total, output_total=output_total, total=input_total + output_total)
    return merged


def _add_rollup(conn: sqlite3.Connection, bucket: tuple[int, str, str, str, str, str],
                contribution: Mapping[str, int], requests: int) -> None:
    hour, provider, model, tier, kind, rule = bucket
    columns = ("requests",) + VALUE_COLUMNS
    values = (hour, provider, model, tier, kind, rule, requests) + tuple(contribution[c] for c in VALUE_COLUMNS)
    assignments = ", ".join(f"{column} = {column} + excluded.{column}" for column in columns)
    conn.execute(
        f"INSERT INTO hourly(hour, provider, model, price_tier, kind, rule, {', '.join(columns)}) "
        f"VALUES ({', '.join('?' * len(values))}) "
        f"ON CONFLICT(hour, provider, model, price_tier, kind, rule) DO UPDATE SET {assignments}",
        values)


def upsert_fact(conn: sqlite3.Connection, fact: Mapping[str, Any], *, merge: bool,
                overrides: Mapping[tuple[str, str], str]) -> bool:
    """Insert one fact, or revision-merge it when merge=True. Returns True when data changed.

    Rollups are adjusted by the difference, so they always equal the sum of facts.
    """
    provider, key = fact["provider"], fact["fact_key"]
    row = conn.execute(
        "SELECT thread_id, model, price_tier, hour, kind, rule, prompt_peak, " + ", ".join(FACT_COLUMNS)
        + " FROM facts WHERE provider=? AND fact_key=?", (provider, key)).fetchone()
    if row is None:
        kind, rule = effective_class(overrides, provider, fact["thread_id"], fact["kind"], fact["rule"])
        prompt_peak = fact.get("prompt_evidence") if provider == model_mod.CLAUDE else None
        tier = (model_mod.claude_price_tier(fact["model"], prompt_peak)
                if provider == model_mod.CLAUDE else fact["price_tier"])
        conn.execute(
            "INSERT INTO facts(provider, fact_key, thread_id, model, price_tier, hour, kind, rule, slug, "
            "classification_version, segment_turn, prompt_peak, " + ", ".join(FACT_COLUMNS) + ") VALUES ("
            + ", ".join("?" * (12 + len(FACT_COLUMNS))) + ")",
            (provider, key, fact["thread_id"], fact["model"], tier, fact["hour"],
             fact["kind"], fact["rule"], fact.get("slug"), fact["classification_version"],
             fact.get("segment_turn"), prompt_peak, *(fact["fields"][name] for name in FACT_COLUMNS)))
        _add_rollup(conn, (fact["hour"], provider, fact["model"], tier, kind, rule),
                    vector(fact["fields"]), 1)
        return True
    if not merge:
        return False
    thread_id, model, old_tier, hour, stored_kind, stored_rule, old_peak = row[:7]
    old = dict(zip(FACT_COLUMNS, row[7:]))
    new = merge_fields(old, fact["fields"])
    prompt_peak = old_peak
    if provider == model_mod.CLAUDE and fact.get("prompt_evidence") is not None:
        prompt_peak = max(old_peak or 0, fact["prompt_evidence"])
    tier = model_mod.claude_price_tier(model, prompt_peak) if provider == model_mod.CLAUDE else old_tier
    tier_changed = tier != old_tier
    if all(new[name] == old[name] for name in FACT_COLUMNS) and prompt_peak == old_peak and not tier_changed:
        return False
    conn.execute(
        "UPDATE facts SET " + ", ".join(f"{name}=?" for name in FACT_COLUMNS)
        + ", prompt_peak=?, price_tier=?, revisions = revisions + 1 WHERE provider=? AND fact_key=?",
        (*(new[name] for name in FACT_COLUMNS), prompt_peak, tier, provider, key))
    kind, rule = effective_class(overrides, provider, thread_id, stored_kind, stored_rule)
    before, after = vector(old), vector(new)
    if tier_changed:
        _add_rollup(conn, (hour, provider, model, old_tier, kind, rule),
                    {column: -before[column] for column in VALUE_COLUMNS}, -1)
        conn.execute("DELETE FROM hourly WHERE hour=? AND provider=? AND model=? AND price_tier=? AND kind=? AND rule=? "
                     "AND requests=0 AND " + " AND ".join(f"{column}=0" for column in VALUE_COLUMNS),
                     (hour, provider, model, old_tier, kind, rule))
        _add_rollup(conn, (hour, provider, model, tier, kind, rule), after, 1)
    else:
        _add_rollup(conn, (hour, provider, model, tier, kind, rule),
                    {column: after[column] - before[column] for column in VALUE_COLUMNS}, 0)
    return True


def rebuild_rollups(conn: sqlite3.Connection, overrides: Mapping[tuple[str, str], str]) -> None:
    """Recompute every hourly rollup from facts, with the current overrides applied."""
    conn.execute("DELETE FROM hourly")
    buckets: dict[tuple[int, str, str, str, str, str], dict[str, int]] = {}
    for row in conn.execute("SELECT provider, thread_id, hour, model, price_tier, kind, rule, "
                            + ", ".join(FACT_COLUMNS) + " FROM facts"):
        provider, thread_id, hour, model, tier, kind, rule = row[:7]
        fields = dict(zip(FACT_COLUMNS, row[7:]))
        kind, rule = effective_class(overrides, provider, thread_id, kind, rule)
        bucket = (hour, provider, model, tier, kind, rule)
        target = buckets.setdefault(bucket, {column: 0 for column in ("requests",) + VALUE_COLUMNS})
        for column, value in vector(fields).items():
            target[column] += value
        target["requests"] += 1
    for bucket, totals in buckets.items():
        requests = totals.pop("requests")
        _add_rollup(conn, bucket, totals, requests)


def purge_provider(conn: sqlite3.Connection, provider: str) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_gap_details'").fetchone():
        conn.execute("DELETE FROM ledger_gap_details WHERE gap_id IN (SELECT id FROM gaps WHERE provider=?)", (provider,))
    for table in ("ledger_omissions", "ledger_unknown_kinds", "ledger_message_evidence"):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            conn.execute(f"DELETE FROM {table} WHERE provider=?", (provider,))
    for table in ("provisional_facts", "source_facts", "facts", "hourly", "threads", "sources", "gaps", "counters", "overrides"):
        conn.execute(f"DELETE FROM {table} WHERE provider=?", (provider,))
    # Provider-scoped coverage metadata ("observedFrom:<P>", "pendingBytes:<P>", ...) goes with the rows it describes.
    for (key,) in conn.execute("SELECT key FROM meta").fetchall():
        if key.endswith(":" + provider):
            conn.execute("DELETE FROM meta WHERE key=?", (key,))
    recompute_globals(conn)


def recompute_globals(conn: sqlite3.Connection) -> None:
    """Global coverage aggregates from the providers that remain; drop the global clock when no facts remain."""
    for name in ("pendingBytes", "verifyingBytes"):
        total = sum(int(meta_value(conn, f"{name}:{provider}") or 0) for provider in model_mod.PROVIDERS)
        meta_set(conn, name, total)
    has_facts = conn.execute("SELECT 1 FROM facts UNION ALL SELECT 1 FROM provisional_facts LIMIT 1").fetchone()
    if has_facts is None:
        conn.execute("DELETE FROM meta WHERE key IN ('observedFrom', 'lastIngestedAt')")


# Only normalized accounting attributes are stored; arbitrary input records are never serialized.
_OBSERVATION_KEYS = ("provider", "fact_key", "thread_id", "model", "price_tier", "prompt_evidence", "hour",
                     "kind", "rule", "slug", "classification_version", "segment_turn", "merge")


def stage_observation(conn: sqlite3.Connection, source_key: str, generation: int, epoch: int,
                      fact: Mapping[str, Any]) -> None:
    """Stage one normalized fact in the caller's transaction, merging revisions in this epoch."""
    clean = {name: fact.get(name) for name in _OBSERVATION_KEYS}
    clean["fields"] = {name: fact["fields"].get(name) for name in FACT_COLUMNS}
    identity = (clean["provider"], source_key, generation, epoch, clean["fact_key"])
    old = conn.execute("SELECT observation FROM provisional_facts WHERE provider=? AND source_key=? "
                       "AND generation=? AND epoch=? AND fact_key=?", identity).fetchone()
    if old:
        previous = json.loads(old[0])
        if not clean["merge"]:
            return
        previous["fields"] = merge_fields(previous["fields"], clean["fields"])
        evidence = (previous.get("prompt_evidence"), clean.get("prompt_evidence"))
        previous["prompt_evidence"] = max((value for value in evidence if value is not None), default=None)
        clean = previous
    conn.execute("INSERT INTO provisional_facts VALUES (?, ?, ?, ?, ?, ?) "
                 "ON CONFLICT(provider, source_key, generation, epoch, fact_key) "
                 "DO UPDATE SET observation=excluded.observation", (*identity, json.dumps(clean, sort_keys=True)))


def discard_provisional(conn: sqlite3.Connection, provider: str, source_key: str) -> None:
    """Roll back every provisional fact of a source (a rewrite was detected)."""
    conn.execute("DELETE FROM provisional_facts WHERE provider=? AND source_key=?", (provider, source_key))


def has_provisional(conn: sqlite3.Connection, provider: str, source_key: str) -> bool:
    return conn.execute("SELECT 1 FROM provisional_facts WHERE provider=? AND source_key=? LIMIT 1",
                        (provider, source_key)).fetchone() is not None


def provisional_exists(conn: sqlite3.Connection, providers: list[str]) -> bool:
    """True when any provisional fact is staged for one of the providers, within the caller's snapshot."""
    if not providers:
        return False
    placeholders = ",".join("?" for _ in providers)
    return conn.execute(f"SELECT 1 FROM provisional_facts WHERE provider IN ({placeholders}) LIMIT 1",
                        providers).fetchone() is not None


def verification_debt_bytes(conn: sqlite3.Connection, providers: list[str]) -> int:
    """Consumed prefix bytes with open deferred exact-verification markers."""
    if not providers:
        return 0
    placeholders = ",".join("?" for _ in providers)
    total = 0
    for offset, raw in conn.execute(f"SELECT byte_offset, state FROM sources WHERE provider IN ({placeholders})", providers):
        try:
            state = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if state.get("verification_debt"):
            total += max(1, int(offset))
    return total


def unverified_sources(conn: sqlite3.Connection, provider: str) -> list[dict[str, Any]]:
    """Persisted sources of one provider whose state holds provisional facts or an open verification."""
    rows = conn.execute("SELECT path_key, generation, byte_offset, head_key, state FROM sources WHERE provider=? "
                        "AND (state LIKE '%\"verify\"%' OR state LIKE '%\"provisional\"%' "
                        "OR state LIKE '%\"verification_debt\"%' "
                        "OR state LIKE '%\"ordered_tail_pending\"%')", (provider,)).fetchall()
    found = []
    for path_key, generation, byte_offset, head_key, text in rows:
        state = json.loads(text)
        if (state.get("verify") or state.get("provisional") or state.get("verification_debt")
                or state.get("ordered_tail_pending")):
            found.append({"path_key": path_key, "generation": generation, "byte_offset": byte_offset,
                          "head_key": head_key, "state": state})
    return found


def note_source_fact(conn: sqlite3.Connection, provider: str, source_key: str, fact_key: str,
                     generation: int, total: int | None) -> None:
    """Record that a source reported a fact in this generation (total=None: seen, value unknown)."""
    conn.execute(
        "INSERT INTO source_facts(provider, source_key, fact_key, generation, total, prev_total) "
        "VALUES(?, ?, ?, ?, ?, NULL) ON CONFLICT(provider, source_key, fact_key) DO UPDATE SET "
        "total = CASE WHEN excluded.total IS NULL THEN total WHEN generation < excluded.generation "
        "THEN excluded.total ELSE MAX(COALESCE(total, 0), excluded.total) END, "
        "prev_total = CASE WHEN excluded.total IS NULL THEN prev_total WHEN generation < excluded.generation "
        "THEN total ELSE prev_total END, generation = excluded.generation",
        (provider, source_key, fact_key, generation, total))


def audit_rewrite(conn: sqlite3.Connection, provider: str, source_key: str, generation: int) -> bool:
    """After a replay: True when the rewritten file dropped or lowered usage this source had confirmed."""
    lowered = conn.execute(
        "SELECT 1 FROM source_facts WHERE provider=? AND source_key=? AND (generation < ? OR "
        "(generation = ? AND prev_total IS NOT NULL AND total IS NOT NULL AND prev_total > total)) LIMIT 1",
        (provider, source_key, generation, generation)).fetchone() is not None
    conn.execute("DELETE FROM source_facts WHERE provider=? AND source_key=? AND generation < ?",
                 (provider, source_key, generation))
    conn.execute("UPDATE source_facts SET prev_total=NULL WHERE provider=? AND source_key=?", (provider, source_key))
    return lowered


def discard_epoch(conn: sqlite3.Connection, provider: str, source_key: str, generation: int, epoch: int) -> None:
    conn.execute("DELETE FROM provisional_facts WHERE provider=? AND source_key=? AND generation=? AND epoch=?",
                 (provider, source_key, generation, epoch))


def promote_epoch(conn: sqlite3.Connection, provider: str, source_key: str, generation: int, epoch: int) -> bool:
    """Merge a verified epoch into confirmed facts, then remove its staged support atomically."""
    rows = conn.execute("SELECT observation FROM provisional_facts WHERE provider=? AND source_key=? "
                        "AND generation=? AND epoch=? ORDER BY fact_key",
                        (provider, source_key, generation, epoch)).fetchall()
    overrides = load_overrides(conn)
    changed = False
    for (observation,) in rows:
        fact = json.loads(observation)
        changed = upsert_fact(conn, fact, merge=bool(fact["merge"]), overrides=overrides) or changed
        note_source_fact(conn, provider, source_key, fact["fact_key"], generation, int(fact["fields"]["total"]))
    discard_epoch(conn, provider, source_key, generation, epoch)
    return changed


def summary_rows(conn: sqlite3.Connection, start_hour: int, end_hour: int) -> list[dict[str, Any]]:
    columns = ("requests",) + VALUE_COLUMNS
    sums = ", ".join(f"SUM({column})" for column in columns)
    rows = conn.execute(
        f"SELECT provider, model, price_tier, kind, rule, {sums} FROM hourly "
        "WHERE hour >= ? AND hour < ? GROUP BY provider, model, price_tier, kind, rule",
        (start_hour, end_hour)).fetchall()
    buckets = {tuple(row[:5]): dict(zip(columns, row[5:])) for row in rows}
    staged = conn.execute("SELECT provider, fact_key, observation FROM provisional_facts "
                          "ORDER BY provider, fact_key, source_key, generation, epoch").fetchall()
    if not staged:
        return [dict(zip(("provider", "model", "price_tier", "kind", "rule") + columns, row)) for row in rows]
    overrides = load_overrides(conn)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for provider, key, payload in staged:
        groups.setdefault((provider, key), []).append(json.loads(payload))

    def adjust(fact: Mapping[str, Any], sign: int) -> None:
        if not start_hour <= fact["hour"] < end_hour:
            return
        kind, rule = effective_class(overrides, fact["provider"], fact["thread_id"], fact["kind"], fact["rule"])
        key = (fact["provider"], fact["model"], fact["price_tier"], kind, rule)
        target = buckets.setdefault(key, {name: 0 for name in columns})
        target["requests"] += sign
        for name, value in vector(fact["fields"]).items():
            target[name] += sign * value

    for (provider, key), observations in groups.items():
        row = conn.execute("SELECT thread_id, model, price_tier, hour, kind, rule, prompt_peak, " + ", ".join(FACT_COLUMNS)
                           + " FROM facts WHERE provider=? AND fact_key=?", (provider, key)).fetchone()
        effective = None
        if row:
            effective = dict(zip(("thread_id", "model", "price_tier", "hour", "kind", "rule", "prompt_peak"), row[:7]))
            effective.update(provider=provider, fields=dict(zip(FACT_COLUMNS, row[7:])))
            adjust(effective, -1)
        for observation in observations:
            if effective is None:
                effective = observation
            elif observation["merge"]:
                effective["fields"] = merge_fields(effective["fields"], observation["fields"])
                evidence = (effective.get("prompt_peak"), effective.get("prompt_evidence"),
                            observation.get("prompt_evidence"))
                effective["prompt_peak"] = max((value for value in evidence if value is not None), default=None)
        if effective is not None:
            if provider == model_mod.CLAUDE:
                peak = effective.get("prompt_peak", effective.get("prompt_evidence"))
                effective["price_tier"] = model_mod.claude_price_tier(effective["model"], peak)
            adjust(effective, 1)
    return [dict(zip(("provider", "model", "price_tier", "kind", "rule"), key), **values)
            for key, values in buckets.items() if values["requests"]]


def fact_and_rollup_totals(conn: sqlite3.Connection) -> tuple[int, int]:
    """Sum of fact totals and of rollup totals; equal whenever rollups match facts."""
    facts = conn.execute("SELECT COALESCE(SUM(total), 0) FROM facts").fetchone()[0]
    rollups = conn.execute("SELECT COALESCE(SUM(total), 0) FROM hourly").fetchone()[0]
    return int(facts), int(rollups)
