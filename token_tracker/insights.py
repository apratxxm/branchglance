"""Read-only Insights aggregation over the cached provider snapshot."""
from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

RANGES = frozenset({"24h", "7d", "30d", "all"})
MAX_LIMIT_PEAK_BYTES = 512 * 1024
MAX_LIMIT_PEAK_DAYS = 366
_STORE_LOCKS_GUARD = threading.Lock()
_STORE_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve(strict=False))
    with _STORE_LOCKS_GUARD:
        return _STORE_LOCKS.setdefault(key, threading.RLock())


def _owner_check(check: Callable[[], bool]) -> bool:
    try:
        return bool(check())
    except Exception:
        return False


def _instance_mutex_held() -> bool:
    try:
        from . import widget
        return getattr(widget, "_INSTANCE_HANDLE", None) is not None
    except Exception:
        return False


def _local_now(now: datetime | None = None) -> datetime:
    if now is None:
        from .providers import _local_timezone
        return datetime.now(_local_timezone())
    from .providers import _local_timezone
    if now.tzinfo is None:
        return now.replace(tzinfo=_local_timezone())
    return now.astimezone(_local_timezone())


def _hour_key(epoch: float, tz) -> str:
    return datetime.fromtimestamp(epoch, tz).replace(minute=0, second=0, microsecond=0).isoformat()


def _epoch(value: Any) -> float | None:
    from .providers import _timestamp_epoch
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    return _timestamp_epoch(value)


def _range_window(range_name: str, now: datetime) -> dict[str, Any]:
    today = now.date()
    if range_name == "24h":
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        hours = [current_hour - timedelta(hours=offset) for offset in reversed(range(24))]
        previous = [hour - timedelta(hours=24) for hour in hours]
        start_epoch = hours[0].timestamp()
        end_epoch = now.timestamp()
        previous_start, previous_end = start_epoch - 24 * 3600, start_epoch
        return {"grain": "hour", "keys": [hour.isoformat() for hour in hours],
                "previousKeys": [hour.isoformat() for hour in previous],
                "start": start_epoch, "end": end_epoch,
                "previousStart": previous_start, "previousEnd": previous_end,
                "dates": {hour.date().isoformat() for hour in hours}}
    days = 31 if range_name == "all" else int(range_name[:-1])
    dates = [(today - timedelta(days=offset)).isoformat() for offset in reversed(range(days))]
    previous_dates = [(today - timedelta(days=days + offset)).isoformat()
                      for offset in reversed(range(days))]
    day_start = datetime.combine(date.fromisoformat(dates[0]), datetime.min.time(), now.tzinfo)
    day_end = now
    prev_start = datetime.combine(date.fromisoformat(previous_dates[0]), datetime.min.time(), now.tzinfo)
    return {"grain": "day", "keys": dates, "previousKeys": previous_dates,
            "start": day_start.timestamp(), "end": day_end.timestamp(),
            "previousStart": prev_start.timestamp(), "previousEnd": day_start.timestamp(),
            "dates": set(dates)}


def _models_in_window(data: dict[str, Any], grain: str, keys: list[str]) -> dict[str, dict[str, int]]:
    source = data.get("hourlyModels" if grain == "hour" else "dailyModels") or {}
    result: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for bucket in keys:
        models = source.get(bucket) or {}
        for model, usage in models.items():
            if not isinstance(usage, dict):
                continue
            for key, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    result[str(model)][key] += int(value)
    return {model: dict(values) for model, values in result.items()}


def _model_totals(models: dict[str, dict[str, int]]) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    for usage in models.values():
        for key, value in usage.items():
            totals[key] += int(value or 0)
    return dict(totals)


