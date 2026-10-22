"""Immutable provider collection, disclosure, and send permissions."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


PROVIDERS = ("Codex", "Claude")
_ENV_NAMES = {"Codex": "CODEX", "Claude": "CLAUDE"}
_ACTIONS = frozenset({"collect", "show", "greet", "continue"})
_CAPABILITIES = {
    "Codex": frozenset({"greet", "continue"}),
    "Claude": frozenset({"greet"}),
}
_TRUE = frozenset({"1", "true", "on"})
_FALSE = frozenset({"0", "false", "off"})


class ProviderPolicyError(ValueError):
    """Raised when provider policy is invalid or a requested action is denied."""


@dataclass(frozen=True)
class ProviderPolicy:
    name: str
    collect: bool
    show: bool
    send: bool
    send_origin: str
    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        if any(type(flag) is not bool for flag in (self.collect, self.show, self.send)):
            raise ProviderPolicyError("provider policy flags must be booleans")
        if not isinstance(self.capabilities, frozenset) or any(not isinstance(item, str) for item in self.capabilities):
            raise ProviderPolicyError("provider capabilities must be an immutable string set")
        if self.name in _CAPABILITIES and not self.capabilities <= _CAPABILITIES[self.name]:
            raise ProviderPolicyError(f"unsupported capability for provider: {self.name}")
        if self.send_origin not in {"provider_env", "legacy_global", "default", "provider_denied",
                                    "legacy_veto", "collection_denied"}:
            raise ProviderPolicyError("unknown send origin")

    def allows(self, action: str) -> bool:
        if action not in _ACTIONS:
            return False
        if action == "collect":
            return self.collect
        if action == "show":
            return self.collect and self.show
        if not self.collect:
            return False
        if action == "continue":
            return self.send and self.send_origin == "provider_env" and action in self.capabilities
        return (self.send and (self.send_origin == "provider_env"
                               or self.name == "Codex" and self.send_origin == "legacy_global")
                and action in self.capabilities)

    def require(self, action: str) -> None:
        if not self.allows(action):
            raise ProviderPolicyError(f"{self.name} policy does not allow {action}")


def _read_bool(environ: Mapping[str, str], name: str) -> tuple[bool | None, bool]:
    raw = environ.get(name)
    if raw is None:
        return None, False
    if not isinstance(raw, str):
        raise ProviderPolicyError(f"{name} must be a boolean string")
    value = raw.strip().lower()
    if value in _TRUE:
        return True, True
    if value in _FALSE:
        return False, True
    raise ProviderPolicyError(f"{name} must be one of 1, true, on, 0, false, or off")


def load_policies(environ: Mapping[str, str] | None = None) -> Mapping[str, ProviderPolicy]:
    """Parse strict environment flags once and return an immutable policy map."""
    env = os.environ if environ is None else environ
    legacy, legacy_explicit = _read_bool(env, "TOKEN_TRACKER_LIMIT_RESET")
    result: dict[str, ProviderPolicy] = {}
    for name in PROVIDERS:
        prefix = f"TOKEN_TRACKER_{_ENV_NAMES[name]}_"
        collect_value, collect_explicit = _read_bool(env, prefix + "COLLECT")
        show_value, show_explicit = _read_bool(env, prefix + "SHOW")
        send_value, send_explicit = _read_bool(env, prefix + "SEND")

        collect = collect_value if collect_explicit else name == "Codex"
        show = show_value if show_explicit else (name == "Codex" and collect)
        if show and not collect:
            raise ProviderPolicyError(f"{prefix}SHOW cannot be enabled while collection is disabled")

        if legacy_explicit and legacy is False and send_explicit and send_value:
            raise ProviderPolicyError(f"{prefix}SEND conflicts with TOKEN_TRACKER_LIMIT_RESET=false")

        if send_explicit:
            send = bool(send_value)
            send_origin = "provider_env" if send else "provider_denied"
        elif name == "Codex" and legacy_explicit and legacy:
            send = True
            send_origin = "legacy_global"
        else:
            send = False
            send_origin = "legacy_veto" if legacy_explicit and legacy is False else "default"

        # Explicit provider SEND is a configuration error if it asks for an
        # action whose local data collection was explicitly denied. Legacy
        # greeting consent cannot override a provider-specific collection veto.
        if send and not collect:
            if send_explicit:
                raise ProviderPolicyError(f"{prefix}SEND cannot be enabled while collection is disabled")
            send = False
            send_origin = "collection_denied"

        result[name] = ProviderPolicy(name, bool(collect), bool(show), send, send_origin, _CAPABILITIES[name])

    return MappingProxyType(result)


POLICIES = load_policies()


def policy_for(name: str, policies: Mapping[str, ProviderPolicy] | None = None) -> ProviderPolicy:
    if name not in PROVIDERS:
        raise ProviderPolicyError(f"unknown provider: {name}")
    source = POLICIES if policies is None else policies
    try:
        policy = source[name]
    except (KeyError, TypeError) as exc:
        raise ProviderPolicyError(f"unknown provider: {name}") from exc
    if not isinstance(policy, ProviderPolicy) or policy.name != name:
        raise ProviderPolicyError(f"invalid policy for provider: {name}")
    return policy


def public_policies(policies: Mapping[str, ProviderPolicy] | None = None) -> dict:
    source = POLICIES if policies is None else policies
    rows = []
    for name in PROVIDERS:
        policy = policy_for(name, source)
        rows.append({
            "name": policy.name,
            "collect": policy.collect,
            "show": policy.show,
            "send": policy.send,
            "sendOrigin": policy.send_origin,
            "capabilities": {capability: capability in policy.capabilities
                             for capability in ("greet", "continue")},
        })
    material = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    generation = hashlib.sha256(material).hexdigest()[:16]
    return {"schemaVersion": 1, "generation": generation, "providers": rows}
