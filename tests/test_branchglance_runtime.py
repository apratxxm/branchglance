"""Packaging-runtime behavior: product roots, policy file, single instance, control port, WebView2 storage."""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid

import pytest

from token_tracker import limit_reset, phone_server, product, provider_policy, widget
from token_tracker.provider_policy import (
    ProviderPolicyError,
    load_policies,
    load_policies_resilient,
    public_policies,
    read_policy_file,
)


@pytest.fixture(autouse=True)
def _no_real_snapshot_warmup(monkeypatch):
    monkeypatch.setattr(widget, "_warm_snapshot", lambda: None)


# --- product roots ---------------------------------------------------------------------------

def test_frozen_roots_are_constants_and_ignore_env_override(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path), product.STATE_ROOT_ENV: str(tmp_path / "evil")}
    assert product.state_root(frozen=True, environ=env) == tmp_path / "Branchglance" / "state"
    assert product.webview_storage_path(frozen=True, environ=env) == tmp_path / "Branchglance" / "webview"
    assert product.policy_file_path(frozen=True, environ=env) == tmp_path / "Branchglance" / "policy.json"
    assert product.phone_state_path(frozen=True, environ=env) == tmp_path / "Branchglance" / "state" / "phone-state.json"


def test_dev_roots_keep_legacy_dir_unless_overridden(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path)}
    assert product.state_root(frozen=False, environ=env) == tmp_path / "TokenTracker"
    assert product.policy_file_path(frozen=False, environ=env) is None
    env[product.STATE_ROOT_ENV] = str(tmp_path / "custom")
    assert product.state_root(frozen=False, environ=env) == tmp_path / "custom"


@pytest.mark.parametrize("frozen", [False, True])
def test_claude_hook_paths_are_stable_across_dev_and_frozen_modes(tmp_path, frozen):
    env = {"LOCALAPPDATA": str(tmp_path), product.STATE_ROOT_ENV: str(tmp_path / "override")}
    root = tmp_path / "Branchglance"
    assert product.hook_data_root(environ=env) == root
    assert product.claude_consent_marker_path(frozen=frozen, environ=env) == root / "claude-consent.json"
    assert product.claude_events_dir(environ=env) == root / "claude-events"


def test_frozen_mode_never_writes_under_legacy_tokentracker_dir(tmp_path, monkeypatch):
    monkeypatch.delenv(product.STATE_ROOT_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setattr(product.sys, "frozen", True, raising=False)
    monkeypatch.setattr(widget, "CONTROL_PORT", 0)
    root =limit_reset._runtime_dir()
    assert root == tmp_path / "local" / "Branchglance" / "state"
    service = limit_reset.LimitResetService(sources={}, senders={}, enabled=False)
    assert service.state_path.parent == root and service.cwd == root
    assert widget._window_state_path().parent == root
    assert phone_server._state_path().parent == root
    app = widget.WidgetApp()
    try:
        app.start_control()
        widget._save_window_bounds(widget._window_state_path(), {"x": 1, "y": 2, "width": 300, "height": 300})
    finally:
        app.control.close()
    assert not (tmp_path / "local" / "TokenTracker").exists()
    assert not (tmp_path / "roaming" / "TokenTracker").exists()


def test_identity_reports_product_build_pid_and_state_root():
    who = product.identity()
    assert who["product"] == "Branchglance" and isinstance(who["build"], str) and who["pid"] > 0
    assert who["stateRoot"] == str(product.state_root())
    assert product.BUILD and product.STATE_ROOT


def test_mutex_names_are_global_and_scoped_to_the_user_sid():
    sid = "S-1-5-21-1-2-3-1000"
    assert product.mutex_name("Install", sid) == rf"Global\Branchglance-{sid}-Install"
    assert product.mutex_name("Instance", sid) == rf"Global\Branchglance-{sid}-Instance"


def test_runtime_mutex_creation_uses_user_and_system_only_dacl(monkeypatch):
    import ctypes
    sid = "S-1-5-21-7-8-9-1001"
    calls = {}
    class Fn:
        def __init__(self, callback): self.callback = callback
        def __call__(self, *args): return self.callback(*args)
    class Kernel:
        def __init__(self):
            self.CreateMutexW = Fn(self.create)
            self.WaitForSingleObject = Fn(lambda *_: 0)
            self.CloseHandle = Fn(lambda *_: 1)
            self.LocalFree = Fn(lambda *_: 0)
        def create(self, attributes, _initial, name):
            calls["name"] = name
            calls["descriptor"] = ctypes.c_void_p.from_address(ctypes.cast(attributes, ctypes.c_void_p).value + 8).value
            return 1234
    class Advapi:
        def __init__(self): self.ConvertStringSecurityDescriptorToSecurityDescriptorW = Fn(self.convert)
        def convert(self, sddl, _revision, out, _size):
            calls["sddl"] = sddl
            ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p)).contents.value = 4321
            return 1
    kernel, advapi = Kernel(), Advapi()
    monkeypatch.setattr(product, "current_user_sid", lambda: sid)
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **_kwargs: kernel if name == "kernel32" else advapi)
    name = product.mutex_name("Instance", sid)
    handle, state = widget.claim_instance_mutex(name)
    assert handle == 1234 and state == "acquired" and calls["name"] == name
    assert calls["descriptor"] == 4321
    assert calls["sddl"] == f"D:P(A;;GA;;;{sid})(A;;GA;;;SY)"


