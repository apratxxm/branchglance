"""Ledger purge: one provider's saved history, the purge queue and worker, and POST /api/ledger/purge."""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from test_ledger_support import (  # ledger_guard is autouse
    DAY_END, HOUR_START, claude_file, claude_response, clock_at, codex_count, codex_file, codex_meta,
    ledger_guard, make_roots, write_jsonl,
)

from token_tracker import ledger_service
from token_tracker import ledger_store as store
from token_tracker import server
from token_tracker.ledger import Ledger
from token_tracker.ledger_service import ENV_FLAG, LedgerService, PurgeTicket
from token_tracker.provider_policy import ProviderPolicy

ALL = {"Claude": True, "Codex": True}
PROVIDER_TABLES = ("facts", "hourly", "sources", "threads", "gaps", "counters", "overrides",
                   "provisional_facts", "source_facts", "ledger_omissions", "ledger_message_evidence",
                   "ledger_unknown_kinds")
TOKEN = {"X-Branchglance-Settings-Token": "purge-test-token"}


@pytest.fixture(autouse=True)
def no_startup_delay(monkeypatch):
    # The widget's 60 s ledger start delay is covered in test_ledger_service; purge tests start at once.
    monkeypatch.setattr(ledger_service, "STARTUP_DELAY_SECONDS", 0)


@pytest.fixture(autouse=True)
def restore_current_service(monkeypatch):
    # LedgerService.start() publishes itself as the current service; keep that from leaking across tests.
    monkeypatch.setattr(ledger_service, "_CURRENT", None)
    yield


# ------------------------------------------------------------------ helpers

def make_ledger(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [claude_response(60, "m1", output=5), claude_response(120, "m2", output=7)])
    write_jsonl(codex_file(codex, "a"), codex_meta("thread-a", 30) + [codex_count(90, input_tokens=100, output_tokens=10)])
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    ledger.ingest({"Claude": True, "Codex": True})
    return ledger, data


def provider_state(conn, provider):
    """Every row and provider-scoped coverage key that belongs to one provider, for exact comparison."""
    state = {}
    for table in PROVIDER_TABLES:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            state[table] = sorted(conn.execute(f"SELECT * FROM {table} WHERE provider=?", (provider,)).fetchall())
    state["meta"] = sorted(row for row in conn.execute("SELECT key, value FROM meta").fetchall()
                           if row[0].endswith(":" + provider))
    return state


def meta_keys(conn):
    return {row[0] for row in conn.execute("SELECT key FROM meta")}


def count(conn, table, provider):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE provider=?", (provider,)).fetchone()[0]


def requests_in(ledger, provider):
    return sum(row["requests"] for row in ledger.summary(HOUR_START, DAY_END, show=ALL)["rows"]
               if row["provider"] == provider)


def seed_claude_extras(ledger):
    """Rows that not every ingest creates, so purge is shown to remove them as well."""
    ledger.set_override("Claude", "thread-x", "build")
    conn = ledger.conn
    conn.execute("INSERT INTO ledger_omissions(provider, identity, source_key, message_key, event_epoch) "
                 "VALUES('Claude', 'omitted-1', 'src', 'msg', 1.0)")
    conn.execute("INSERT INTO ledger_message_evidence(provider, message_key, valid_epoch) VALUES('Claude', 'msg', 1.0)")
    conn.execute("INSERT INTO ledger_unknown_kinds(provider, kind, event_key) VALUES('Claude', 'mystery', 'ev-1')")
    store.meta_set(conn, "pendingBytes:Claude", 4096)
    store.meta_set(conn, "verifyingBytes:Claude", 4096)
    store.meta_set(conn, "verifyingBytes", 4096)  # the global aggregate the ingest would have written


# ------------------------------------------------------------------ store and facade

