from __future__ import annotations

import pytest

from token_tracker.provider_policy import (
    ProviderPolicy,
    ProviderPolicyError,
    load_policies,
    policy_for,
    public_policies,
)


def test_defaults_are_codex_read_only_and_claude_disabled():
    policies = load_policies({})

    assert policies["Codex"].collect and policies["Codex"].show
    assert not policies["Codex"].send
    assert not policies["Claude"].collect
    assert not policies["Claude"].show
    assert not policies["Claude"].send
    assert policies["Codex"].capabilities == frozenset({"greet", "continue"})
    assert policies["Claude"].capabilities == frozenset({"greet"})


def test_legacy_true_grants_only_codex_greeting():
    policies = load_policies({"TOKEN_TRACKER_LIMIT_RESET": " TRUE "})

    assert policy_for("Codex", policies).allows("greet")
    assert not policy_for("Codex", policies).allows("continue")
    assert not policy_for("Claude", policies).allows("greet")
    assert policies["Codex"].send_origin == "legacy_global"


def test_malformed_claude_legacy_origin_cannot_greet():
    policy = ProviderPolicy("Claude", True, True, True, "legacy_global", frozenset({"greet"}))
    assert not policy.allows("greet")


def test_legacy_false_is_global_send_veto_and_conflicts_with_send_true():
    assert not load_policies({"TOKEN_TRACKER_LIMIT_RESET": "off"})["Codex"].send
    with pytest.raises(ProviderPolicyError):
        load_policies({"TOKEN_TRACKER_LIMIT_RESET": "false", "TOKEN_TRACKER_CODEX_SEND": "true"})


def test_explicit_provider_denial_overrides_legacy_grant():
    policies = load_policies({
        "TOKEN_TRACKER_LIMIT_RESET": "true",
        "TOKEN_TRACKER_CODEX_SEND": "false",
    })
    assert not policies["Codex"].allows("greet")


def test_claude_requires_explicit_collection_before_send():
    with pytest.raises(ProviderPolicyError):
        load_policies({"TOKEN_TRACKER_CLAUDE_SEND": "true"})

    policy = load_policies({
        "TOKEN_TRACKER_CLAUDE_COLLECT": "on",
        "TOKEN_TRACKER_CLAUDE_SHOW": "on",
        "TOKEN_TRACKER_CLAUDE_SEND": "true",
    })["Claude"]
    assert policy.allows("collect") and policy.allows("show") and policy.allows("greet")
    assert not policy.allows("continue")


@pytest.mark.parametrize("env", [
    {"TOKEN_TRACKER_CLAUDE_SHOW": "true"},
    {"TOKEN_TRACKER_CODEX_SHOW": "true", "TOKEN_TRACKER_CODEX_COLLECT": "false"},
    {"TOKEN_TRACKER_CODEX_SEND": "true", "TOKEN_TRACKER_CODEX_COLLECT": "false"},
])
def test_show_or_send_cannot_bypass_collection(env):
    with pytest.raises(ProviderPolicyError):
        load_policies(env)


@pytest.mark.parametrize("name,value", [
    ("TOKEN_TRACKER_CLAUDE_COLLECT", "yes"),
    ("TOKEN_TRACKER_CODEX_SHOW", "enabled"),
    ("TOKEN_TRACKER_LIMIT_RESET", "2"),
])
def test_invalid_flags_fail_closed(name, value):
    with pytest.raises(ProviderPolicyError):
        load_policies({name: value})


def test_policy_mapping_is_immutable_and_public_generation_is_stable():
    policies = load_policies({})
    with pytest.raises(TypeError):
        policies["Codex"] = policies["Claude"]
    public = public_policies(policies)
    assert public == public_policies(load_policies({}))
    assert public["schemaVersion"] == 1
    assert all(set(row) == {"name", "collect", "show", "send", "sendOrigin", "capabilities"}
               for row in public["providers"])
    assert all(isinstance(row["capabilities"], dict) for row in public["providers"])


def test_unknown_provider_and_action_fail_closed():
    with pytest.raises(ProviderPolicyError):
        policy_for("Other", load_policies({}))
    assert not load_policies({})["Codex"].allows("run arbitrary")
    injected = ProviderPolicy("Other", True, True, True, "provider_env", frozenset({"greet"}))
    with pytest.raises(ProviderPolicyError):
        policy_for("Other", {"Other": injected})


def test_injected_malformed_policy_cannot_bypass_collection_boundary():
    malformed = ProviderPolicy("Codex", False, True, True, "provider_env",
                               frozenset({"greet", "continue"}))
    assert not malformed.allows("show")
    assert not malformed.allows("greet")
    assert not malformed.allows("continue")
    no_origin = ProviderPolicy("Codex", True, True, True, "default",
                               frozenset({"greet", "continue"}))
    assert not no_origin.allows("greet")
    assert not no_origin.allows("continue")