def test_claude_hook_launcher_capability_is_exposed():
    assert product.CLAUDE_HOOK_LAUNCHER is True


# --- policy file -----------------------------------------------------------------------------

def _write_policy(path, providers, **extra):
    path.write_text(json.dumps({"schemaVersion": 1, "providers": providers, **extra}), encoding="utf-8")
    return path


def test_policy_file_codex_send_maps_to_legacy_greeting_only(tmp_path):
    path = _write_policy(tmp_path / "policy.json", {"Codex": {"collect": True, "show": True, "send": True}},
                         origin="user_settings")
    policies = load_policies_resilient({}, policy_path=path)
    codex = policies["Codex"]
    assert codex.allows("greet") and not codex.allows("continue")
    assert codex.send_origin == "legacy_global"
    public = public_policies(policies)
    assert public["origin"] == "policy_file" and public["policyFile"] == "ok" and public["warnings"] == []


@pytest.mark.parametrize("content", [
    "not json", "[]", '{"schemaVersion": 2, "providers": {}}',
    '{"schemaVersion": 1, "providers": {"Other": {}}}',
    '{"schemaVersion": 1, "providers": {"Codex": {"send": "yes"}}}',
    '{"schemaVersion": 1, "providers": {"Codex": {"send": true, "collect": false}}}',
    '{"schemaVersion": 1, "providers": {"Codex": {"send": true}}}',
    '{"schemaVersion": 1, "providers": {}, "extra": 1}',
    '{"schemaVersion": true, "providers": {}}',
])
def test_malformed_policy_file_falls_back_to_defaults_with_visible_warning(tmp_path, content):
    path = tmp_path / "policy.json"
    path.write_text(content, encoding="utf-8")
    policies = load_policies_resilient({}, policy_path=path)
    assert not policies["Codex"].send and policies["Codex"].collect and not policies["Claude"].collect
    public = public_policies(policies)
    assert public["policyFile"] == "malformed" and public["warnings"]


def test_policy_file_oversized_or_missing(tmp_path):
    big = tmp_path / "policy.json"
    big.write_text(" " * 70_000, encoding="utf-8")
    assert read_policy_file(big).status == "malformed"
    assert read_policy_file(tmp_path / "absent.json").status == "absent"
    assert read_policy_file(None).status == "absent"
    assert read_policy_file(tmp_path).status == "malformed"  # a directory is not a policy file


def test_env_flags_override_file_and_explicit_zero_vetoes(tmp_path):
    path = _write_policy(tmp_path / "policy.json", {"Codex": {"collect": True, "show": True, "send": True}})
    vetoed = load_policies_resilient({"TOKEN_TRACKER_CODEX_SEND": "0"}, policy_path=path)
    assert not vetoed["Codex"].allows("greet") and vetoed["Codex"].send_origin == "provider_denied"
    assert public_policies(vetoed)["origin"] == "policy_file+environment"
    legacy_veto = load_policies_resilient({"TOKEN_TRACKER_LIMIT_RESET": "0"}, policy_path=path)
    assert not legacy_veto["Codex"].allows("greet")
    collect_veto = load_policies_resilient({"TOKEN_TRACKER_CODEX_COLLECT": "0"}, policy_path=path)
    assert not collect_veto["Codex"].collect and not collect_veto["Codex"].send
    assert not collect_veto["Codex"].allows("show") and not collect_veto["Codex"].allows("greet")