def test_purge_removes_every_claude_table_and_meta_key_and_leaves_codex_identical(tmp_path):
    ledger, _data = make_ledger(tmp_path)
    seed_claude_extras(ledger)
    conn = ledger.conn
    assert count(conn, "facts", "Claude") > 0 and count(conn, "facts", "Codex") > 0
    codex_before = provider_state(conn, "Codex")
    assert codex_before["facts"] and codex_before["meta"]
    assert any(key.endswith(":Claude") for key in meta_keys(conn))

    ledger.purge("Claude")

    for table in PROVIDER_TABLES:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            assert count(conn, table, "Claude") == 0, table
    assert not [key for key in meta_keys(conn) if key.endswith(":Claude")]
    assert provider_state(conn, "Codex") == codex_before
    assert requests_in(ledger, "Codex") > 0 and requests_in(ledger, "Claude") == 0


def test_purge_recomputes_global_aggregates_and_clears_verifying(tmp_path):
    ledger, _data = make_ledger(tmp_path)
    seed_claude_extras(ledger)
    conn = ledger.conn
    store.meta_set(conn, "pendingBytes:Codex", 128)
    store.meta_set(conn, "verifyingBytes:Codex", 0)
    assert int(store.meta_value(conn, "verifyingBytes")) >= 4096

    ledger.purge("Claude")

    assert int(store.meta_value(conn, "pendingBytes")) == 128
    assert int(store.meta_value(conn, "verifyingBytes")) == 0
    coverage = ledger.coverage()
    assert coverage["verifyingBytes"] == 0 and coverage["status"] != "verifying"
    assert store.meta_value(conn, "observedFrom:Claude") is None


def test_purging_every_provider_drops_the_global_clock(tmp_path):
    ledger, _data = make_ledger(tmp_path)
    conn = ledger.conn
    assert store.meta_value(conn, "observedFrom") is not None

    ledger.purge("Claude")
    assert store.meta_value(conn, "observedFrom") is not None  # Codex facts remain
    ledger.purge("Codex")

    assert store.meta_value(conn, "observedFrom") is None
    assert store.meta_value(conn, "lastIngestedAt") is None
    assert store.meta_value(conn, "pendingBytes") == "0"
    assert store.meta_value(conn, "verifyingBytes") == "0"


def test_purge_rolls_back_everything_on_injected_failure(tmp_path, monkeypatch):
    ledger, _data = make_ledger(tmp_path)
    seed_claude_extras(ledger)
    conn = ledger.conn
    before = provider_state(conn, "Claude")
    globals_before = (store.meta_value(conn, "pendingBytes"), store.meta_value(conn, "verifyingBytes"),
                      store.meta_value(conn, "revision"))
    real_purge = store.purge_provider

    def delete_then_fail(connection, provider):
        real_purge(connection, provider)  # every delete has run inside the transaction
        raise RuntimeError("injected after delete")

    monkeypatch.setattr(store, "purge_provider", delete_then_fail)
    with pytest.raises(RuntimeError, match="injected after delete"):
        ledger.purge("Claude")

    assert provider_state(conn, "Claude") == before
    assert (store.meta_value(conn, "pendingBytes"), store.meta_value(conn, "verifyingBytes"),
            store.meta_value(conn, "revision")) == globals_before
    assert conn.in_transaction is False


def test_purge_rejects_unknown_provider(tmp_path):
    ledger, _data = make_ledger(tmp_path)
    with pytest.raises(ValueError, match="unknown provider"):
        ledger.purge("Other")


def test_purge_clears_only_that_providers_partial_verification_cache(tmp_path):
    ledger, _data = make_ledger(tmp_path)
    cache = ledger._ingestor._partial_verification
    cache[("Claude", "path-a")] = {"position": 10}
    cache[("Claude", "path-b")] = {"position": 20}
    cache[("Codex", "path-c")] = {"position": 30}

    ledger.purge("Claude")

    assert cache == {("Codex", "path-c"): {"position": 30}}