def _cost_for(name: str, models: dict[str, dict[str, int]], *,
              fallback_tokens: int = 0) -> tuple[float | None, list[str]]:
    """Priced-model API-equivalent cost plus the names of models without a price row.

    The cost sums only priced models; it is None only when no model in ``models`` is priced
    (or when there are no per-model rows but tokens exist, so nothing can be priced).
    """
    from . import providers
    total = _model_totals(models)
    if not models:
        return (0.0 if fallback_tokens <= 0 else None), []
    if not total.get("total_tokens"):
        return 0.0, []
    table = providers.CLAUDE_PRICING if name == "Claude" else providers.CODEX_PRICING
    unpriced = sorted(model for model in models if not providers._price_for(model, table))
    if len(unpriced) == len(models):
        return None, unpriced
    cost = providers._costs(total, models, table, claude=name == "Claude")
    return cost.get("apiEquivalentCost"), unpriced


def _cache_hit_rate(name: str, totals: dict[str, int]) -> float | None:
    if name == "Claude":
        denominator = sum(totals.get(key, 0) for key in
                          ("input_tokens", "cached_tokens", "cache_5m_tokens", "cache_1h_tokens"))
    else:
        denominator = totals.get("input_tokens", 0)
    if denominator <= 0:
        return None
    return round(totals.get("cached_tokens", 0) * 100.0 / denominator, 1)


def _sessions_in_window(data: dict[str, Any], window: dict[str, Any], grain: str) -> tuple[int, int]:
    roots, subagents = set(), set()
    for session in data.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        stamp = _epoch(session.get("lastActivityAt"))
        if stamp is None or not window["start"] <= stamp < window["end"]:
            continue
        identity = session.get("id")
        if not isinstance(identity, str):
            continue
        (subagents if session.get("subagent") or session.get("parentId") else roots).add(identity)
    return len(roots), len(subagents)


def _active_hours(data: dict[str, Any], window: dict[str, Any]) -> float:
    hours = set()
    for value in data.get("activeHours") or []:
        stamp = _epoch(value)
        if stamp is not None and window["start"] <= stamp < window["end"]:
            hours.add(str(value))
    return float(len(hours))