def test_conflicting_env_flags_do_not_crash_and_fail_closed_visibly():
    env = {"TOKEN_TRACKER_LIMIT_RESET": "false", "TOKEN_TRACKER_CODEX_SEND": "true"}
    with pytest.raises(ProviderPolicyError):
        load_policies(env)
    policies = load_policies_resilient(env)
    assert not any(policies[name].collect or policies[name].send for name in ("Codex", "Claude"))
    public = public_policies(policies)
    assert public["origin"] == "environment_error" and "rejected" in public["warnings"][-1]


def test_claude_collection_needs_both_file_and_consent_marker(tmp_path):
    path = _write_policy(tmp_path / "policy.json", {"Claude": {"collect": True, "show": True}})
    without = load_policies_resilient({}, policy_path=path, claude_consent=False)
    assert not without["Claude"].collect and public_policies(without)["warnings"]
    granted = load_policies_resilient({}, policy_path=path, claude_consent=True)
    assert granted["Claude"].allows("collect") and granted["Claude"].allows("show")
    assert not granted["Claude"].allows("greet")
    no_file_entry = _write_policy(tmp_path / "other.json", {"Codex": {"collect": True}})
    assert not load_policies_resilient({}, policy_path=no_file_entry, claude_consent=True)["Claude"].collect


def test_claude_send_in_policy_file_is_ignored(tmp_path):
    path = _write_policy(tmp_path / "policy.json", {"Claude": {"collect": True, "show": True, "send": True}})
    policies = load_policies_resilient({}, policy_path=path, claude_consent=True)
    assert not policies["Claude"].send and not policies["Claude"].allows("greet")


def test_dev_checkout_ignores_installed_policy_file(monkeypatch):
    assert product.policy_file_path() is None  # conftest clears BRANCHGLANCE_POLICY_FILE


# --- single instance / control port ---------------------------------------------------------

def _close(handle):
    if handle:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


def _in_thread(function):
    """Run ``function`` on another OS thread (mutex ownership is per thread) and return its result."""
    box = []
    worker = threading.Thread(target=lambda: box.append(function()))
    worker.start(); worker.join(5)
    return box[0]


def _test_mutex_name():
    return f"Global\\Branchglance-{product.current_user_sid()}-Instance.Test.{uuid.uuid4().hex}"


def _bystander_handle(name):
    """Open (never own) the named mutex so the kernel object outlives its owners."""
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    return kernel32.CreateMutexW(None, False, name)


def test_instance_mutex_second_acquirer_is_refused():
    name = _test_mutex_name()
    handle, owned = widget.acquire_instance_mutex(name)
    assert owned
    try:
        assert _in_thread(lambda: widget.claim_instance_mutex(name)) == (None, "busy")
        assert _in_thread(lambda: widget.acquire_instance_mutex(name)) == (None, False)
    finally:
        _close(handle)
    again, owned_again = _in_thread(lambda: widget.acquire_instance_mutex(name))
    assert owned_again
    _close(again)


def test_released_mutex_kept_alive_by_another_handle_is_acquired_not_refused():
    # F3: the object still exists (a plain handle is open elsewhere) but nobody owns it.
    name = _test_mutex_name()
    bystander = _bystander_handle(name)
    try:
        handle, status = _in_thread(lambda: widget.claim_instance_mutex(name))
        assert status == "acquired" and handle
        _close(handle)
    finally:
        _close(bystander)


def test_abandoned_mutex_with_retained_handle_is_owned_and_reported_abandoned():
    name = _test_mutex_name()
    bystander = _bystander_handle(name)
    try:
        # The previous owner's thread ends while owning the mutex (a crash, in effect).
        died_owning, status = _in_thread(lambda: widget.claim_instance_mutex(name))
        assert status == "acquired"
        handle, status = _in_thread(lambda: widget.claim_instance_mutex(name))
        assert status == "abandoned" and handle
        _close(handle)
        _close(died_owning)
    finally:
        _close(bystander)


def test_main_recovers_after_abandoned_mutex_then_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(widget, "claim_instance_mutex", lambda name=None: (None, "abandoned"))
    monkeypatch.setattr(widget, "recover_abandoned_instance", lambda: calls.append("recover"))
    monkeypatch.setattr(widget.product, "set_app_user_model_id", lambda *a: True)
    monkeypatch.setattr(widget, "WidgetApp", type("X", (), {
        "__init__": lambda self, **k: None, "run": lambda self: calls.append("run")}))
    widget.main([])
    assert calls == ["recover", "run"]


