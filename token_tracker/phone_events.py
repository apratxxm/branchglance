"""Measured lifecycle notifications for the Token Tracker phone client.

``NotificationEvents.observe(chats, observed_at, healthy=True)`` accepts an
iterable of normalized chat dictionaries, or a mapping from chat identity to
those dictionaries, and returns zero or more push-ready event dictionaries.
Only lifecycle state and run identity are examined; prompt, path, context,
activity, and request-body fields are never copied to notifications.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping


_ACTIVE = {"running", "quiet", "compacting"}
_LAUNCHABLE = {"running", "compacting"}
_IN_FLIGHT = _ACTIVE | {"needs input"}
_TERMINAL = {"idle", "failed"}
_KNOWN = _ACTIVE | _TERMINAL | {"needs input"}
_GROUP_WINDOW_SECONDS = 3.0
_ENTITY_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_ENTITIES = 5000
_MAX_EMITTED_IDS = 10000


def _epoch(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except ValueError:
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _identity(value: Any) -> str | None:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        provider, chat_id = value
        if provider is not None and chat_id is not None:
            return f"{provider}:{chat_id}"
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _state(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().lower().replace("_", " ").replace("-", " ").split())
    return normalized if normalized in _KNOWN else None


def _digest(*parts: Any) -> str:
    material = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class NotificationEvents:
    """Convert observed state transitions into bounded, privacy-safe events.

    Constructor options are intentionally small: names remain disabled by
    default, and ``retention_seconds`` can tune how long absent entities are
    remembered. ``max_entities`` bounds per-process memory.
    """

    def __init__(
        self,
        *,
        include_names: bool = False,
        retention_seconds: float = _ENTITY_RETENTION_SECONDS,
        max_entities: int = _MAX_ENTITIES,
    ) -> None:
        self.include_names = bool(include_names)
        self.retention_seconds = max(0.0, float(retention_seconds))
        self.max_entities = max(1, int(max_entities))
        self._entities: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._pending: dict[str, dict[str, Any]] = {}
        self._last_observed: float | None = None
        self._emitted: OrderedDict[str, None] = OrderedDict()
        self._initialized = False

    def observe(
        self,
        chats: Iterable[dict[str, Any]] | Mapping[Any, dict[str, Any]],
        observed_at: Any,
        healthy: bool = True,
    ) -> list[dict[str, Any]]:
        """Return newly measured launch, completion, question, or failure events.

        Unhealthy observations do not alter baselines or release pending groups.
        Out-of-order observation times are ignored. Missing chats are retained
        silently until their retention limit; their disappearance is never a
        lifecycle event.
        """
        if not healthy:
            return []
        sample_time = _epoch(observed_at)
        if sample_time is None or (
            self._last_observed is not None and sample_time < self._last_observed
        ):
            return []

        output: list[dict[str, Any]] = []
        # A fixed group deadline is measured from its first transition.
        output.extend(self._flush_expired(sample_time))
        self._last_observed = sample_time

        items = chats.items() if isinstance(chats, Mapping) else ((None, row) for row in chats)
        normalized: list[tuple[str, dict[str, Any]]] = []
        for supplied_key, row in items:
            if not isinstance(row, dict):
                continue
            identity = _identity(row.get("key")) or _identity(supplied_key)
            if identity is None:
                provider = row.get("provider")
                chat_id = row.get("id")
                if provider is not None and chat_id is not None:
                    identity = f"{provider}:{chat_id}"
            if identity:
                normalized.append((identity, row))

        # The first healthy view seeds a quiet baseline and cannot create a
        # launch/completion flood. Urgent states already present are baseline too.
        if not self._initialized:
            self._initialized = True
            for identity, row in normalized:
                record = self._record(row, sample_time)
                if record is not None:
                    self._remember(identity, record, sample_time)
            self._prune(sample_time)
            return output

        for identity, row in normalized:
            current = self._record(row, sample_time)
            if current is None:
                continue
            previous = self._entities.get(identity)
            if previous is not None:
                # A stale state/run timestamp must never roll the entity back.
                old_stamp = previous.get("state_epoch")
                new_stamp = current.get("state_epoch")
                if old_stamp is not None and new_stamp is not None and new_stamp < old_stamp:
                    continue

            self._remember(identity, current, sample_time)
            if previous is None:
                if current["status"] in _LAUNCHABLE:
                    self._queue_transition("launch", identity, current, sample_time)
                elif current["status"] == "needs input":
                    output.extend(self._immediate("question", identity, current, sample_time))
                elif current["status"] == "failed":
                    output.extend(self._immediate("failure", identity, current, sample_time))
                continue

            old_status = previous["status"]
            new_status = current["status"]
            old_run = previous.get("run_token")
            new_run = current.get("run_token")
            run_changed = bool(new_run and old_run and new_run != old_run)

            if run_changed and new_status in _LAUNCHABLE:
                self._queue_transition("launch", identity, current, sample_time)
            elif old_status in _TERMINAL and new_status in _LAUNCHABLE:
                self._queue_transition("launch", identity, current, sample_time)
            elif old_status == "quiet" and new_status == "running":
                self._queue_transition("launch", identity, current, sample_time)

            question_advanced = (
                current.get("question_id") is not None
                and current.get("question_id") != previous.get("question_id")
            ) or (
                current.get("has_state_timestamp")
                and previous.get("has_state_timestamp")
                and current["state_epoch"] > previous["state_epoch"]
            )
            if new_status == "needs input" and (
                old_status != new_status or question_advanced or run_changed
            ):
                output.extend(self._immediate("question", identity, current, sample_time))
            elif old_status != new_status and new_status == "failed":
                output.extend(self._immediate("failure", identity, current, sample_time))
            elif old_status in _IN_FLIGHT and new_status == "idle":
                self._queue_transition("completion", identity, current, sample_time)

        self._prune(sample_time)
        return output

    @staticmethod
    def _record(row: dict[str, Any], sample_time: float) -> dict[str, Any] | None:
        status = _state(row.get("actualStatus")) or _state(row.get("status"))
        if status is None:
            return None
        raw_state_stamp = row.get("stateSince")
        if raw_state_stamp is None:
            raw_state_stamp = row.get("statusSince")
        parsed_state_stamp = _epoch(raw_state_stamp) if raw_state_stamp is not None else None
        state_epoch = parsed_state_stamp if parsed_state_stamp is not None else sample_time
        if state_epoch is not None and state_epoch > sample_time:
            return None
        raw_run_start = row.get("runStartedAt")
        run_start = _epoch(raw_run_start) if raw_run_start is not None else None
        if run_start is not None and run_start > sample_time:
            return None
        request_id = row.get("latestRequestId")
        question_id = row.get("questionId") or row.get("pendingQuestionId")
        run_token = ("request", str(request_id)) if request_id not in (None, "") else (
            ("start", run_start) if run_start is not None else None
        )
        title = row.get("displayName") or row.get("currentName") or row.get("title") or row.get("name")
        if not isinstance(title, str):
            title = ""
        return {
            "status": status,
            "state_epoch": state_epoch,
            "state_key": state_epoch,
            "has_state_timestamp": parsed_state_stamp is not None,
            "run_token": run_token,
            "question_id": str(question_id) if question_id not in (None, "") else None,
            "title": " ".join(title.split())[:80],
        }

    def _remember(self, identity: str, record: dict[str, Any], sample_time: float) -> None:
        self._entities[identity] = {**record, "last_seen": sample_time}
        self._entities.move_to_end(identity)

    def _prune(self, sample_time: float) -> None:
        expiry = sample_time - self.retention_seconds
        for identity in list(self._entities):
            if self._entities[identity]["last_seen"] < expiry:
                del self._entities[identity]
        while len(self._entities) > self.max_entities:
            self._entities.popitem(last=False)

    def _queue_transition(self, kind: str, identity: str, record: dict[str, Any], sample_time: float) -> None:
        event_id = _digest(identity, record.get("run_token"), kind, record["status"], record["state_key"])
        group = self._pending.get(kind)
        if group is None:
            group = {"deadline": sample_time + _GROUP_WINDOW_SECONDS, "first_at": sample_time, "items": OrderedDict()}
            self._pending[kind] = group
        group["items"].setdefault(event_id, {"identity": identity, "title": record["title"], "status": record["status"]})

    def _immediate(self, kind: str, identity: str, record: dict[str, Any], sample_time: float) -> list[dict[str, Any]]:
        event_id = _digest(
            identity,
            record.get("run_token"),
            kind,
            record["status"],
            record["state_key"],
            record.get("question_id") if kind == "question" else None,
        )
        item = {"identity": identity, "title": record["title"], "status": record["status"]}
        tag = f"tt-{kind}-{_digest(identity, record.get('run_token'))[:16]}"
        event = self._event(kind, [item], event_id, sample_time, tag=tag)
        return [event] if event is not None else []

    def _flush_expired(self, sample_time: float) -> list[dict[str, Any]]:
        output = []
        for kind, group in list(self._pending.items()):
            if sample_time >= group["deadline"]:
                group_id = _digest(kind, group["first_at"], *group["items"].keys())
                event = self._event(kind, list(group["items"].values()), group_id, group["first_at"])
                if event is not None:
                    output.append(event)
                del self._pending[kind]
        return output

    def _event(
        self,
        kind: str,
        items: list[dict[str, Any]],
        identity_hash: str,
        at: float,
        *,
        tag: str | None = None,
    ) -> dict[str, Any] | None:
        if identity_hash in self._emitted:
            return None
        self._emitted[identity_hash] = None
        self._emitted.move_to_end(identity_hash)
        while len(self._emitted) > _MAX_EMITTED_IDS:
            self._emitted.popitem(last=False)

        topics = {"launch": "runs", "completion": "done", "question": "question", "failure": "failure"}
        titles = {"launch": "Work started", "completion": "Work completed", "question": "A chat needs your input", "failure": "A chat stopped with an error"}
        count = len(items)
        noun = "chat" if count == 1 else "chats"
        state = {"launch": "started", "completion": "completed", "question": "needs input", "failure": "failed"}[kind]
        if self.include_names:
            names = [item["title"] or item["identity"] for item in items[:3]]
            label = ", ".join(self._safe_name(name) for name in names)
            if count > 3:
                label += f" and {count - 3} more"
            body = f"{label} {state}." if count == 1 else f"{count} {noun} changed state; {label}."
        else:
            body = f"{count} {noun} {state}."
        body = body[:180]
        tag = tag or f"tt-{topics[kind]}"
        event = {
            "id": identity_hash,
            "kind": kind,
            "title": titles[kind][:80],
            "body": body,
            "url": "/phone.html",
            "tag": re.sub(r"[^a-zA-Z0-9_-]", "", tag)[:32],
            "observedAt": datetime.fromtimestamp(at, timezone.utc).isoformat(),
        }
        return event

    @staticmethod
    def _safe_name(name: str) -> str:
        # Display names only; strip controls and collapse any accidental long copy.
        cleaned = "".join(ch for ch in name if ch.isprintable())
        return " ".join(cleaned.split())[:80] or "A chat"
