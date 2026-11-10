import json
import os

import pytest

from token_tracker.claude_identity import IdentityAuthority, account_path, read_passive_account, desktop_matches


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO regression")
def test_passive_identity_rejects_fifo_without_writer(tmp_path):
    path = tmp_path / "account-fifo"
    os.mkfifo(path)
    assert read_passive_account(path) is None


def test_identity_epoch_fences_switch_rotation_and_restart(tmp_path):
    authority = IdentityAuthority()
    first = authority.credential_changed((str(tmp_path / "a"), "token-a"))
    a = authority.bind(first, "account-a", "org")
    assert a is not None
    second = authority.credential_changed((str(tmp_path / "b"), "token-b"))
    assert second != first
    assert authority.bind(first, "account-a", "org") is None
    b = authority.bind(second, "account-b", "org")
    assert b is not None and b.account_uuid != a.account_uuid
    third = authority.credential_changed((str(tmp_path / "a"), "token-a"))
    assert third not in (first, second)
    assert authority.bind(third, "account-a", "org") is not None
    with authority.guard(first) as stale:
        assert stale is None
    assert IdentityAuthority().capture()[0] != third


def test_identity_rejects_missing_or_late_profile(tmp_path):
    authority = IdentityAuthority()
    epoch = authority.credential_changed(str(tmp_path))
    assert authority.bind(epoch, "", "org") is None
    assert authority.bind(epoch, "account", " ") is None
    authority.invalidate()
    assert authority.bind(epoch, "account", "org") is None


def test_stale_failure_cannot_invalidate_new_identity(tmp_path):
    authority = IdentityAuthority()
    old = authority.credential_changed((str(tmp_path), "old"))
    new = authority.credential_changed((str(tmp_path), "new"))
    assert authority.bind(new, "account", "org") is not None
    assert authority.invalidate(expected_generation=old) == new
    assert authority.capture()[1].generation == new


def test_account_switch_in_same_credential_epoch_invalidates_old_generation(tmp_path):
    authority = IdentityAuthority()
    epoch = authority.credential_changed((str(tmp_path), "token"))
    assert authority.bind(epoch, "account-a", "same-org") is not None
    changed = authority.bind(epoch, "account-b", "same-org")
    assert changed is not None and changed.generation != epoch
    assert authority.bind(epoch, "account-a", "same-org") is None


def test_passive_identity_switch_disagreement_and_removal(tmp_path):
    path = tmp_path / ".claude.json"
    def write(account, org, email="person@example.test"):
        path.write_text(json.dumps({"oauthAccount": {"accountUuid": account,
                        "organizationUuid": org, "emailAddress": email}}), encoding="utf-8")
    authority = IdentityAuthority()
    write("a", "org")
    first = authority.refresh_passive(path)
    assert first.label == "person@example.test"
    assert "person@example.test" not in repr(first)
    write("b", "org")
    second = authority.refresh_passive(path)
    assert second.generation != first.generation
    assert authority.refresh_passive(path, other=("a", "org")) is None
    assert authority.capture()[1] is None
    write("a", "org", "")
    third = authority.refresh_passive(path)
    assert third.generation not in (first.generation, second.generation)
    assert third.label == "a"
    path.unlink()
    assert authority.refresh_passive(path) is None
    fallback = authority.refresh_passive(path, other=("a", "org"))
    assert fallback is not None and fallback.account_uuid == "a"


def test_passive_identity_rejects_oversized_file(tmp_path):
    path = tmp_path / "account"
    path.write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
    assert read_passive_account(path) is None


def test_passive_refresh_does_not_erase_credential_epoch(tmp_path):
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({"oauthAccount": {"accountUuid": "a", "organizationUuid": "org"}}), encoding="utf-8")
    authority = IdentityAuthority()
    authority.credential_changed("token-a")
    first = authority.refresh_passive(path)
    assert authority.refresh_passive(path).generation == first.generation
    next_epoch = authority.credential_changed("token-b")
    assert next_epoch != first.generation
    assert authority.refresh_passive(path).generation == next_epoch


