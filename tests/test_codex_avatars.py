from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from token_tracker import codex_avatars as avatars


@pytest.fixture(autouse=True)
def clear_cache(monkeypatch):
    monkeypatch.setattr(avatars, "_DISCOVERY", None)
    monkeypatch.setattr(avatars, "_MEMO", {})


def test_discovery_single_flight_and_cache(tmp_path, monkeypatch):
    calls = []
    entered, release = threading.Event(), threading.Event()
    asar = tmp_path / "version" / "app" / "resources" / "app.asar"
    def discover():
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return asar
    monkeypatch.setattr(avatars, "_codex_asar", discover)
    monkeypatch.setattr(avatars, "_extract", lambda path: ["avatar"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(avatars.codex_avatars, tmp_path) for _ in range(8)]
        assert entered.wait(5)
        release.set()
        assert all(f.result() == ["avatar"] for f in futures)
    assert avatars.codex_avatars(tmp_path) == ["avatar"] and len(calls) == 1


def test_unavailable_discovery_is_cached_and_refreshed(tmp_path, monkeypatch):
    clock = [0]
    calls = []
    monkeypatch.setattr(avatars.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(avatars, "_codex_asar", lambda: calls.append(1))
    assert avatars.codex_avatars(tmp_path) == avatars.codex_avatars(tmp_path) == []
    clock[0] += avatars._DISCOVERY_TTL
    assert avatars.codex_avatars(tmp_path) == [] and len(calls) == 2


def test_malformed_bundle_returns_empty(tmp_path, monkeypatch):
    asar = tmp_path / "v" / "app" / "resources" / "app.asar"
    asar.parent.mkdir(parents=True)
    asar.write_bytes(b"bad")
    monkeypatch.setattr(avatars, "_codex_asar", lambda: asar)
    monkeypatch.setattr(avatars, "_extract", lambda path: (_ for _ in ()).throw(ValueError("bad header")))
    assert avatars.codex_avatars(tmp_path) == []
