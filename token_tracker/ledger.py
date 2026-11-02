"""Work ledger facade: exact observed usage per model and kind of work, with explicit coverage.

The caller supplies the data directory and the Claude and Codex roots. Nothing here
reads a default profile path. Ingestion runs only for providers the caller marks as
collected; summaries hide providers the caller does not show.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import ledger_classify, ledger_ingest, ledger_model, ledger_store as store

LEDGER_SCHEMA_VERSION_KEY = "schemaVersion"
_COUNT_COLUMNS = ("requests",) + store.VALUE_COLUMNS


def _iso(epoch: float | int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat()


def _hour(epoch: float) -> int:
    return int(epoch // 3600) * 3600


class LedgerUnavailable(RuntimeError):
    """A read-only view could not be produced: the database is missing, locked, or not yet migrated."""


class Ledger:
    def __init__(self, data_dir: Path | str, *, claude_root: Path | str, codex_root: Path | str,
                 clock: Callable[[], float] = time.time, kind_registry_path: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.kind_registry_path = Path(kind_registry_path) if kind_registry_path is not None else self.data_dir / "ledger-kinds.json"
        try:
            with self.kind_registry_path.open("x", encoding="utf-8") as handle:
                json.dump([{"kind": kind, "meaning": meaning} for kind, meaning in
                           ledger_classify.DEFAULT_KIND_REGISTRY.items()], handle, indent=2)
        except FileExistsError:
            pass
        self.db_path = self.data_dir / store.DB_NAME
        self.recovery_intent_path = self.data_dir / (store.DB_NAME + ".recovery-intent")
        self.clock = clock
        self.recovery: dict[str, Any] | None = None
        self.conn = self._open()
        self._ingestor = ledger_ingest.Ingestor(self.conn, claude_root=Path(claude_root),
                                                codex_root=Path(codex_root), clock=clock,
                                                kind_registry_path=self.kind_registry_path)

    # ------------------------------------------------------------------ lifecycle

    def _open(self) -> sqlite3.Connection:
        if self.recovery_intent_path.exists():
            self.recovery = self._load_recovery_intent()
            self._quarantine(self.recovery["quarantined"])
            return self._open_replacement()
        if self.db_path.exists():
            try:
                conn = store.connect(self.db_path)
            except sqlite3.OperationalError:
                raise  # locked or unavailable: never rebuild a database that might be healthy
            except sqlite3.DatabaseError:
                self.recovery = self._begin_recovery("unreadable")
            else:
                try:
                    store.quick_check(conn)
                    store.migrate(conn)
                    return conn
                except sqlite3.OperationalError:
                    conn.close()
                    raise
                except sqlite3.DatabaseError:
                    conn.close()
                    self.recovery = self._begin_recovery("corrupt")
        return self._open_replacement()

    def _open_replacement(self) -> sqlite3.Connection:
        conn = store.connect(self.db_path)
        try:
            store.quick_check(conn)
            store.migrate(conn)
            if self.recovery:
                recovery_key = "recovery:" + self.recovery["id"]
                with store.transaction(conn):
                    if store.meta_value(conn, recovery_key) is None:
                        store.add_gap(conn, "*", "corruption-recovery", int(self.clock()), 0)
                        store.meta_set(conn, recovery_key, "recorded")
                self._clear_recovery_intent()
        except BaseException:
            conn.close()
            raise
        return conn

    def _begin_recovery(self, reason: str) -> dict[str, Any]:
        token = uuid.uuid4().hex
        intent = {"status": "rebuilt", "reason": reason, "id": token,
                  "quarantined": f"{self.db_path.name}.corrupt-{token}"}
        temporary = self.recovery_intent_path.with_name(self.recovery_intent_path.name + "." + token + ".tmp")
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(intent, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.recovery_intent_path)
        self._quarantine(intent["quarantined"])
        return intent

    def _load_recovery_intent(self) -> dict[str, Any]:
        intent = json.loads(self.recovery_intent_path.read_text(encoding="utf-8"))
        token = intent.get("id") if isinstance(intent, dict) else None
        if (not isinstance(token, str) or len(token) != 32 or any(c not in "0123456789abcdef" for c in token)
                or intent.get("quarantined") != f"{self.db_path.name}.corrupt-{token}"
                or intent.get("reason") not in {"corrupt", "unreadable"}):
            raise ValueError("invalid ledger recovery intent")
        return intent

    def _clear_recovery_intent(self) -> None:
        self.recovery_intent_path.unlink()

    def _quarantine(self, name: str) -> None:
        """Move a corrupt database aside (kept for inspection) so a clean one can be rebuilt."""
        target = self.db_path.with_name(name)
        if target.exists() and self.db_path.exists():
            return  # Quarantine completed; the current database is the replacement.
        if not target.exists() and self.db_path.exists():
            self.db_path.rename(target)
        for suffix in ("-wal", "-shm"):
            side = self.db_path.with_name(self.db_path.name + suffix)
            if side.exists():
                side.rename(target.with_name(target.name + suffix))

    def close(self) -> None:
        self.conn.close()

    def health(self) -> dict[str, Any]:
        return {"status": "rebuilt" if self.recovery else "ok", "recovery": self.recovery,
                "schemaVersion": int(store.meta_value(self.conn, LEDGER_SCHEMA_VERSION_KEY) or 0)}

    # ------------------------------------------------------------------ writes

    def ingest(self, collect: Mapping[str, bool] | None = None, *, budget_bytes: int | None = None,
               fault: Callable[[str], None] | None = None,
               should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Read retained sources for collected providers only. Fail closed: no collect, no reads.

        fault is a test seam: it is called inside the transaction at "after_facts" and
        "after_cursor", and raising there must leave nothing counted or advanced.
        should_stop is polled between files and once per hashed verification chunk; a stop
        commits nothing from the interrupted step and returns with "stopped": True.
        """
        return self._ingestor.run(dict(collect or {}), budget_bytes, fault, should_stop)

    def pending_bytes(self, show: Mapping[str, bool] | None = None) -> int:
        """Bytes not yet read from the last pass, counting only providers the show policy allows."""
        allowed = [name for name in ledger_model.PROVIDERS if show is None or show.get(name)]
        return sum(int(store.meta_value(self.conn, f"pendingBytes:{name}") or 0) for name in allowed)

    def set_override(self, provider: str, thread_id: str, kind: str) -> None:
        if kind not in ledger_classify.load_kind_registry(self.kind_registry_path) and kind != "other":
            raise ValueError(f"unknown kind: {kind}")
        with store.transaction(self.conn) as conn:
            conn.execute("INSERT INTO overrides(provider, thread_id, kind) VALUES(?, ?, ?) "
                         "ON CONFLICT(provider, thread_id) DO UPDATE SET kind=excluded.kind",
                         (provider, thread_id, kind))
            self._rebuild(conn)

    def clear_override(self, provider: str, thread_id: str) -> None:
        with store.transaction(self.conn) as conn:
            conn.execute("DELETE FROM overrides WHERE provider=? AND thread_id=?", (provider, thread_id))
            self._rebuild(conn)

    def rebuild_rollups(self) -> None:
        with store.transaction(self.conn) as conn:
            self._rebuild(conn)

    def purge(self, provider: str) -> None:
        """Delete everything stored for one provider (facts, rollups, cursors, gaps, overrides)."""
        if provider not in ledger_model.PROVIDERS:
            raise ValueError(f"unknown provider: {provider}")
        with store.transaction(self.conn) as conn:
            store.purge_provider(conn, provider)
            store.meta_set(conn, "revision", int(store.meta_value(conn, "revision") or 0) + 1)

    def _rebuild(self, conn: sqlite3.Connection) -> None:
        store.rebuild_rollups(conn, store.load_overrides(conn))
        store.meta_set(conn, "revision", int(store.meta_value(conn, "revision") or 0) + 1)

    # ------------------------------------------------------------------ reads

    def summary(self, start: float, end: float, *, show: Mapping[str, bool] | None = None,
                time_zone: str = "UTC", plan_fee_usd: float | None = None) -> dict[str, Any]:
        """Per provider+model+kind rows and totals for hours in [start, end), from one snapshot."""
        now = self.clock()
        conn = store.connect_readonly(self.db_path)
        try:
            return self._payload(conn, now, start, end, show=show, time_zone=time_zone, plan_fee_usd=plan_fee_usd)
        finally:
            conn.close()

    @staticmethod
    def _payload(conn: sqlite3.Connection, now: float, start: float, end: float, *,
                 show: Mapping[str, bool] | None, time_zone: str, plan_fee_usd: float | None) -> dict[str, Any]:
        with store.snapshot(conn):
            raw = store.summary_rows(conn, _hour(start), int(end))
            coverage = Ledger._coverage(conn, now, show)
            revision = int(store.meta_value(conn, "revision") or 0)
            schema = int(store.meta_value(conn, LEDGER_SCHEMA_VERSION_KEY) or 0)
        rows = Ledger._rows(raw, show)
        totals = {"requests": 0, "inputTotal": 0, "outputTotal": 0, "totalTokens": 0, "knownCostUsd": None,
                  "costComplete": True, "unpricedTokens": 0}
        known = 0.0
        for row in rows:
            totals["requests"] += row["requests"]
            totals["inputTotal"] += row["inputTotal"]
            totals["outputTotal"] += row["outputTotal"]
            totals["totalTokens"] += row["totalTokens"]
            totals["unpricedTokens"] += row["unpricedTokens"]
            totals["costComplete"] = totals["costComplete"] and row["costComplete"]
            if row["knownCostUsd"] is not None:
                known += row["knownCostUsd"]
                totals["knownCostUsd"] = round(known, 6)
        ratio_value = None
        if plan_fee_usd and plan_fee_usd > 0 and totals["knownCostUsd"] is not None and totals["costComplete"]:
            ratio_value = round(totals["knownCostUsd"] / plan_fee_usd, 2)
        return {
            "schemaVersion": schema,
            "ledgerRevision": revision,
            "generatedAt": _iso(now),
            "range": {"start": _iso(start), "end": _iso(end), "timeZone": time_zone},
            "coverage": coverage,
            "pricingVersion": ledger_model.pricing_version(),
            "classificationVersion": ledger_classify.CLASSIFICATION_VERSION,
            "rows": rows,
            "totals": totals,
            "apiRatio": {
                "value": ratio_value, "basis": "api-equivalent / plan fee", "planFeeUsd": plan_fee_usd,
                "unavailableReason": None if ratio_value is not None else (
                    "plan fee not provided" if not plan_fee_usd else "cost incomplete"),
            },
            "planConsumedPercent": None,
            "planConsumedUnavailableReason": "Local logs carry no allowance data",
        }


    @staticmethod
    def _rows(raw: list[dict[str, Any]], show: Mapping[str, bool] | None) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        for record in raw:
            provider, model, tier, kind, rule = (record[name] for name in ("provider", "model", "price_tier", "kind", "rule"))
            if show is not None and not show.get(provider):
                continue
            group = groups.setdefault((provider, model, kind), {
                "sums": {column: 0 for column in _COUNT_COLUMNS}, "rules": set(), "known": 0.0,
                "priced": False, "unpriced": 0, "missingCost": False})
            sums = {column: int(record[column] or 0) for column in _COUNT_COLUMNS}
            for column in _COUNT_COLUMNS:
                group["sums"][column] += sums[column]
            group["rules"].add(rule)
            cost = ledger_model.price_usd(provider, model, tier, sums)
            if cost is None:
                group["unpriced"] += sums["total"]
            else:
                group["known"] += cost
                group["priced"] = True
            for field in ledger_model.COST_NULLABLE_FIELDS[provider]:
                if sums[store.NULLABLE_COLUMNS[field][1]]:
                    group["missingCost"] = True
            if provider == ledger_model.CLAUDE and sums["cache_write_unknown"] > 0:
                group["missingCost"] = True
        rows = []
        for (provider, model, kind), group in sorted(groups.items()):
            sums = group["sums"]
            requests = sums["requests"]

            def breakdown(field: str) -> int | None:
                value_column, miss_column = store.NULLABLE_COLUMNS[field]
                if requests and sums[miss_column] == requests:
                    return None
                return sums[value_column]

            rules = group["rules"]
            rows.append({
                "provider": provider,
                "modelId": model,
                "displayName": model,
                "kind": kind,
                "requests": requests,
                "inputTotal": sums["input_total"],
                "cachedRead": breakdown("cached_read"),
                "cacheWrite5m": breakdown("cache_write_5m"),
                "cacheWrite1h": breakdown("cache_write_1h"),
                "cacheWriteUnknown": breakdown("cache_write_unknown"),
                "outputTotal": sums["output_total"],
                "reasoningOutput": breakdown("reasoning_output"),
                "totalTokens": sums["total"],
                "knownCostUsd": round(group["known"], 6) if group["priced"] else None,
                "costComplete": group["unpriced"] == 0 and not group["missingCost"],
                "unpricedTokens": group["unpriced"],
                "classification": {
                    "status": "override" if rules == {"override"} else "inferred",
                    "source": next(iter(rules)) if len(rules) == 1 else "mixed",
                },
            })
        return rows

    def coverage(self) -> dict[str, Any]:
        conn = store.connect_readonly(self.db_path)
        try:
            with store.snapshot(conn):
                return self._coverage(conn, self.clock())
        finally:
            conn.close()

    @staticmethod
    def _coverage(conn: sqlite3.Connection, now: float, show: Mapping[str, bool] | None = None) -> dict[str, Any]:
        allowed = [name for name in ledger_model.PROVIDERS if show is None or show.get(name)]
        observed_values = [store.meta_value(conn, "observedFrom:" + name) for name in allowed]
        last_values = [store.meta_value(conn, "lastIngestedAt:" + name) for name in allowed]
        observed_values = [int(value) for value in observed_values if value is not None]
        last_values = [int(value) for value in last_values if value is not None]
        # Older ledgers only have global metadata; use it only with no filter.
        observed = min(observed_values) if observed_values else (store.meta_value(conn, "observedFrom") if show is None else None)
        last = max(last_values) if last_values else (store.meta_value(conn, "lastIngestedAt") if show is None else None)
        if show is None:
            pending = int(store.meta_value(conn, "pendingBytes") or 0)
            verifying = int(store.meta_value(conn, "verifyingBytes") or 0)
        else:
            pending = sum(int(store.meta_value(conn, "pendingBytes:" + name) or 0) for name in allowed)
            verifying = sum(int(store.meta_value(conn, "verifyingBytes:" + name) or 0) for name in allowed)
        collected = set(json.loads(store.meta_value(conn, "collected") or "[]"))
        selected = allowed + ["*"]
        placeholders = ",".join("?" for _ in selected)
        counts = {name: int(value or 0) for name, value in conn.execute(
            f"SELECT name, SUM(value) FROM counters WHERE provider IN ({placeholders}) GROUP BY name", selected)}
        gap_count = conn.execute(f"SELECT COUNT(*) FROM gaps WHERE provider IN ({placeholders})", selected).fetchone()[0]
        gaps = []
        has_details = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_gap_details'").fetchone()
        detail_join = "LEFT JOIN ledger_gap_details d ON d.gap_id=g.id" if has_details else ""
        detail_cols = "d.thread_id, d.interval_start, d.interval_end" if has_details else "NULL, NULL, NULL"
        for provider, kind, at, thread, start, end in conn.execute(
                f"SELECT g.provider, g.kind, g.at_epoch, {detail_cols} FROM gaps g {detail_join} "
                f"WHERE g.provider IN ({placeholders}) "
                # The advisory lowered-usage gap lists after the reset gap it explains.
                "ORDER BY g.kind = 'rewrite-lowered-confirmed', g.id DESC LIMIT 20", selected):
            gap = {"provider": provider, "kind": kind, "at": _iso(at)}
            if thread is not None:
                gap.update(threadId=thread, intervalStart=_iso(start), intervalEnd=_iso(end))
            gaps.append(gap)
        omissions = 0
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_omissions'").fetchone():
            omissions = conn.execute(f"SELECT COUNT(*) FROM ledger_omissions WHERE provider IN ({placeholders})", selected).fetchone()[0]
        unknown_kinds = {}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_unknown_kinds'").fetchone():
            unknown_kinds = dict(conn.execute(
                f"SELECT kind, COUNT(*) FROM ledger_unknown_kinds WHERE provider IN ({placeholders}) "
                "GROUP BY kind ORDER BY kind", selected))
        if verifying or store.provisional_exists(conn, allowed):
            # Staged usage is unconfirmed even when an interrupted run left the coverage meta behind.
            status = "verifying"
        elif gap_count or counts.get("readFailures") or omissions:
            status = "partial"
        elif observed is None:
            status = "empty"
        elif pending:
            status = "catching-up"
        else:
            status = "complete"
        return {
            "status": status,
            "observedFrom": _iso(int(observed)) if observed is not None else None,
            "lastIngestedAt": _iso(int(last)) if last is not None else None,
            "ingestionLagSeconds": max(0, int(now - int(last))) if last is not None else None,
            "pendingBytes": pending,
            "verifyingBytes": verifying,
            "gaps": gaps,
            "gapCount": gap_count,
            "readFailures": counts.get("readFailures", 0),
            "truncations": counts.get("truncations", 0),
            "replacements": counts.get("replacements", 0),
            "malformedLines": counts.get("malformedLines", 0),
            "unknownUsageRecords": counts.get("unknownUsageRecords", 0),
            "unknownKinds": unknown_kinds,
            "providersNotCollected": [name for name in allowed if name not in collected],
        }


def read_summary(data_dir: Path | str, start: float, end: float, *, show: Mapping[str, bool] | None,
                 time_zone: str, plan_fee_usd: float | None = None, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """Same payload as Ledger.summary, read through a reader that never creates, migrates or recovers the file."""
    path = Path(data_dir) / store.DB_NAME
    if not path.is_file():
        raise LedgerUnavailable("ledger database is missing")
    try:
        conn = store.connect_readonly(path)
    except sqlite3.Error as error:
        raise LedgerUnavailable("ledger database could not be opened") from error
    try:
        return Ledger._payload(conn, clock(), start, end, show=show, time_zone=time_zone, plan_fee_usd=plan_fee_usd)
    except sqlite3.Error as error:
        raise LedgerUnavailable("ledger database could not be read") from error
    finally:
        conn.close()
