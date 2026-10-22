"""Bounded Web Push delivery and read-only lifecycle polling for phone clients."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .phone_events import NotificationEvents


LOG = logging.getLogger("token_tracker.phone.push")
POLL_SECONDS = 2.0
FULL_REFRESH_SECONDS = 120.0
FULL_RETRY_DELAYS = (2.0, 4.0, 8.0, 15.0)
STRUCTURE_REFRESH_INTERVAL = 10.0
SUBSCRIBER_LIMIT = 20
PUSH_TIMEOUT_SECONDS = 5
MAX_RETRIES = 2
MAX_RETRY_WAIT = 5.0


class PhonePushService:
    """Observe complete healthy desktop samples and send privacy-safe events."""

    def __init__(self, store, *, desktop_origin: str, sender=None, clock=time.monotonic,
                 wait=None, event_engine=None, refresh_gate=None):
        self.store = store
        self.desktop_origin = desktop_origin.rstrip("/")
        self.sender = sender or (lambda subscription, event: send_web_push(store, subscription, event))
        self.clock = clock
        self.events = event_engine or NotificationEvents()
        self.refresh_gate = refresh_gate
        self.stop_event = threading.Event()
        self.wait = wait or self.stop_event.wait
        self.thread: threading.Thread | None = None
        self.full_snapshot = None
        self.last_full_at: float | None = None
        self.next_full_retry_at: float | None = None
        self.full_retry_step = 0
        self.last_structural_refresh = float("-inf")

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="TokenTrackerPhonePush", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=POLL_SECONDS + PUSH_TIMEOUT_SECONDS + 1)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                if self.store.subscriptions():
                    self.poll_once()
            except Exception as exc:
                LOG.warning("Phone notification poll failed (%s)", type(exc).__name__)
            self.stop_event.wait(POLL_SECONDS)

    def poll_once(self, *, observed_at=None) -> list[dict]:
        """One bounded poll, exposed for deterministic tests and controlled callers."""
        now = self.clock()
        baseline_before_poll = self.full_snapshot
        full_at_before_poll = self.last_full_at
        retry_wait = self.next_full_retry_at is not None and now < self.next_full_retry_at
        refresh_due = (self.last_full_at is None or now - self.last_full_at >= FULL_REFRESH_SECONDS
                       or self.next_full_retry_at is not None and not retry_wait)
        full_failed = retry_wait
        if refresh_due and not retry_wait:
            full_failed = not self._refresh_full(now)

        live = _get_json(self.desktop_origin + "/api/live", timeout=5)
        if not _valid_live(live) or self.full_snapshot is None or full_failed:
            self.events.observe([], observed_at or datetime.now(timezone.utc), healthy=False)
            return []

        full_stamp = self.full_snapshot.get("structureStamp")
        live_stamp = live.get("structureStamp")
        if full_stamp is not None and live_stamp is not None and full_stamp != live_stamp:
            if self.refresh_gate is not None:
                force = bool(self.refresh_gate())
            else:
                force = now - self.last_structural_refresh >= STRUCTURE_REFRESH_INTERVAL
                if force:
                    self.last_structural_refresh = now
            if not force:
                self.events.observe([], observed_at or datetime.now(timezone.utc), healthy=False)
                return []
            query = "?refresh=1"
            refreshed = _get_json(self.desktop_origin + "/api/stats" + query, timeout=20)
            refreshed_stamp = refreshed.get("structureStamp") if _valid_stats(refreshed) else None
            structure_matches = not (refreshed_stamp is not None and live_stamp is not None and refreshed_stamp != live_stamp)
            if _valid_stats(refreshed) and structure_matches:
                self.full_snapshot = refreshed
                self.last_full_at = now
                self.next_full_retry_at = None
                self.full_retry_step = 0
            else:
                self.full_snapshot = baseline_before_poll
                self.last_full_at = full_at_before_poll
                full_failed = True
                self._schedule_full_retry(now)
            if full_failed:
                self.events.observe([], observed_at or datetime.now(timezone.utc), healthy=False)
                return []

        chats = _normalized_chats(self.full_snapshot, live)
        events = self.events.observe(chats, observed_at or datetime.now(timezone.utc), healthy=True)
        for event in events:
            if event:
                self._dispatch(event)
        return events

    def _refresh_full(self, now: float) -> bool:
        snapshot = _get_json(self.desktop_origin + "/api/stats", timeout=20)
        if not _valid_stats(snapshot):
            self._schedule_full_retry(now)
            return False
        self.full_snapshot = snapshot
        self.last_full_at = now
        self.next_full_retry_at = None
        self.full_retry_step = 0
        return True

    def _schedule_full_retry(self, now: float) -> None:
        delay = FULL_RETRY_DELAYS[min(self.full_retry_step, len(FULL_RETRY_DELAYS) - 1)]
        self.next_full_retry_at = now + delay
        self.full_retry_step = min(self.full_retry_step + 1, len(FULL_RETRY_DELAYS) - 1)

    def _dispatch(self, event: dict) -> None:
        subscriptions = self.store.subscriptions()[:SUBSCRIBER_LIMIT]
        if not subscriptions:
            return
        for subscription in subscriptions:
            if self.stop_event.is_set():
                return
            endpoint = subscription.get("endpoint")
            if not isinstance(endpoint, str):
                continue
            result = self._send_with_retry(subscription, event)
            if result in {404, 410}:
                self.store.remove_endpoint(endpoint)

    def _send_with_retry(self, subscription: dict, event: dict) -> int | None:
        for attempt in range(MAX_RETRIES + 1):
            if self.stop_event.is_set():
                return None
            try:
                result = self.sender(subscription, event)
                status = _response_status(result)
                if status in {404, 410}:
                    return status
                if status in {429, 503} and attempt < MAX_RETRIES:
                    delay = _retry_after(result, fallback=min(MAX_RETRY_WAIT, 2 ** attempt))
                    self.wait(delay)
                    continue
                if status is not None and status >= 400:
                    LOG.warning("Phone push delivery failed (HTTP %d)", status)
                return status
            except Exception as exc:
                status = _exception_status(exc)
                if status in {404, 410}:
                    return status
                if status in {429, 503} and attempt < MAX_RETRIES:
                    self.wait(_retry_after(exc, fallback=min(MAX_RETRY_WAIT, 2 ** attempt)))
                    continue
                # Exception messages may contain endpoint URLs, payloads, or keys.
                LOG.warning("Phone push delivery failed (%s)", type(exc).__name__)
                return status
        return None


def _get_json(url: str, *, timeout: float):
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                return None
            result = json.loads(payload)
            return result if isinstance(result, dict) else None
    except (HTTPError, URLError, OSError, TimeoutError, ValueError):
        return None


def _valid_stats(value) -> bool:
    return isinstance(value, dict) and isinstance(value.get("providers"), list)


def _valid_live(value) -> bool:
    return isinstance(value, dict) and isinstance(value.get("chats"), list)


def _normalized_chats(snapshot: dict, live: dict) -> list[dict]:
    rows = {}
    for provider in snapshot.get("providers", []):
        if not isinstance(provider, dict) or not isinstance(provider.get("chats"), list):
            continue
        provider_name = provider.get("name")
        if not isinstance(provider_name, str):
            continue
        for chat in provider["chats"]:
            if not isinstance(chat, dict) or chat.get("id") in (None, ""):
                continue
            key = f"{provider_name}:{chat['id']}"
            rows[key] = {**chat, "provider": provider_name, "key": key}
    for chat in live.get("chats", []):
        if not isinstance(chat, dict):
            continue
        key = chat.get("key")
        if not isinstance(key, str) or ":" not in key:
            continue
        if key in rows:
            rows[key].update(chat)
        else:
            # Live-only roots are valid if the feed provides provider and id fields.
            rows[key] = dict(chat)
    return list(rows.values())


def send_web_push(store, subscription: dict, event: dict):
    """Send one encrypted notification using locally persisted VAPID credentials."""
    from pywebpush import webpush
    from py_vapid import Vapid

    try:
        private_key = store.push_private_key()
        if not private_key:
            raise RuntimeError("VAPID key unavailable")
        vapid = Vapid.from_pem(private_key.encode("ascii"))
        return webpush(
            subscription_info=subscription,
            data=json.dumps(event, separators=(",", ":")),
            vapid_private_key=vapid,
            vapid_claims={"sub": __import__("os").environ.get("TOKEN_TRACKER_VAPID_SUBJECT", "")},
            timeout=PUSH_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        # Retain status/headers only in the wrapper so callers can handle provider conditions.
        status = _exception_status(exc)
        if status is not None:
            raise _PushStatusError(status, _retry_after(exc)) from None
        raise RuntimeError(type(exc).__name__) from None


class _PushStatusError(RuntimeError):
    def __init__(self, status: int, retry_after: float):
        super().__init__("web push delivery failed")
        self.status_code = status
        self.retry_after_seconds = retry_after


def _response_status(value) -> int | None:
    if isinstance(value, int):
        return value
    status = getattr(value, "status_code", None) or getattr(value, "status", None)
    return status if isinstance(status, int) else None


def _exception_status(exc) -> int | None:
    if isinstance(exc, HTTPError):
        return exc.code
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _retry_after(value, *, fallback: float = 1.0) -> float:
    explicit = getattr(value, "retry_after_seconds", None)
    if isinstance(explicit, (int, float)):
        return max(0.0, min(MAX_RETRY_WAIT, float(explicit)))
    headers = getattr(value, "headers", None) or getattr(getattr(value, "response", None), "headers", None)
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None:
        return max(0.0, min(MAX_RETRY_WAIT, fallback))
    try:
        delay = float(raw)
    except (ValueError, TypeError):
        try:
            delay = (parsedate_to_datetime(raw).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            delay = fallback
    return max(0.0, min(MAX_RETRY_WAIT, delay))
