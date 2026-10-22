"""One-shot, explicitly selected Codex chat continuation actions."""
from __future__ import annotations

import copy
import math
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .provider_policy import POLICIES, ProviderPolicy, ProviderPolicyError, policy_for


MAX_TARGETS = 20
MAX_CYCLES = 64
MAX_LEDGER_TARGETS = 128
INSPECTION_MAX_AGE_SECONDS = 5
ACTION_SCHEMA_VERSION = 2


def default_action() -> dict[str, Any]:
    return {"schemaVersion": ACTION_SCHEMA_VERSION, "mode": "greet", "threadIds": [],
            "armed": False, "oneShot": True, "lastRun": None}


def _valid_ids(thread_ids: Iterable[str]) -> list[str]:
    if isinstance(thread_ids, (str, bytes)):
        raise ValueError("thread_ids must be a list of UUIDs")
    try:
        values = list(thread_ids)
    except TypeError as exc:
        raise ValueError("thread_ids must be a list of UUIDs") from exc
    if len(values) > MAX_TARGETS:
        raise ValueError(f"at most {MAX_TARGETS} chats can be selected")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("chat IDs must be UUID strings")
        try:
            parsed = str(uuid.UUID(value))
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"invalid chat UUID: {value!r}") from exc
        if parsed != value.lower():
            raise ValueError(f"invalid chat UUID: {value!r}")
        if parsed not in seen:
            normalized.append(parsed)
            seen.add(parsed)
    return normalized