def test_main_refuses_visibly_when_mutex_state_cannot_be_read(monkeypatch):
    messages, launches = [], []
    monkeypatch.setattr(widget, "claim_instance_mutex", lambda name=None: (None, "failed"))
    monkeypatch.setattr(widget, "show_message", lambda *a: messages.append(a))
    monkeypatch.setattr(widget, "WidgetApp", type("X", (), {
        "notify_existing": staticmethod(lambda: launches.append("notify")),
        "__init__": lambda self, **k: launches.append(k)}))
    widget.main([])
    assert launches == [] and len(messages) == 1


def test_recover_abandoned_instance_removes_only_the_stale_nonce():
    path = product.control_nonce_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stale")
    (path.parent / "limit-reset-state.json").write_text("{}")
    widget.recover_abandoned_instance()
    assert not path.exists() and (path.parent / "limit-reset-state.json").exists()


def test_main_when_mutex_held_shows_message_and_does_not_launch(monkeypatch):
    messages, launches = [], []
    monkeypatch.setattr(widget, "claim_instance_mutex", lambda name=None: (None, "busy"))
    monkeypatch.setattr(widget.WidgetApp, "notify_existing", staticmethod(lambda: False))
    monkeypatch.setattr(widget, "show_message", lambda *a: messages.append(a))
    monkeypatch.setattr(widget, "WidgetApp", type("X", (), {
        "notify_existing": staticmethod(lambda: False), "__init__": lambda self, **k: launches.append(k)}))
    widget.main([])
    assert launches == [] and len(messages) == 1 and "already running" in messages[0][1]


def test_main_when_mutex_held_by_running_instance_focuses_it_silently(monkeypatch):
    messages = []
    monkeypatch.setattr(widget, "claim_instance_mutex", lambda name=None: (None, "busy"))
    monkeypatch.setattr(widget, "show_message", lambda *a: messages.append(a))
    monkeypatch.setattr(widget, "WidgetApp", type("X", (), {"notify_existing": staticmethod(lambda: True)}))
    widget.main([])
    assert messages == []


@pytest.fixture
def live_control(monkeypatch):
    monkeypatch.setattr(widget, "CONTROL_PORT", 0)
    app = widget.WidgetApp()
    shown = threading.Event()
    app.show = lambda *_: shown.set()
    app.start_control()
    monkeypatch.setattr(widget, "CONTROL_PORT", app.control.getsockname()[1])
    yield app, shown
    app.quitting = True
    app.control.close()


def test_control_identity_is_open_but_commands_need_the_challenge_response(live_control):
    app, shown = live_control
    who = json.loads(widget._control_request("identity"))
    assert who["product"] == "Branchglance" and who["pid"] > 0 and who["stateRoot"] == str(product.state_root())
    assert "proof" not in who and "challenge" not in who
    nonce = widget.read_control_nonce()
    # The old raw-nonce form is no longer a command, even with the right nonce.
    for line in ("show", "show wrong-nonce", f"show {nonce}", f"maintenance-exit {nonce}"):
        assert widget._control_request(line) == "error"
    assert widget.control_command("show", nonce="f0" * 32) is None  # server unverifiable: nothing sent
    assert widget.control_command("unknown-command") is None
    assert not shown.is_set() and not app.maintenance_exit_requested
    assert widget.control_command("show") == "ok" and shown.wait(2)


def test_authenticated_control_health_binds_pid_build_and_state_root(live_control):
    app, _shown = live_control
    nonce = widget.read_control_nonce()
    who = product.control_health(nonce, port=app.control.getsockname()[1], timeout=1)
    assert who["product"] == product.PRODUCT
    assert who["pid"] == __import__("os").getpid()
    assert who["build"] == product.BUILD and who["stateRoot"] == str(product.state_root())


def test_maintenance_ack_is_sent_before_shutdown_is_started():
    import json as json_module
    app = widget.WidgetApp()
    nonce = "ab" * 32
    cc = "cd" * 32
    app._nonce = nonce
    class Connection:
        def __init__(self): self.incoming = bytearray(f"hello {cc}\n".encode()); self.sent = []; self.ack_sent = False
        def recv(self, count):
            data = bytes(self.incoming[:count]); del self.incoming[:count]; return data
        def sendall(self, data):
            self.sent.append(data)
            if data.startswith(b"{"):
                hello = json_module.loads(data)
                mac = product.control_mac(nonce, "client", cc, hello["challenge"], "maintenance-exit")
                self.incoming.extend(f"maintenance-exit {mac}\n".encode())
            elif data == b"ok\n":
                self.ack_sent = True
    connection = Connection()
    called = []
    def begin_shutdown():
        assert connection.ack_sent
        called.append(True)
    app._request_maintenance_exit = begin_shutdown
    app.serve_control_connection(connection)
    assert called == [True] and connection.sent[-1] == b"ok\n"


