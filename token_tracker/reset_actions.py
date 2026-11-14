"""One-shot, explicitly selected Codex chat continuation actions."""
from __future__ import annotations

import copy
import math
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .codex_client import CodexNotStarted
from .provider_policy import POLICIES, ProviderPolicy, ProviderPolicyError, policy_for
from .providers import SnapshotShutdown


MAX_TARGETS = 20
MAX_CYCLES = 64
MAX_LEDGER_TARGETS = 128
INSPECTION_MAX_AGE_SECONDS = 5
ACTION_SCHEMA_VERSION = 2
MAX_NOT_STARTED_ATTEMPTS = 3
NOT_STARTED_RETRY_SECONDS = 900


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
        lock: threading.RLock | None = None,
        policies: Mapping[str, ProviderPolicy] | None = None,
    ) -> None:
        self.list_candidates = list_candidates
        self.inspect_target = inspect_target
        self.queue_continue = queue_continue
        self.clock = clock
        self.save = save
        self.action_state = action_state
        self.lock = lock or threading.RLock()
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
        validated_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        ids = self.validate_configuration(mode, thread_ids) if validated_ids is None else validated_ids
        with self.lock:
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
                "armGeneration": str(uuid.uuid4()),
            })
            if mode == "continue":
                state.update({key: copy.deepcopy(value) for key, value in (baseline or {}).items()
                              if key in {"armedAt", "previousSample", "frozenDeadline", "sawBlocked", "cycleId"}})
            if not self.save():
                state.clear()
                state.update(previous)
                raise RuntimeError("could not durably save reset action configuration")
            return self.public_state(state)

    def validate_configuration(self, mode: str, thread_ids: Iterable[str]) -> list[str]:
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
            with self.lock:
                selected_ids = frozenset(self.action_state().get("threadIds") or [])
            try:
                candidates = self.list_candidates(limit=100)
            except Exception as exc:
                raise RuntimeError("could not verify selected Codex chats") from exc
            by_id = {
                row.get("id"): row for row in candidates
                if isinstance(row, dict) and isinstance(row.get("id"), str)
            }
            for thread_id in ids:
                row = by_id.get(thread_id)
                if row is None and thread_id in selected_ids:
                    try:
                        info = self.inspect_target(thread_id) if self.inspect_target else None
                    except Exception as exc:
                        raise RuntimeError("could not verify selected Codex chats") from exc
                    if isinstance(info, dict) and info.get("exists") is True and info.get("archived") is False:
                        continue
                if row is None or row.get("archived") is True or row.get("status") == "archived":
                    raise ValueError(f"selected Codex chat does not exist or is archived: {thread_id}")

        return ids

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
        with self.lock:
            state = self.action_state()
            if state.get("mode") != "continue" or not state.get("armed"):
                return None
            if state.get("retryPending") and self.clock() < float(state.get("retryAfter") or 0):
                return None
            previous = copy.deepcopy(state)
            generation = state.get("armGeneration") or str(uuid.uuid4())
            state["armGeneration"] = generation
            targets = tuple(state.get("threadIds") or [])
            ledger = self._ledger(state)
            cycles = ledger["cycles"]
            cycle = next((row for row in cycles if row.get("cycleId") == cycle_id), None)
            if cycle is None:
                cycle = {"cycleId": cycle_id, "targets": []}
                cycles.append(cycle)
                ledger["cycles"] = cycles[-MAX_CYCLES:]
            entries = cycle.setdefault("targets", [])
            by_id = {row.get("threadId"): row for row in entries if isinstance(row, dict)}
            deliver = []
            for target in targets:
                entry = by_id.get(target)
                if entry is not None and entry.get("status") == "claimed":
                    entry.update(status="unknown", reason="delivery interrupted")
                if entry is None:
                    if len(entries) >= MAX_LEDGER_TARGETS:
                        continue
                    entry = {"threadId": target, "armGeneration": generation, "attempts": 0}
                    entries.append(entry)
                    by_id[target] = entry
                elif not (entry.get("status") == "not_started" and entry.get("armGeneration") == generation
                          and int(entry.get("attempts") or 0) < MAX_NOT_STARTED_ATTEMPTS):
                    continue
                entry.update(status="unknown", reason="delivery interrupted", attempts=int(entry.get("attempts") or 0) + 1)
                deliver.append(target)
            state["armed"] = False
            state.pop("retryPending", None)
            state["finalizePending"] = {"armGeneration": generation, "cycleId": cycle_id,
                                        "targets": list(targets)}
            state["lastRun"] = {"cycleId": cycle_id, "at": self.clock(),
                                "targets": [copy.deepcopy(by_id[target]) if target in by_id else
                                            {"threadId": target, "status": "skipped", "reason": "cycle target ledger is full"}
                                            for target in targets]}
            if not self.save():
                state.clear()
                state.update(previous)
                return {"cycleId": cycle_id, "at": self.clock(), "targets": [
                    {"threadId": target, "status": "skipped", "reason": "could not persist queue claim"}
                    for target in targets]}
        if deliver:
            for thread_id in deliver:
                with self.lock:
                    if self.action_state().get("armGeneration") != generation:
                        break
                status, reason = self._queue_one(thread_id)
                with self.lock:
                    current = self.action_state()
                    current_cycle = next((row for row in self._ledger(current)["cycles"]
                                          if row.get("cycleId") == cycle_id), None)
                    if current_cycle is None:
                        break
                    entry = next((row for row in current_cycle["targets"] if row.get("threadId") == thread_id), None)
                    if entry is None or entry.get("armGeneration") != generation:
                        break
                    previous_result = copy.deepcopy(current)
                    entry.update(status=status, reason=reason)
                    if current.get("armGeneration") == generation:
                        selected = {row.get("threadId"): row for row in current_cycle["targets"]}
                        current["lastRun"]["targets"] = [copy.deepcopy(selected[target]) if target in selected else
                                                         {"threadId": target, "status": "skipped", "reason": "cycle target ledger is full"}
                                                         for target in targets]
                        retryable = any(row.get("threadId") in targets and row.get("armGeneration") == generation
                                        and row.get("status") == "not_started"
                                        and int(row.get("attempts") or 0) < MAX_NOT_STARTED_ATTEMPTS
                                        for row in current_cycle["targets"])
                        current["armed"] = retryable
                        current["finalizePending"]["retryable"] = retryable
                        if retryable:
                            current["retryPending"] = True
                            current["retryAfter"] = self.clock() + NOT_STARTED_RETRY_SECONDS
                        else:
                            current.pop("retryPending", None)
                    if not self.save():
                        current.clear()
                        current.update(previous_result)
                        break
        with self.lock:
            current = self.action_state()
            current_cycle = next((row for row in self._ledger(current)["cycles"] if row.get("cycleId") == cycle_id), cycle)
            selected = {row.get("threadId"): row for row in current_cycle.get("targets", [])}
            run = {"cycleId": cycle_id, "armGeneration": generation, "at": self.clock(),
                   "targets": [copy.deepcopy(selected[target]) if target in selected else
                               {"threadId": target, "status": "skipped", "reason": "cycle target ledger is full"}
                               for target in targets]}
            if current.get("armGeneration") == generation:
                current["lastRun"] = copy.deepcopy(run)
                current.pop("finalizePending", None)
                self.save()
            return run

    def _queue_one(self, thread_id: str) -> tuple[str, str | None]:
        if self.inspect_target is None or self.queue_continue is None:
            return "skipped", "Codex chat continuation is unavailable"
        try:
            info = self.inspect_target(thread_id)
        except SnapshotShutdown:
            return "skipped", "chat inspection cancelled during shutdown"
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
        except ProviderPolicyError:
            return "skipped", "Codex continuation is not permitted"
        try:
            result = self.queue_continue(thread_id)
        except CodexNotStarted:
            return "not_started", "queue did not start"
        except Exception:
            return "unknown", "queue result is unknown"
        if not isinstance(result, dict):
            return "unknown", "queue result is unknown"
        status = result.get("status")
        if status == "queued":
            return "queued", None
        if status == "not_started":
            return "not_started", "queue did not start"
        if status == "rejected":
            return "rejected", "queue rejected the continuation"
        return "unknown", "queue result is unknown"