def _daily_activity(provider: dict[str, Any]) -> dict[str, int]:
    result = {}
    for item in provider.get("activity") or []:
        if isinstance(item, dict) and isinstance(item.get("date"), str):
            try:
                result[item["date"]] = int(item.get("value") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
    return result


def _valid_peak_day(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


class LimitPeakStore:
    """Bounded append journal of daily high-water limit observations."""

    def __init__(self, path: Path | None = None, *, owner_check: Callable[[], bool] = _instance_mutex_held,
                 max_bytes: int = MAX_LIMIT_PEAK_BYTES, max_days: int = MAX_LIMIT_PEAK_DAYS) -> None:
        if path is None:
            from .product import state_root
            path = state_root() / "insights-limit-peaks.jsonl"
        self.path = Path(path)
        self.owner_check = owner_check
        self.max_bytes = max_bytes
        self.max_days = max_days

    def _read_locked(self) -> dict[tuple[str, str], dict[str, Any]]:
        if not _owner_check(self.owner_check):
            return self._read_without_compaction_locked()
        try:
            if self.path.stat().st_size > self.max_bytes:
                self._compact_locked()
                if not _owner_check(self.owner_check):
                    return self._read_without_compaction_locked()
            lines = self.path.read_bytes().splitlines()
        except OSError:
            return {}
        peaks: dict[tuple[str, str], dict[str, Any]] = {}
        for line in lines:
            if len(line) > 2048:
                continue
            try:
                item = json.loads(line)
                provider, day = item["provider"], item["date"]
                if provider not in {"Claude", "Codex"} or not _valid_peak_day(day):
                    continue
                target = peaks.setdefault((provider, day), {"fiveHour": None, "weekly": None})
                for key in ("fiveHour", "weekly"):
                    value = item.get(key)
                    if (isinstance(value, (int, float)) and not isinstance(value, bool)
                            and 0 <= value <= 100 and math.isfinite(value)
                            and (target[key] is None or value > target[key])):
                        target[key] = float(value)
            except (ValueError, TypeError, KeyError):
                continue
        return peaks

    def _compact_locked(self) -> None:
        if not _owner_check(self.owner_check):
            return
        peaks = self._read_without_compaction_locked()
        if not peaks:
            if not _owner_check(self.owner_check):
                return
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        days = sorted({day for _, day in peaks})[-self.max_days:]
        keep = [{"provider": provider, "date": day, **peaks[(provider, day)]}
                for day in days for provider in ("Claude", "Codex") if (provider, day) in peaks]
        payload = b"".join((json.dumps(row, separators=(",", ":")) + "\n").encode() for row in keep)
        if not _owner_check(self.owner_check):
            return
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if not _owner_check(self.owner_check):
                return
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def _read_without_compaction_locked(self) -> dict[tuple[str, str], dict[str, Any]]:
        try:
            with self.path.open("rb") as handle:
                size = self.path.stat().st_size
                offset = max(0, size - self.max_bytes)
                if offset:
                    handle.seek(offset)
                    handle.readline()  # drop a partial first record
                lines = handle.read(self.max_bytes).splitlines()[-self.max_days * 16:]
        except OSError:
            return {}
        peaks: dict[tuple[str, str], dict[str, Any]] = {}
        for line in lines:
            if len(line) > 2048:
                continue
            try:
                item = json.loads(line)
                provider, day = item["provider"], item["date"]
                if provider not in {"Claude", "Codex"} or not _valid_peak_day(day):
                    continue
                target = peaks.setdefault((provider, day), {"fiveHour": None, "weekly": None})
                for key in ("fiveHour", "weekly"):
                    value = item.get(key)
                    if (isinstance(value, (int, float)) and not isinstance(value, bool)
                            and 0 <= value <= 100 and math.isfinite(value)):
                        target[key] = value if target[key] is None else max(target[key], value)
            except (ValueError, TypeError, KeyError):
                continue
        return peaks

    def observe(self, providers: list[dict[str, Any]], *, now: datetime | None = None) -> None:
        try:
            if not self.owner_check():
                return
        except Exception:
            return
        local_now = _local_now(now)
        day = local_now.date().isoformat()
        samples = []
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            name = provider.get("name")
            if name not in {"Claude", "Codex"}:
                continue
            values = {}
            for source, target in (("fiveHour", "fiveHour"), ("weekly", "weekly")):
                percent = (provider.get(source) or {}).get("percent")
                values[target] = (float(percent) if isinstance(percent, (int, float))
                                  and not isinstance(percent, bool) and 0 <= percent <= 100
                                  and math.isfinite(percent) else None)
            if any(value is not None for value in values.values()):
                samples.append({"provider": name, "date": day, **values})
        if not samples:
            return
        lock = _path_lock(self.path)
        with lock:
            try:
                if not _owner_check(self.owner_check):
                    return
                self.path.parent.mkdir(parents=True, exist_ok=True)
                existing = self._read_locked()
                additions = []
                for sample in samples:
                    prior = existing.get((sample["provider"], day), {})
                    row = {"provider": sample["provider"], "date": day,
                           "fiveHour": max(v for v in (prior.get("fiveHour"), sample["fiveHour"]) if v is not None)
                           if prior.get("fiveHour") is not None or sample["fiveHour"] is not None else None,
                           "weekly": max(v for v in (prior.get("weekly"), sample["weekly"]) if v is not None)
                           if prior.get("weekly") is not None or sample["weekly"] is not None else None}
                    if row != {"provider": sample["provider"], "date": day,
                               "fiveHour": prior.get("fiveHour"), "weekly": prior.get("weekly")}:
                        additions.append(row)
                        existing[(sample["provider"], day)] = row
                if additions:
                    if not _owner_check(self.owner_check):
                        return
                    with self.path.open("ab") as handle:
                        for row in additions:
                            handle.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
                        handle.flush()
                        os.fsync(handle.fileno())
                    if self.path.stat().st_size > self.max_bytes:
                        self._compact_locked()
            except OSError:
                return

    def peaks(self, providers: list[str], dates: set[str]) -> dict[str, dict[str, float | None]]:
        result = {name: {"fiveHour": None, "weekly": None} for name in providers}
        if not result:
            return result
        with _path_lock(self.path):
            rows = self._read_locked()
        for (provider, day), values in rows.items():
            if provider not in result or day not in dates:
                continue
            for field in ("fiveHour", "weekly"):
                value = values.get(field)
                if value is not None:
                    result[provider][field] = value if result[provider][field] is None else max(
                        result[provider][field], value)
        return result


def _default_store() -> LimitPeakStore:
    return LimitPeakStore()


def _fresh_limit_observations(providers: list[dict[str, Any]], *, now: datetime) -> list[tuple[dict[str, Any], datetime]]:
    now_utc = now.astimezone(timezone.utc)
    result = []
    for provider in providers:
        if (not isinstance(provider, dict) or provider.get("connected") is not True
                or provider.get("stale") is True):
            continue
        try:
            observed = datetime.fromisoformat(str(provider.get("limitsUpdatedAt", "")).replace("Z", "+00:00"))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            observed = observed.astimezone(timezone.utc)
            if observed > now_utc or now_utc - observed > timedelta(minutes=10):
                continue
            result.append((provider, _local_now(observed)))
        except (TypeError, ValueError, OverflowError):
            continue
    return result


def record_limit_peaks(snapshot: dict[str, Any], *, allowed_names: set[str],
                       now: datetime | None = None) -> None:
    """Sample limit percents from a freshly built snapshot. Owner-gated; never raises."""
    try:
        now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        candidates = [provider for provider in snapshot.get("providers") or []
                      if isinstance(provider, dict) and provider.get("name") in {"Claude", "Codex"}
                      and provider.get("name") in allowed_names]
        store = _default_store()
        for provider, observed in _fresh_limit_observations(candidates, now=now_utc):
            store.observe([provider], now=observed)
    except Exception:
        return


def _empty(range_name: str, generated_at: str | None = None) -> dict[str, Any]:
    return {"range": range_name, "generatedAt": generated_at or datetime.now(timezone.utc).isoformat(),
            "providers": []}


def build_insights(snapshot: dict[str, Any], range_name: str, *,
                   now: datetime | None = None, peak_store: LimitPeakStore | None = None,
                   allowed_names: set[str] | None = None) -> dict[str, Any]:
    if range_name not in RANGES:
        raise ValueError("invalid insights range")
    local_now = _local_now(now)
    window = _range_window(range_name, local_now)
    visible = [provider for provider in snapshot.get("providers") or []
               if provider.get("name") in {"Claude", "Codex"}
               and (allowed_names is None or provider.get("name") in allowed_names)]
    store = peak_store or LimitPeakStore()
    for provider, observed in _fresh_limit_observations(visible, now=local_now):
        store.observe([provider], now=observed)
    peak_dates = window["dates"]
    peaks = store.peaks([str(item["name"]) for item in visible], peak_dates)
    response_providers = []
    for provider in visible:
        name = provider["name"]
        data = provider.get("_insightsData") or {}
        hourly_source = data.get("hourlyModels")
        hourly_available = isinstance(hourly_source, dict)
        model_usage = _models_in_window(data, window["grain"], window["keys"])
        totals = _model_totals(model_usage)
        daily_activity = _daily_activity(provider)
        if totals.get("total_tokens") is None:
            if window["grain"] == "hour":
                totals["total_tokens"] = 0 if hourly_available else None
            else:
                totals["total_tokens"] = sum(daily_activity.get(day, 0) for day in window["dates"])
        series = []
        fallback_tokens = {}
        for day in window["dates"]:
            fallback_tokens[day] = daily_activity.get(day, 0)
        for bucket in window["keys"]:
            bucket_models = _models_in_window(data, window["grain"], [bucket])
            bucket_totals = _model_totals(bucket_models)
            tokens = bucket_totals.get("total_tokens")
            if tokens is None:
                tokens = (fallback_tokens.get(bucket, 0) if window["grain"] == "day"
                          else 0 if hourly_available else None)
            bucket_cost, bucket_unpriced = (_cost_for(name, bucket_models, fallback_tokens=int(tokens or 0))
                                            if tokens is not None else (None, []))
            row = {"date": bucket, "tokens": int(tokens) if tokens is not None else None,
                   "apiEquivalentCost": bucket_cost}
            if bucket_unpriced:
                row["costPartial"] = True
            series.append(row)
        sessions, subagents = _sessions_in_window(data, window, window["grain"])
        selected_hours = _active_hours(data, window)
        daily_models = _models_in_window(data, window["grain"], window["keys"])
        models_out = []
        for model, usage in sorted(daily_models.items(), key=lambda item: -item[1].get("total_tokens", 0)):
            cost, _unpriced = _cost_for(name, {model: usage})
            models_out.append({"name": model, "tokens": int(usage.get("total_tokens") or 0),
                               "apiEquivalentCost": cost})
        previous = None
        if range_name in {"24h", "7d"}:
            previous_models = _models_in_window(data, window["grain"], window["previousKeys"])
            previous_totals = _model_totals(previous_models)
            previous_tokens = previous_totals.get("total_tokens")
            if previous_tokens is None and window["grain"] == "day":
                previous_tokens = sum(daily_activity.get(day, 0) for day in window["previousKeys"])
            elif previous_tokens is None:
                previous_tokens = 0 if hourly_available else None
            previous_cost, previous_unpriced = (_cost_for(name, previous_models,
                                                          fallback_tokens=int(previous_tokens or 0))
                                                if previous_tokens is not None else (None, []))
            previous = {"tokens": int(previous_tokens) if previous_tokens is not None else None,
                        "apiEquivalentCost": previous_cost}
            if previous_unpriced:
                previous["costPartial"] = True
        total_cost, total_unpriced = (_cost_for(name, model_usage,
                                                fallback_tokens=int(totals.get("total_tokens") or 0))
                                     if totals.get("total_tokens") is not None else (None, []))
        totals_out = {
            "tokens": int(totals["total_tokens"]) if totals.get("total_tokens") is not None else None,
            "apiEquivalentCost": total_cost,
            "sessions": sessions, "subagentsActive": subagents,
            "activeHours": selected_hours,
            "cacheHitRate": _cache_hit_rate(name, totals),
        }
        if total_unpriced:
            totals_out["costPartial"] = True
            totals_out["unpricedModels"] = total_unpriced
        response_providers.append({
            "name": name,
            "totals": totals_out,
            "previous": previous,
            "series": series,
            "models": models_out,
            "limitPeaks": peaks.get(name, {"fiveHour": None, "weekly": None}),
        })
    return {"range": range_name,
            "generatedAt": snapshot.get("generatedAt") or local_now.astimezone(timezone.utc).isoformat(),
            "providers": response_providers}


class InsightsCache:
    TTL_SECONDS = 60.0

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.values: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self.building: set[tuple[str, str]] = set()

    def get(self, range_name: str, generation: str, *, allowed_names: set[str],
            snapshot_loader: Callable[[], dict[str, Any] | None],
            peak_store: LimitPeakStore | None = None) -> dict[str, Any] | None:
        if range_name not in RANGES:
            raise ValueError("invalid insights range")
        if not allowed_names:
            return _empty(range_name)
        key = (range_name, generation)
        with self.condition:
            now = time.monotonic()
            cached = self.values.get(key)
            if cached and now - cached[0] < self.TTL_SECONDS:
                return copy.deepcopy(cached[1])
            while key in self.building:
                self.condition.wait()
                now = time.monotonic()
                cached = self.values.get(key)
                if cached and now - cached[0] < self.TTL_SECONDS:
                    return copy.deepcopy(cached[1])
            self.building.add(key)
        try:
            snapshot = snapshot_loader()
            if snapshot is None:
                return None
            value = build_insights(snapshot, range_name, allowed_names=allowed_names,
                                   peak_store=peak_store)
            with self.condition:
                self.values[key] = (time.monotonic(), copy.deepcopy(value))
            return value
        finally:
            with self.condition:
                self.building.discard(key)
                self.condition.notify_all()


INSIGHTS_CACHE = InsightsCache()