class ResetActionDispatcher:
    """Persisted selection and deduplicated queueing for a single reset cycle.

    ``save`` must durably persist the complete state and return its success.
    A claim is written before an external queue call. Any claim left pending by
    a crash is terminal and is surfaced as unknown on the next attempt.
    """

    def __init__(
        self,
        *,
        list_candidates: Callable[..., list[dict[str, Any]]] | None,
        inspect_target: Callable[[str], dict[str, Any]] | None,
        queue_continue: Callable[[str], dict[str, Any]] | None,
        clock: Callable[[], float],
        save: Callable[[], bool],
        action_state: Callable[[], dict[str, Any]],
        policies: Mapping[str, ProviderPolicy] | None = None,
    ) -> None:
        self.list_candidates = list_candidates
        self.inspect_target = inspect_target
        self.queue_continue = queue_continue
        self.clock = clock
        self.save = save
        self.action_state = action_state
        self.policies = POLICIES if policies is None else policies
        for provider_name in ("Codex", "Claude"):
            policy_for(provider_name, self.policies)
        self._migrate_action_state()

    def _migrate_action_state(self) -> None:
        state = self.action_state()
        if state.get("schemaVersion") == ACTION_SCHEMA_VERSION:
            if (state.get("mode") == "continue" and state.get("armed")
                    and not policy_for("Codex", self.policies).allows("continue")):
                state["armed"] = False
                self.save()
            return
        ids: list[str] = []
        saved_ids = state.get("threadIds")
        if isinstance(saved_ids, list):
            for item in saved_ids:
                if not isinstance(item, str):
                    continue
                try:
                    parsed = str(uuid.UUID(item))
                except (ValueError, AttributeError):
                    continue
                if parsed == item.lower() and parsed not in ids:
                    ids.append(parsed)
        migrated = default_action()
        migrated.update({
            "mode": "continue" if ids else "greet",
            "threadIds": ids,
            "armed": False,
            "lastRun": state.get("lastRun"),
            "ledger": state.get("ledger", {"cycles": []}),
        })
        state.clear()
        state.update(migrated)
        self.save()

    def configure(
        self, mode: str, thread_ids: Iterable[str], *, baseline: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if mode not in {"greet", "continue"}:
            raise ValueError("mode must be 'greet' or 'continue'")
        if mode == "continue":
            policy_for("Codex", self.policies).require("continue")
        ids = _valid_ids(thread_ids)
        if mode == "greet" and ids:
            raise ValueError("greet mode cannot have selected chats")
        if mode == "continue":
            if not ids:
                raise ValueError("continue mode requires at least one selected chat")
            if self.list_candidates is None:
                raise RuntimeError("Codex chat discovery is unavailable")
            try:
                candidates = self.list_candidates(limit=100)
            except TypeError:
                candidates = self.list_candidates(100)
            except Exception as exc:
                raise RuntimeError("could not verify selected Codex chats") from exc
            by_id = {
                row.get("id"): row for row in candidates
                if isinstance(row, dict) and isinstance(row.get("id"), str)
            }
            for thread_id in ids:
                row = by_id.get(thread_id)
                if row is None or row.get("archived") is True or row.get("status") == "archived":
                    raise ValueError(f"selected Codex chat does not exist or is archived: {thread_id}")

        state = self.action_state()
        previous = copy.deepcopy(state)
        state.clear()
        state.update({
            "schemaVersion": ACTION_SCHEMA_VERSION,
            "mode": mode,
            "threadIds": ids if mode == "continue" else [],
            "armed": mode == "continue",
            "oneShot": True,
            "lastRun": previous.get("lastRun"),
            "ledger": previous.get("ledger", {"cycles": []}),
        })
        if mode == "continue":
            state.update(copy.deepcopy(baseline or {}))
        if not self.save():
            state.clear()
            state.update(previous)
            raise RuntimeError("could not durably save reset action configuration")
        return self.public_state(state)

    @staticmethod
    def public_state(state: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy({
            "schemaVersion": state.get("schemaVersion", ACTION_SCHEMA_VERSION),
            "mode": state.get("mode", "greet"),
            "threadIds": state.get("threadIds", []),
            "armed": bool(state.get("armed", False)),
            "oneShot": True,
            "lastRun": state.get("lastRun"),
        })

    def get(self) -> dict[str, Any]:
        return self.public_state(self.action_state())

    def _ledger(self, state: dict[str, Any]) -> dict[str, Any]:
        ledger = state.setdefault("ledger", {"cycles": []})
        if not isinstance(ledger, dict) or not isinstance(ledger.get("cycles"), list):
            ledger = {"cycles": []}
            state["ledger"] = ledger
        return ledger

    def dispatch(self, cycle_id: str) -> dict[str, Any] | None:
        policy_for("Codex", self.policies).require("continue")
        state = self.action_state()
        if state.get("mode") != "continue" or not state.get("armed"):
            return None
        targets = list(state.get("threadIds") or [])
        ledger = self._ledger(state)
        cycles = ledger["cycles"]
        cycle = next((row for row in cycles if row.get("cycleId") == cycle_id), None)
        if cycle is None:
            cycle = {"cycleId": cycle_id, "targets": []}
            cycles.append(cycle)
        entries = cycle["targets"]
        by_id = {row.get("threadId"): row for row in entries if isinstance(row, dict)}
        results: list[dict[str, Any]] = []
        for thread_id in targets:
            old = by_id.get(thread_id)
            if old is not None:
                status = old.get("status", "unknown")
                if status == "claimed":
                    status = "unknown"
                results.append({"threadId": thread_id, "status": status, "reason": old.get("reason")})
                continue
            if len(entries) >= MAX_LEDGER_TARGETS:
                results.append({"threadId": thread_id, "status": "skipped", "reason": "cycle target ledger is full"})
                continue
            entry = {"threadId": thread_id, "status": "claimed", "reason": None}
            entries.append(entry)
            # Retain at most 64 cycles and 128 target claims in the durable ledger.
            ledger["cycles"] = cycles[-MAX_CYCLES:]
            if not self.save():
                entries.remove(entry)
                results.append({"threadId": thread_id, "status": "skipped", "reason": "could not persist queue claim"})
                continue

            status, reason = self._queue_one(thread_id)
            entry.update({"status": status, "reason": reason})
            if not self.save():
                # The durable claim remains terminal even if the result update
                # fails; recovery treats a persisted claimed entry as unknown.
                status, reason = "unknown", "queue result could not be persisted"
            results.append({"threadId": thread_id, "status": status, "reason": reason})

        run = {"cycleId": cycle_id, "at": self.clock(), "targets": results}
        state["lastRun"] = run
        state.update({"mode": "continue", "armed": False, "oneShot": True})
        self.save()
        return copy.deepcopy(run)

    def _queue_one(self, thread_id: str) -> tuple[str, str | None]:
        if self.inspect_target is None or self.queue_continue is None:
            return "skipped", "Codex chat continuation is unavailable"
        try:
            info = self.inspect_target(thread_id)
        except Exception:
            return "skipped", "chat inspection failed"
        if not isinstance(info, dict) or info.get("exists") is not True:
            return "skipped", "chat is missing or unknown"
        if info.get("archived") is True:
            return "skipped", "chat is archived"
        if info.get("archived") is not False:
            return "skipped", "chat archival status is unknown"
        try:
            if isinstance(info.get("observedAt"), bool):
                raise ValueError("boolean timestamp")
            observed = float(info.get("observedAt"))
        except (TypeError, ValueError):
            return "skipped", "chat inspection timestamp is unknown"
        if not math.isfinite(observed):
            return "skipped", "chat inspection timestamp is unknown"
        age = self.clock() - observed
        if age < 0 or age > INSPECTION_MAX_AGE_SECONDS:
            return "skipped", "chat inspection is stale"
        if (info.get("running") or info.get("waiting") or info.get("compacting")
                or info.get("needsInput") or info.get("knownIdle") is not True):
            return "skipped", "chat is active or not verified idle"
        try:
            policy_for("Codex", self.policies).require("continue")
            result = self.queue_continue(thread_id)
        except ProviderPolicyError:
            return "skipped", "Codex continuation is not permitted"
        except Exception:
            return "unknown", "queue result is unknown"
        if not isinstance(result, dict):
            return "unknown", "queue result is unknown"
        status = result.get("status")
        if status == "queued":
            return "queued", None
        if status == "rejected":
            return "rejected", "queue rejected the continuation"
        return "unknown", "queue result is unknown"