def test_purge_removes_quarantined_database_copies_and_nothing_else(tmp_path):
    ledger, data = make_ledger(tmp_path)
    corrupt = data / (store.DB_NAME + ".corrupt-" + "a" * 32)
    corrupt_wal = data / (store.DB_NAME + ".corrupt-" + "a" * 32 + "-wal")
    corrupt.write_bytes(b"not a database")
    corrupt_wal.write_bytes(b"wal")

    ledger.purge("Claude")

    assert not corrupt.exists() and not corrupt_wal.exists()
    assert (data / "ledger-kinds.json").exists()
    assert ledger.db_path.exists()


# ------------------------------------------------------------------ queue and worker

class FakeLedger:
    """Stands in for Ledger in worker tests: records calls, and can block or fail a purge on purpose."""

    def __init__(self):
        self.events = []
        self.purge_gate = None  # threading.Event: a purge waits on it when set
        self.purge_error = None
        self.purge_errors = {}  # per-provider failures, taking precedence over purge_error
        self.ingests = 0
        self.entered = threading.Event()

    def ingest(self, collect, budget_bytes=None, should_stop=None):
        self.ingests += 1
        self.events.append("ingest")
        return {"filesVisited": 0, "bytesRead": 0, "pendingBytes": 0}

    def pending_bytes(self, show=None):
        return 0

    def purge(self, provider):
        self.events.append(("purge", provider))
        self.entered.set()
        if self.purge_gate is not None:
            self.purge_gate.wait(10)
        error = self.purge_errors.get(provider, self.purge_error)
        if error is not None:
            raise error

    def close(self):
        pass


class PreemptingLedger(FakeLedger):
    """The first pass polls should_stop the way a long pass does, and reports stopped once asked."""

    def ingest(self, collect, budget_bytes=None, should_stop=None):
        self.ingests += 1
        self.events.append("ingest")
        if self.ingests == 1:
            self.entered.set()
            deadline = time.monotonic() + 10
            while not should_stop():
                if time.monotonic() > deadline:
                    raise AssertionError("queued purge never reached should_stop")
                time.sleep(0.01)
            return {"filesVisited": 0, "bytesRead": 0, "pendingBytes": None, "stopped": True}
        return {"filesVisited": 0, "bytesRead": 0, "pendingBytes": 0}


def policies(*, claude_collect=True, codex_collect=False):
    flags = {"Claude": claude_collect, "Codex": codex_collect}
    return {name: ProviderPolicy(name=name, collect=collect, show=True, send=False, send_origin="default",
                                 capabilities=frozenset()) for name, collect in flags.items()}


def started_service(tmp_path, fake, *, claude_collect=True, codex_collect=False):
    data, claude, codex = make_roots(tmp_path)
    service = LedgerService(data_dir=data, claude_root=claude, codex_root=codex, environ={ENV_FLAG: "1"},
                            policies=policies(claude_collect=claude_collect, codex_collect=codex_collect),
                            ledger_factory=lambda *a, **k: fake)
    service.start()
    return service


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.005)


def test_queued_purge_wakes_an_idle_worker_without_waiting_for_the_cadence(tmp_path):
    fake = FakeLedger()
    service = started_service(tmp_path, fake)
    try:
        wait_until(lambda: "ingest" in fake.events)  # the worker is now in its 30 s wait
        began = time.monotonic()
        ticket = service.request_purge("Claude")
        assert ticket.wait(5) and ticket.status == "done"
        assert time.monotonic() - began < 5
        assert ("purge", "Claude") in fake.events
    finally:
        service.stop()


def test_requests_coalesce_per_provider_while_unresolved(tmp_path):
    fake = FakeLedger()
    fake.purge_gate = threading.Event()
    service = started_service(tmp_path, fake)
    try:
        first = service.request_purge("Claude")
        wait_until(fake.entered.is_set)  # running now
        assert service.request_purge("Claude") is first
        assert service.health()["purging"] == ["Claude"]
        fake.purge_gate.set()
        assert first.wait(5) and first.status == "done"
        assert fake.events.count(("purge", "Claude")) == 1
        assert service.health()["purging"] == []
    finally:
        fake.purge_gate.set()
        service.stop()