def _hello(port, client_challenge):
    connection = socket.create_connection((widget.HTTP_HOST, port), timeout=2)
    connection.sendall(f"hello {client_challenge}\n".encode())
    reader = widget._LineReader(connection, widget.CONTROL_REPLY_LIMIT)
    return connection, reader, json.loads(reader.line())


def test_control_server_proves_nonce_and_binds_command_to_one_exchange(live_control):
    app, shown = live_control
    nonce = widget.read_control_nonce()
    cc = "ab" * 32
    first, reader, hello = _hello(widget.CONTROL_PORT, cc)
    with first:
        assert hello["proof"] == product.control_mac(nonce, "server", cc, hello["challenge"])
        assert nonce not in json.dumps(hello)
        line = f"show {product.control_mac(nonce, 'client', cc, hello['challenge'], 'show')}"
        first.sendall(line.encode() + b"\n")
        assert reader.line() == "ok" and shown.wait(2)
    shown.clear()
    # Replaying the recorded command on a new connection fails: the server challenge differs.
    second, reader, hello2 = _hello(widget.CONTROL_PORT, cc)
    with second:
        assert hello2["challenge"] != hello["challenge"]
        second.sendall(line.encode() + b"\n")
        assert reader.line() == "denied"
    # A MAC for one command cannot authorize another.
    third, reader, hello3 = _hello(widget.CONTROL_PORT, cc)
    with third:
        show_mac = product.control_mac(nonce, "client", cc, hello3["challenge"], "show")
        third.sendall(f"maintenance-exit {show_mac}\n".encode())
        assert reader.line() == "denied"
    # Malformed client challenges are refused before any proof is produced.
    for bad in ("short", "ZZ" * 32, "ab" * 70):
        with socket.create_connection((widget.HTTP_HOST, widget.CONTROL_PORT), timeout=2) as raw:
            raw.sendall(f"hello {bad}\n".encode())
            assert widget._LineReader(raw, 4096).line() in ("denied", "error", None)
    assert not shown.is_set() and not app.maintenance_exit_requested


def test_nonce_file_is_fresh_per_start_and_removed_on_quit(monkeypatch):
    monkeypatch.setattr(widget, "CONTROL_PORT", 0)
    first = widget.WidgetApp(); first.start_control()
    nonce_one = first.nonce_path.read_text()
    first.quit(); 
    assert not first.nonce_path.exists()
    monkeypatch.setattr(widget, "CONTROL_PORT", 0)
    second = widget.WidgetApp(); second.start_control()
    try:
        assert second.nonce_path.read_text() != nonce_one
    finally:
        second.quit()


def test_maintenance_exit_with_nonce_quits_the_app(live_control):
    app, _shown = live_control
    quit_called = threading.Event()
    app.quit = lambda *_: quit_called.set()
    assert widget.control_command("maintenance-exit") == "ok"
    assert quit_called.wait(2)
    deadline = time.monotonic() + 2
    while not app.maintenance_exit_requested and time.monotonic() < deadline:
        time.sleep(0.001)
    assert app.maintenance_exit_requested


def test_silent_client_does_not_block_other_clients(live_control):
    app, _ = live_control
    silent = socket.create_connection((widget.HTTP_HOST, widget.CONTROL_PORT))
    try:
        assert json.loads(widget._control_request("identity", timeout=0.5))["product"] == "Branchglance"
    finally:
        silent.close()


def test_http_bind_failure_is_fatal_visible_and_releases_control(monkeypatch):
    blocker = widget._ExclusiveHTTPServer((widget.HTTP_HOST, 0), widget.Handler)
    messages = []
    try:
        monkeypatch.setattr(widget, "HTTP_PORT", blocker.server_address[1])
        monkeypatch.setattr(widget, "CONTROL_PORT", 0)
        monkeypatch.setattr(widget, "BIND_RETRY_ATTEMPTS", 2)
        monkeypatch.setattr(widget, "BIND_RETRY_DELAY", 0)
        monkeypatch.setattr(widget, "show_message", lambda *a: messages.append(a))
        monkeypatch.setattr(widget.webview, "create_window", lambda *a, **k: pytest.fail("window created"))
        app = widget.WidgetApp()
        app.run()
        assert app.server is None and len(messages) == 1 and str(blocker.server_address[1]) in messages[0][1]
        assert app.control.fileno() == -1 and not app.nonce_path.exists()
    finally:
        blocker.server_close()