def test_configured_account_path(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert account_path() == tmp_path / ".claude.json"


def test_same_account_restart_rebinds_fingerprint_and_change_time(tmp_path):
    from token_tracker import claude_statusline
    account = tmp_path / ".claude.json"
    state = tmp_path / "identity.json"
    account.write_text(json.dumps({"oauthAccount": {"accountUuid": "account-a", "organizationUuid": "org-a"}}))
    first = IdentityAuthority(state, clock=lambda: 100).refresh_passive(account)
    windows = {"five_hour": {"used_percentage": 12, "resets_at": 2000, "receivedAt": 1000}}
    claude_statusline.write(windows, tmp_path, now=1000, filename=claude_statusline.CLI_FILENAME,
                            source="claude-cli", org_id="org-a", identity_fingerprint=first.fingerprint)
    second = IdentityAuthority(state, clock=lambda: 200).refresh_passive(account)
    assert first.fingerprint == second.fingerprint and second.changed_at == 100
    assert first.generation != second.generation
    assert "account-a" not in state.read_text() and "org-a" not in state.read_text()
    cli = claude_statusline.read(tmp_path / claude_statusline.CLI_FILENAME, now=1000,
                                 source="claude-cli", include_org=True)
    assert claude_statusline.merge_usage({"fiveHour": None, "weekly": None, "fiveHourPercent": None,
                                         "weeklyPercent": None, "observedAt": None}, [], now=1000,
                                        org_id="org-a", cli_path=tmp_path / claude_statusline.CLI_FILENAME,
                                        identity_generation=second.generation, identity_fingerprint=second.fingerprint)["fiveHourPercent"] == 12
    assert cli["identityFingerprint"] == second.fingerprint


def test_account_switch_restart_rejects_old_fingerprint(tmp_path):
    from token_tracker import claude_statusline
    account, state = tmp_path / ".claude.json", tmp_path / "identity.json"
    account.write_text(json.dumps({"oauthAccount": {"accountUuid": "a", "organizationUuid": "org"}}))
    first = IdentityAuthority(state, clock=lambda: 100).refresh_passive(account)
    claude_statusline.write({"five_hour": {"used_percentage": 12, "resets_at": 2000, "receivedAt": 1000}},
                            tmp_path, now=1000, filename=claude_statusline.CLI_FILENAME,
                            source="claude-cli", org_id="org", identity_fingerprint=first.fingerprint)
    account.write_text(json.dumps({"oauthAccount": {"accountUuid": "b", "organizationUuid": "org"}}))
    second = IdentityAuthority(state, clock=lambda: 200).refresh_passive(account)
    assert first.fingerprint != second.fingerprint and second.changed_at == 200
    cli = claude_statusline.read(tmp_path / claude_statusline.CLI_FILENAME, now=1000,
                                 source="claude-cli", include_org=True)
    assert cli["identityFingerprint"] != second.fingerprint


def test_desktop_observation_must_follow_account_change(tmp_path):
    account = tmp_path / ".claude.json"
    account.write_text(json.dumps({"oauthAccount": {"accountUuid": "a", "organizationUuid": "org"}}))
    identity = IdentityAuthority(tmp_path / "identity.json", clock=lambda: 200).refresh_passive(account)
    assert not desktop_matches(identity, "org", 199)
    assert desktop_matches(identity, "org", 200)
    assert not desktop_matches(identity, "other", 201)


def test_corrupt_huge_changed_at_is_replaced(tmp_path):
    account, state = tmp_path / ".claude.json", tmp_path / "identity.json"
    account.write_text(json.dumps({"oauthAccount": {"accountUuid": "a", "organizationUuid": "org"}}))
    state.write_text(json.dumps({"fingerprint": "a" * 64, "changedAt": 10**400}))
    identity = IdentityAuthority(state, clock=lambda: 123).refresh_passive(account)
    assert identity is not None and identity.changed_at == 123
    assert json.loads(state.read_text())["changedAt"] == 123


def test_busy_lock_serves_last_identity_instead_of_reporting_a_change(tmp_path):
    import threading
    authority = IdentityAuthority()
    generation = authority.credential_changed((str(tmp_path / "a"), "token-a"))
    bound = authority.bind(generation, "account-a", "org")
    held, release = threading.Event(), threading.Event()

    def hold():
        with authority.guard(generation):
            held.set()
            release.wait(5)

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert held.wait(5)
        assert authority.capture_nonblocking() == (generation, bound)
    finally:
        release.set()
        worker.join(5)
    changed = authority.credential_changed((str(tmp_path / "b"), "token-b"))
    assert authority.capture_nonblocking() == (changed, None)