def test_queued_purge_ends_a_running_pass_then_runs_before_the_next_ingest(tmp_path):
    fake = PreemptingLedger()
    service = started_service(tmp_path, fake)
    try:
        wait_until(fake.entered.is_set)
        ticket = service.request_purge("Claude")
        assert ticket.wait(5) and ticket.status == "done"
        wait_until(lambda: fake.ingests >= 2)
        assert fake.events[:3] == ["ingest", ("purge", "Claude"), "ingest"]
    finally:
        service.stop()


def test_failed_purge_resolves_with_public_class_name_only(tmp_path):
    fake = FakeLedger()
    fake.purge_error = sqlite3.OperationalError("database is locked at C:/Users/secret/ledger.sqlite3")
    service = started_service(tmp_path, fake)
    try:
        ticket = service.request_purge("Claude")
        assert ticket.wait(5)
        assert ticket.status == "failed" and ticket.error == "OperationalError"
        assert "secret" not in json.dumps(service.health())
        assert service.health()["lastPurge"] == {"Claude": {"ticket": ticket.id, "result": "failed", "error": "OperationalError"}}
    finally:
        service.stop()


def test_stop_cancels_queued_waiters_and_deletes_nothing_more(tmp_path):
    fake = FakeLedger()
    fake.purge_gate = threading.Event()
    service = started_service(tmp_path, fake)
    try:
        running = service.request_purge("Claude")
        wait_until(fake.entered.is_set)
        queued = service.request_purge("Codex")  # waits behind the running purge
        service.stop(timeout=0.2)
        assert queued.wait(1) and queued.status == "cancelled"
        assert service.request_purge("Codex").status == "cancelled"  # after stop nothing queues
        assert ("purge", "Codex") not in fake.events
    finally:
        fake.purge_gate.set()
    assert running.wait(5) and running.status == "done"  # a running purge finishes its own transaction


def test_request_without_a_worker_resolves_cancelled_immediately(tmp_path):
    service = LedgerService(data_dir=tmp_path / "data", claude_root=tmp_path / "claude",
                            codex_root=tmp_path / "codex", environ={}, policies=policies())
    ticket = service.request_purge("Claude")
    assert ticket.wait(0) and ticket.status == "cancelled"
    assert service.health()["purging"] == []


def test_ticket_resolves_once():
    ticket = PurgeTicket("Claude")
    ticket.resolve("done")
    ticket.resolve("failed", "RuntimeError")
    assert ticket.status == "done" and ticket.error is None and ticket.wait(0)


# ------------------------------------------------------------------ endpoint