def test_control_bind_retries_a_bounded_number_of_times(monkeypatch):
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    widget._claim_address(holder)
    holder.bind((widget.HTTP_HOST, 0)); holder.listen(1)
    sleeps = []
    monkeypatch.setattr(widget, "CONTROL_PORT", holder.getsockname()[1])
    monkeypatch.setattr(widget, "BIND_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(widget, "BIND_RETRY_DELAY", 0.01)
    monkeypatch.setattr(widget.time, "sleep", lambda s: sleeps.append(s))
    app = widget.WidgetApp()
    try:
        with pytest.raises(OSError):
            app.start_control()
        assert len(sleeps) == 2
    finally:
        app.control.close(); holder.close()


# --- WebView2 storage ------------------------------------------------------------------------

def test_webview_storage_seeded_once_and_legacy_left_intact(tmp_path):
    legacy = tmp_path / "pywebview"
    (legacy / "EBWebView" / "Default").mkdir(parents=True)
    (legacy / "EBWebView" / "Default" / "Local Storage.txt").write_text("keep")
    destination = tmp_path / "Branchglance" / "webview"
    assert product.prepare_webview_storage(destination, legacy) == destination
    assert (destination / "EBWebView" / "Default" / "Local Storage.txt").read_text() == "keep"
    assert (legacy / "EBWebView" / "Default" / "Local Storage.txt").exists()
    (destination / "EBWebView" / "Default" / "Local Storage.txt").write_text("changed")
    product.prepare_webview_storage(destination, legacy)  # existing destination is never re-seeded
    assert (destination / "EBWebView" / "Default" / "Local Storage.txt").read_text() == "changed"


def test_webview_storage_without_legacy_creates_nothing(tmp_path):
    destination = tmp_path / "webview"
    product.prepare_webview_storage(destination, tmp_path / "nothing")
    assert not destination.exists()


def test_webview_copy_failure_leaves_no_partial_destination(tmp_path, monkeypatch):
    legacy = tmp_path / "pywebview"
    legacy.mkdir()
    (legacy / "file").write_text("x")
    monkeypatch.setattr(product, "_copy_plain_tree", lambda s, t, **k: (_ for _ in ()).throw(PermissionError("locked")))
    destination = tmp_path / "out" / "webview"
    product.prepare_webview_storage(destination, legacy)
    assert not destination.exists()
    assert not list((tmp_path / "out").glob("webview.copy-*"))
    assert (legacy / "file").exists()


def test_webview_copy_skips_junctions(tmp_path):
    import subprocess
    legacy = tmp_path / "pywebview"
    outside = tmp_path / "outside"
    outside.mkdir(); (outside / "secret.txt").write_text("s")
    legacy.mkdir(); (legacy / "ok.txt").write_text("ok")
    link = legacy / "junction"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")
    destination = tmp_path / "dest"
    product.prepare_webview_storage(destination, legacy)
    assert (destination / "ok.txt").exists()
    assert not (destination / "junction").exists()
    assert (outside / "secret.txt").exists()


def test_run_passes_explicit_storage_path_to_webview(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(widget, "CONTROL_PORT", 0)
    monkeypatch.setattr(widget, "HTTP_PORT", 0)
    monkeypatch.setattr(widget.webview, "start", lambda **k: started.append(k))
    monkeypatch.setattr(widget.webview, "create_window", lambda *a, **k: type("W", (), {
        "events": type("E", (), {"__getattr__": lambda s, n: type("Ev", (list,), {"__iadd__": lambda self, h: self})()})()})())
    monkeypatch.setattr(widget.pystray, "Icon", lambda *a, **k: type("T", (), {"run": lambda s, **kw: None})())
    app = widget.WidgetApp()
    app.limit_reset = type("Reset", (), {"start": lambda self: None, "stop": lambda self: None})()
    try:
        app.run()
    finally:
        if app.server:
            app.server.shutdown(); app.server.server_close()
        app.control.close()
    assert started and started[0]["storage_path"] == str(product.webview_storage_path())


def test_app_user_model_id_is_explicit():
    assert product.APP_USER_MODEL_ID == "Branchglance.App"


# --- Phase A review fixes: F2 (redirected storage), F4 (maintenance during startup), F5, F6, F7 ----

import subprocess  # noqa: E402


def _junction(link, target):
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")


def _tree_snapshot(root):
    return sorted((str(path.relative_to(root)), path.read_bytes() if path.is_file() else None)
                  for path in root.rglob("*"))


@pytest.mark.parametrize("redirect", ["data-root", "state", "webview", "nested-profile"])
def test_frozen_redirected_storage_is_refused_visibly_and_target_untouched(tmp_path, monkeypatch, redirect):
    monkeypatch.delenv(product.STATE_ROOT_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setattr(product.sys, "frozen", True, raising=False)
    monkeypatch.setattr(widget, "CONTROL_PORT", 0)
    monkeypatch.setattr(widget, "HTTP_PORT", 0)
    legacy_state = tmp_path / "local" / "TokenTracker"
    legacy_state.mkdir(parents=True)
    (legacy_state / "limit-reset-state.json").write_text("legacy-state")
    legacy_profile = tmp_path / "roaming" / "pywebview"
    (legacy_profile / "EBWebView" / "Default").mkdir(parents=True)
    (legacy_profile / "EBWebView" / "Default" / "Cookies").write_text("legacy-cookies")
    root = tmp_path / "local" / "Branchglance"
    if redirect == "data-root":
        _junction(root, legacy_state)
    elif redirect == "state":
        root.mkdir()
        _junction(root / "state", legacy_state)
    elif redirect == "webview":
        root.mkdir()
        _junction(root / "webview", legacy_profile)
    else:
        (root / "webview" / "EBWebView").mkdir(parents=True)
        _junction(root / "webview" / "EBWebView" / "Default", legacy_profile / "EBWebView" / "Default")
    before = (_tree_snapshot(legacy_state), _tree_snapshot(legacy_profile))
    assert product.storage_problem() is not None
    messages = []
    monkeypatch.setattr(widget, "show_message", lambda *a: messages.append(a))
    monkeypatch.setattr(widget.webview, "create_window", lambda *a, **k: pytest.fail("window created"))
    monkeypatch.setattr(widget.webview, "start", lambda **k: pytest.fail("webview started"))
    app = widget.WidgetApp()
    try:
        app.run()
    finally:
        app.control.close()
    assert len(messages) == 1 and "redirected" in messages[0][1]
    assert app.server is None and app._nonce is None  # neither port was opened
    assert (_tree_snapshot(legacy_state), _tree_snapshot(legacy_profile)) == before


def test_frozen_plain_storage_passes_validation(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path)}
    assert product.storage_problem(frozen=True, environ=env) is None  # nothing exists yet
    (tmp_path / "Branchglance" / "state").mkdir(parents=True)
    (tmp_path / "Branchglance" / "webview" / "EBWebView" / "Default").mkdir(parents=True)
    (tmp_path / "Branchglance" / "webview" / "EBWebView" / "Default" / "Cookies").write_text("x")
    assert product.storage_problem(frozen=True, environ=env) is None
    (tmp_path / "Branchglance" / "state").rmdir()
    (tmp_path / "Branchglance" / "state").write_text("not a directory")
    assert "not a directory" in product.storage_problem(frozen=True, environ=env)


def test_dev_storage_is_not_validated(tmp_path):
    assert product.storage_problem(frozen=False, environ={"LOCALAPPDATA": str(tmp_path)}) is None


def test_existing_redirected_webview_destination_is_refused_unmodified(tmp_path):
    target = tmp_path / "legacy-profile"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    destination = tmp_path / "Branchglance" / "webview"
    destination.parent.mkdir()
    _junction(destination, target)
    with pytest.raises(product.StorageRedirectedError):
        product.prepare_webview_storage(destination, tmp_path / "nothing")
    assert (target / "keep.txt").read_text() == "keep" and sorted(p.name for p in target.iterdir()) == ["keep.txt"]


def test_webview_staging_collision_is_refused_not_deleted(tmp_path, monkeypatch):
    legacy = tmp_path / "pywebview"
    legacy.mkdir()
    (legacy / "file").write_text("x")
    destination = tmp_path / "out" / "webview"
    fixed = uuid.UUID(int=7)
    monkeypatch.setattr(product.uuid, "uuid4", lambda: fixed)
    occupied = destination.with_name(f"webview.copy-{fixed.hex}")
    occupied.mkdir(parents=True)
    (occupied / "unrelated.txt").write_text("retained")
    product.prepare_webview_storage(destination, legacy)
    assert not destination.exists()
    assert (occupied / "unrelated.txt").read_text() == "retained"


def test_webview_seed_leaves_unrelated_staging_dirs_alone(tmp_path):
    legacy = tmp_path / "pywebview"
    legacy.mkdir()
    (legacy / "file").write_text("x")
    destination = tmp_path / "out" / "webview"
    stale = destination.with_name(f"webview.copy-{12345}")  # a predictable old-style name (PID reuse)
    stale.mkdir(parents=True)
    (stale / "data").write_text("retained")
    product.prepare_webview_storage(destination, legacy)
    assert (destination / "file").read_text() == "x"
    assert (stale / "data").read_text() == "retained"
    assert [p.name for p in destination.parent.iterdir() if p.name.startswith("webview.copy-")] == [stale.name]


class _Event(list):
    def __iadd__(self, handler):
        self.append(handler)
        return self


class _Events:
    def __getattr__(self, name):
        event = _Event()
        setattr(self, name, event)
        return event


class _FakeWindow:
    def __init__(self):
        self.events = _Events()
        self.destroyed = 0

    def destroy(self):
        self.destroyed += 1


@pytest.mark.parametrize("during", ["http", "limit_reset", "window"])
def test_maintenance_exit_during_startup_cancels_remaining_init_and_releases_resources(monkeypatch, during):
    monkeypatch.setattr(widget, "CONTROL_PORT", 0)
    monkeypatch.setattr(widget, "HTTP_PORT", 0)
    started, trays, windows = [], [], []
    app = widget.WidgetApp()

    def maintenance():
        monkeypatch.setattr(widget, "CONTROL_PORT", app.control.getsockname()[1])
        assert widget.control_command("maintenance-exit") == "ok"
        deadline = time.monotonic() + 2
        while not app.maintenance_exit_requested and time.monotonic() < deadline:
            time.sleep(0.001)
        assert app.maintenance_exit_requested

    class Reset:
        started = stopped = False

        def start(self):
            self.started = True
            if during == "limit_reset":
                maintenance()

        def stop(self):
            self.stopped = True

    def create_window(*_a, **_k):
        window = _FakeWindow()
        windows.append(window)
        if during == "window":
            maintenance()
        return window

    monkeypatch.setattr(widget.webview, "start", lambda **k: started.append(k))
    monkeypatch.setattr(widget.webview, "create_window", create_window)
    monkeypatch.setattr(widget.pystray, "Icon", lambda *a, **k: trays.append(a) or pytest.fail("tray created"))
    reset = app.limit_reset = Reset()
    if during == "http":
        original = app.start_http
        app.start_http = lambda: (original(), maintenance())[0]
    try:
        app.run()
    finally:
        app.control.close()
    assert app.maintenance_exit_requested and app.quitting
    assert started == [] and trays == []
    assert reset.stopped and reset.started == (during != "http")
    if during == "window":
        assert len(windows) == 1 and windows[0].destroyed == 1
    else:
        assert windows == []
    assert app.server is not None and app.server.socket.fileno() == -1
    assert app.control.fileno() == -1 and not app.nonce_path.exists()


def test_deeply_nested_policy_file_falls_back_with_warning(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text("[" * 30000 + "]" * 30000, encoding="utf-8")  # < 64 KiB
    assert path.stat().st_size < 64 * 1024
    policies = load_policies_resilient({}, policy_path=path)
    public = public_policies(policies)
    assert public["policyFile"] == "malformed" and public["warnings"]
    assert not policies["Codex"].send and policies["Codex"].collect


def test_startup_policy_warnings_reach_identity_and_one_tray_notice(tmp_path, monkeypatch):
    path = tmp_path / "policy.json"
    path.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(provider_policy, "POLICIES", load_policies_resilient({}, policy_path=path))
    who = product.identity()
    assert who["policyWarnings"] and "policy file ignored" in who["policyWarnings"][0]
    notices = []
    icon = type("Icon", (), {"visible": False, "notify": lambda self, text, title: notices.append((title, text))})()
    app = widget.WidgetApp()
    try:
        app._tray_ready(icon)
        app._tray_ready(icon)
    finally:
        app.control.close()
    assert icon.visible and len(notices) == 1
    assert notices[0][0] == "Branchglance" and "policy file ignored" in notices[0][1]


def test_no_tray_notice_without_policy_warnings(monkeypatch):
    monkeypatch.setattr(provider_policy, "POLICIES", load_policies_resilient({}, policy_path=None))
    assert product.identity()["policyWarnings"] == []
    notices = []
    icon = type("Icon", (), {"visible": False, "notify": lambda self, *a: notices.append(a)})()
    app = widget.WidgetApp()
    try:
        app._tray_ready(icon)
    finally:
        app.control.close()
    assert icon.visible and notices == []
