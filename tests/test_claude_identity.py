import json

from token_tracker.claude_identity import IdentityAuthority, account_path, read_passive_account


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