@pytest.fixture
def api(monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    server.set_settings_token("purge-test-token")
    try:
        yield httpd.server_port
    finally:
        server.set_settings_token(None)
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


@pytest.fixture
def services():
    started = []
    yield started
    for service in started:
        service.stop(timeout=1)


def request(port, method, path, body=None, *, headers=None, origin=True, raw=None):
    encoded = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request_headers = {"Host": f"127.0.0.1:{port}"}
    if origin:
        request_headers["Origin"] = f"http://127.0.0.1:{port}"
    if encoded is not None:
        request_headers.update({"Content-Type": "application/json", "Content-Length": str(len(encoded))})
    request_headers.update(headers or {})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    conn.request(method, path, body=encoded, headers=request_headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, (json.loads(data) if data else {})


def purge(port, body=None, **kwargs):
    return request(port, "POST", "/api/ledger/purge", body, **kwargs)


def test_purge_endpoint_refuses_every_guard_before_anything_runs(api, tmp_path, services, monkeypatch):
    fake = FakeLedger()
    services.append(started_service(tmp_path, fake))
    monkeypatch.setattr(ledger_service, "_CURRENT", services[0])
    ok = {"provider": "Codex"}

    status, denied = purge(api, ok, headers={"Host": "evil.invalid", **TOKEN}, origin=False)
    assert status == 403 and denied["error"] == "Open settings from the Branchglance widget to change this."
    assert purge(api, ok, origin=False, headers=TOKEN)[0] == 403  # no same-origin proof
    assert purge(api, ok, headers={**TOKEN, "Origin": "http://dashboard.invalid"})[0] == 403
    assert purge(api, ok)[0] == 403  # the dashboard origin without the settings token
    assert purge(api, ok, headers={"X-Branchglance-Settings-Token": "wrong"})[0] == 403
    assert purge(api, ok, headers={"X-Branchglance-Settings-Token": "wrong", "Content-Type": "text/plain"})[0] == 403
    assert purge(api, ok, headers={**TOKEN, "Content-Type": "text/plain"})[0] == 415
    assert purge(api, raw=b"{bad", headers=TOKEN)[0] == 400
    assert purge(api, {"provider": "Other"}, headers=TOKEN)[0] == 400
    assert purge(api, {"provider": "Claude", "extra": 1}, headers=TOKEN)[0] == 400
    assert purge(api, ["Codex"], headers=TOKEN)[0] == 400
    assert purge(api, {"provider": []}, headers=TOKEN) == (400, {"ok": False, "error": "Expected provider Claude or Codex."})
    assert purge(api, {"provider": {"name": "Codex"}}, headers=TOKEN)[0] == 400
    assert purge(api, {"provider": None}, headers=TOKEN)[0] == 400
    assert not [event for event in fake.events if isinstance(event, tuple)]  # no purge ran


def test_purge_endpoint_is_503_when_the_ledger_is_off(api, monkeypatch):
    monkeypatch.setattr(ledger_service, "_CURRENT", None)
    status, data = purge(api, {"provider": "Codex"}, headers=TOKEN)
    assert status == 503 and data == {"ok": False, "error": "Ledger is off."}


def test_purge_endpoint_is_409_while_collection_is_on_and_queues_nothing(api, tmp_path, services, monkeypatch):
    fake = FakeLedger()
    service = started_service(tmp_path, fake, claude_collect=True)
    services.append(service)
    monkeypatch.setattr(ledger_service, "_CURRENT", service)
    status, data = purge(api, {"provider": "Claude"}, headers=TOKEN)
    assert status == 409 and data == {"ok": False, "error": "Turn off collection for Claude first."}
    assert not [event for event in fake.events if isinstance(event, tuple)] and service.health()["purging"] == []


def test_purge_endpoint_success_is_200(api, tmp_path, services, monkeypatch):
    fake = FakeLedger()
    service = started_service(tmp_path, fake, claude_collect=True, codex_collect=False)
    services.append(service)
    monkeypatch.setattr(ledger_service, "_CURRENT", service)
    status, data = purge(api, {"provider": "Codex"}, headers=TOKEN)
    assert status == 200 and data["ok"] is True and len(data["ticket"]) == 32
    assert ("purge", "Codex") in fake.events


def test_purge_endpoint_timeout_is_202_pending_and_the_purge_still_runs(api, tmp_path, services, monkeypatch):
    fake = FakeLedger()
    fake.purge_gate = threading.Event()
    service = started_service(tmp_path, fake, claude_collect=True, codex_collect=False)
    services.append(service)
    monkeypatch.setattr(ledger_service, "_CURRENT", service)
    monkeypatch.setattr(ledger_service, "PURGE_WAIT_SECONDS", 0.2)
    try:
        status, data = purge(api, {"provider": "Codex"}, headers=TOKEN)
        assert status == 202 and data["ok"] is True and data["pending"] is True
        assert data["ticket"] == service._tickets["Codex"].id
        assert service.health()["purging"] == ["Codex"]
        status, health = request(api, "GET", "/api/ledger/status")
        assert status == 200 and health["purging"] == ["Codex"]
    finally:
        fake.purge_gate.set()
    wait_until(lambda: service.health()["purging"] == [])
    assert ("purge", "Codex") in fake.events


def test_purge_endpoint_failure_is_500_with_class_name_only(api, tmp_path, services, monkeypatch):
    fake = FakeLedger()
    fake.purge_error = RuntimeError("path C:/secret")
    service = started_service(tmp_path, fake, claude_collect=True, codex_collect=False)
    services.append(service)
    monkeypatch.setattr(ledger_service, "_CURRENT", service)
    status, data = purge(api, {"provider": "Codex"}, headers=TOKEN)
    assert status == 500 and data["ok"] is False and data["error"] == "RuntimeError" and len(data["ticket"]) == 32


def test_purge_endpoint_cancelled_is_503_not_deleted(api, tmp_path, services, monkeypatch):
    fake = FakeLedger()
    service = started_service(tmp_path, fake, claude_collect=True, codex_collect=False)
    services.append(service)
    monkeypatch.setattr(ledger_service, "_CURRENT", service)
    cancelled = PurgeTicket("Codex")
    cancelled.resolve("cancelled")
    monkeypatch.setattr(service, "request_purge", lambda provider: cancelled)
    status, data = purge(api, {"provider": "Codex"}, headers=TOKEN)
    assert status == 503 and data == {"ok": False, "error": "Not deleted; Branchglance is closing.", "ticket": cancelled.id}


def test_status_get_is_read_only_and_reports_the_writer_collect_flags(api, tmp_path, services, monkeypatch):
    status, data = request(api, "GET", "/api/ledger/status")
    assert status == 200 and data["state"] == "disabled" and data["purging"] == [] and data["collect"] == {}
    fake = FakeLedger()
    service = started_service(tmp_path, fake, claude_collect=True, codex_collect=False)
    services.append(service)
    monkeypatch.setattr(ledger_service, "_CURRENT", service)
    wait_until(lambda: service.health()["state"] in {"live", "backfilling"})
    status, data = request(api, "GET", "/api/ledger/status")
    assert status == 200 and data["collect"] == {"Claude": True, "Codex": False}
    assert data["lastPurge"] == {} and fake.events[:1] == ["ingest"]


def test_purge_results_are_kept_per_provider_and_ticket(tmp_path):
    # Sol's replay: Claude fails, then Codex succeeds. Codex's result must not overwrite Claude's.
    fake = FakeLedger()
    fake.purge_errors = {"Claude": RuntimeError("injected")}
    service = started_service(tmp_path, fake)
    try:
        claude = service.request_purge("Claude")
        assert claude.wait(5) and claude.status == "failed"
        codex = service.request_purge("Codex")
        assert codex.wait(5) and codex.status == "done"
        assert service.health()["lastPurge"] == {
            "Claude": {"ticket": claude.id, "result": "failed", "error": "RuntimeError"},
            "Codex": {"ticket": codex.id, "result": "done", "error": None},
        }
        assert claude.id != codex.id
    finally:
        service.stop()


def test_token_revoked_while_the_body_is_read_queues_nothing(tmp_path, services, monkeypatch):
    class RevokingHandler(server.Handler):
        """Revokes the settings token during the body read, after the header-time token check has passed."""

        def setup(self):
            super().setup()
            stream = self.rfile

            class RevokingReader:
                def read(self, size=-1):
                    data = stream.read(size)
                    server.set_settings_token("revoked-while-reading")
                    return data

                def __getattr__(self, name):
                    return getattr(stream, name)

            self.rfile = RevokingReader()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), RevokingHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    server.set_settings_token("purge-test-token")
    fake = FakeLedger()
    service = started_service(tmp_path, fake, claude_collect=True, codex_collect=False)
    services.append(service)
    monkeypatch.setattr(ledger_service, "_CURRENT", service)
    try:
        status, data = purge(httpd.server_port, {"provider": "Codex"}, headers=TOKEN)
        assert status == 403
        assert data == {"ok": False, "error": "Open settings from the Branchglance widget to change this."}
        assert service.health()["purging"] == []
        assert not [event for event in fake.events if isinstance(event, tuple)]
    finally:
        server.set_settings_token(None)
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
