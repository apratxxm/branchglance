from __future__ import annotations

import sys
from pathlib import Path

import pytest

from token_tracker import server
from token_tracker import provider_policy
from token_tracker.provider_policy import ProviderPolicyError


def _mock_raw_server_init(monkeypatch):
    calls = []

    def fake_init(self, server_address, handler_class, bind_and_activate=True):
        calls.append((server_address, handler_class, bind_and_activate))
        self.server_address = server_address
        self.RequestHandlerClass = handler_class

    monkeypatch.setattr(server._BaseThreadingHTTPServer, "__init__", fake_init)
    return calls


def test_binding_tests_import_this_checkout():
    expected = Path(__file__).resolve().parents[1] / "token_tracker" / "server.py"
    assert Path(server.__file__).resolve() == expected


@pytest.mark.parametrize("host", ["127.0.0.1"])
def test_application_server_accepts_only_loopback_ipv4_without_binding(host, monkeypatch):
    calls = _mock_raw_server_init(monkeypatch)

    instance = server.ThreadingHTTPServer((host, 0), server.Handler, bind_and_activate=False)

    assert instance.server_address == (host, 0)
    assert calls == [((host, 0), server.Handler, False)]
    assert issubclass(server.ThreadingHTTPServer, server._BaseThreadingHTTPServer)


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "LoCaLhOsT"])
def test_localhost_is_canonicalized_to_ipv4_loopback(host, monkeypatch):
    calls = _mock_raw_server_init(monkeypatch)

    instance = server.ThreadingHTTPServer((host, 0), server.Handler, bind_and_activate=False)

    assert instance.server_address == ("127.0.0.1", 0)
    assert calls == [(("127.0.0.1", 0), server.Handler, False)]


@pytest.mark.parametrize(
    "address",
    [
        ("0.0.0.0", 8765),
        ("::", 8765),
        ("::1", 8765),
        ("127.2.3.4", 8765),
        ("127.255.255.255", 8765),
        ("192.168.1.20", 8765),
        ("8.8.8.8", 8765),
        ("example.com", 8765),
        ("999.1.1.1", 8765),
        ("127.0.0.1:8765", 8765),
        (None, 8765),
        (123, 8765),
        ("127.0.0.1", True),
        ("127.0.0.1", "8765"),
        ("127.0.0.1", 65536),
        ["127.0.0.1", 8765],
        ("127.0.0.1",),
    ],
)
def test_invalid_bindings_fail_before_raw_server_init_even_when_deferred(address, monkeypatch):
    calls = _mock_raw_server_init(monkeypatch)

    with pytest.raises(ValueError, match="Token Tracker|IPv6"):
        server.ThreadingHTTPServer(address, server.Handler, bind_and_activate=False)

    assert calls == []


def test_cli_main_constructs_guarded_export(monkeypatch):
    calls = _mock_raw_server_init(monkeypatch)
    monkeypatch.setattr(server.ThreadingHTTPServer, "serve_forever", lambda self: None)
    monkeypatch.setattr(server.ThreadingHTTPServer, "server_close", lambda self: None)
    monkeypatch.setattr(sys, "argv", ["token-tracker", "--host", "LOCALHOST", "--no-open"])

    server.main()

    assert calls == [(("127.0.0.1", 8765), server.Handler, True)]


def test_widget_exclusive_server_uses_guarded_application_export(monkeypatch):
    from token_tracker import widget

    calls = _mock_raw_server_init(monkeypatch)

    instance = widget._ExclusiveHTTPServer(("localhost", 0), server.Handler, bind_and_activate=False)

    assert isinstance(instance, server.ThreadingHTTPServer)
    assert calls == [(("127.0.0.1", 0), server.Handler, False)]


def test_queue_cli_boundary_requires_explicit_codex_send(monkeypatch):
    policies = dict(provider_policy.POLICIES)
    policy = policies["Codex"]
    policies["Codex"] = type(policy)(policy.name, policy.collect, policy.show, False,
                                    "default", policy.capabilities)
    monkeypatch.setattr(provider_policy, "POLICIES", policies)
    monkeypatch.setattr(server.shutil, "which", lambda _name: pytest.fail("denied queue must not inspect CLI"))

    with pytest.raises(ProviderPolicyError, match="explicit Codex SEND consent"):
        server.queue_codex_message("01a0cad8-0000-7000-8000-000000000001", "hello")
