"""Immutable provider collection, disclosure, and send permissions."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
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


_FILE_SCHEMA_VERSION = 1
_FILE_MAX_BYTES = 64 * 1024
_FILE_FLAGS = ("collect", "show", "send")
_FILE_TOP_KEYS = frozenset({"schemaVersion", "providers", "origin", "writtenAt"})


@dataclass(frozen=True)
class PolicyFile:
    """Strictly validated contents of the installer-written policy file."""

    providers: Mapping[str, Mapping[str, bool]]
    claude_consent: bool = False


@dataclass(frozen=True)
class PolicyFileResult:
    status: str  # "absent" | "ok" | "malformed"
    policy: PolicyFile | None = None
    warning: str | None = None


def _validate_policy_document(document: object) -> dict[str, dict[str, bool]]:
    if not isinstance(document, dict):
        raise ProviderPolicyError("policy file must contain a JSON object")
    if set(document) - _FILE_TOP_KEYS:
        raise ProviderPolicyError("policy file has unknown keys")
    version = document.get("schemaVersion")
    if type(version) is not int or version != _FILE_SCHEMA_VERSION:
        raise ProviderPolicyError("policy file schemaVersion must be 1")
    for key in ("origin", "writtenAt"):
        if key in document and not isinstance(document[key], str):
            raise ProviderPolicyError(f"policy file {key} must be a string")
    providers = document.get("providers")
    if not isinstance(providers, dict) or set(providers) - set(PROVIDERS):
        raise ProviderPolicyError("policy file providers must name only Codex and Claude")
    result: dict[str, dict[str, bool]] = {}
    for name, flags in providers.items():
        if not isinstance(flags, dict) or set(flags) - set(_FILE_FLAGS):
            raise ProviderPolicyError(f"policy file flags for {name} are invalid")
        if any(type(value) is not bool for value in flags.values()):
            raise ProviderPolicyError(f"policy file flags for {name} must be booleans")
        if (flags.get("show") or flags.get("send")) and not flags.get("collect"):
            raise ProviderPolicyError(f"policy file enables show/send for {name} without collection")
        result[name] = dict(flags)
    return result


def read_policy_file(path: Path | str | None, *, claude_consent: bool = False) -> PolicyFileResult:
    """Read the policy file once. Never raises: problems become a warning."""
    if path is None:
        return PolicyFileResult("absent")
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return PolicyFileResult("absent")
    except OSError:
        return PolicyFileResult("malformed", warning="policy file could not be inspected; defaults are in force")
    reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if not stat.S_ISREG(info.st_mode) or reparse or info.st_size > _FILE_MAX_BYTES:
        return PolicyFileResult("malformed", warning="policy file is not a plain small file; defaults are in force")
    try:
        document = json.loads(path.read_bytes().decode("utf-8-sig"))
        providers = _validate_policy_document(document)
    except (OSError, ValueError, ProviderPolicyError, RecursionError) as exc:
        # RecursionError: pathologically nested JSON is malformed input, not a crash.
        if isinstance(exc, ProviderPolicyError):
            reason = str(exc)
        elif isinstance(exc, RecursionError):
            reason = "nested too deeply"
        else:
            reason = "not valid JSON"
        return PolicyFileResult("malformed", warning=f"policy file ignored ({reason}); defaults are in force")
    return PolicyFileResult("ok", PolicyFile(MappingProxyType(providers), bool(claude_consent)))


class PolicyMap(Mapping):
    """Immutable provider policy mapping that also carries public provenance metadata."""

    __slots__ = ("_items", "meta")

    def __init__(self, items: Mapping[str, ProviderPolicy], meta: Mapping[str, object] | None = None) -> None:
        self._items = dict(items)
        self.meta = MappingProxyType(dict(meta or {"origin": "defaults", "policyFile": "absent", "warnings": ()}))

    def __getitem__(self, key: str) -> ProviderPolicy:
        return self._items[key]

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)


def load_policies(environ: Mapping[str, str] | None = None, *,
                  policy_file: PolicyFile | None = None,
                  policy_file_status: str | None = None,
                  warnings: tuple[str, ...] = ()) -> Mapping[str, ProviderPolicy]:
    """Parse strict environment flags (and an optional validated policy file) once.

    Precedence per flag: explicit provider env flag, then the legacy global env
    flag (Codex greeting only), then the policy file, then the default. An
    explicit ``0`` in the environment vetoes a file grant.
    """
    env = os.environ if environ is None else environ
    legacy, legacy_explicit = _read_bool(env, "TOKEN_TRACKER_LIMIT_RESET")
    notes: list[str] = list(warnings)
    result: dict[str, ProviderPolicy] = {}
    env_used = legacy_explicit
    file_used = False
    for name in PROVIDERS:
        prefix = f"TOKEN_TRACKER_{_ENV_NAMES[name]}_"
        collect_value, collect_explicit = _read_bool(env, prefix + "COLLECT")
        show_value, show_explicit = _read_bool(env, prefix + "SHOW")
        send_value, send_explicit = _read_bool(env, prefix + "SEND")
        env_used = env_used or collect_explicit or show_explicit or send_explicit

        file_flags: dict[str, bool] = {}
        if policy_file is not None:
            file_flags = dict(policy_file.providers.get(name, {}))
            if name == "Claude" and not policy_file.claude_consent and (
                    file_flags.get("collect") or file_flags.get("show")):
                notes.append("policy file enables Claude collection but the Claude consent marker is missing; ignored")
                file_flags = {}
            if name == "Claude" and file_flags.pop("send", False):
                notes.append("policy file Claude send is not supported and was ignored")

        file_collect = file_flags.get("collect")
        file_show = file_flags.get("show")
        file_send = file_flags.get("send")
        if collect_explicit:
            collect = bool(collect_value)
        elif file_collect is not None:
            collect, file_used = file_collect, True
        else:
            collect = name == "Codex"

        if show_explicit:
            show = bool(show_value)
        elif file_show is not None:
            show, file_used = file_show, True
            if show and not collect:
                show = False  # an env veto of collection downgrades a file grant
                notes.append(f"policy file show for {name} ignored because collection is disabled")
        else:
            show = name == "Codex" and collect
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
        elif legacy_explicit and legacy is False:
            send, send_origin = False, "legacy_veto"
        elif name == "Codex" and file_send is not None:
            file_used = True
            if file_send:
                # Greeting-only semantics, identical to the legacy global flag.
                send, send_origin = True, "legacy_global"
            else:
                send, send_origin = False, "provider_denied"
        else:
            send, send_origin = False, "default"

        # Explicit provider SEND is a configuration error if it asks for an
        # action whose local data collection was explicitly denied. Legacy
        # greeting consent cannot override a provider-specific collection veto.
        if send and not collect:
            if send_explicit:
                raise ProviderPolicyError(f"{prefix}SEND cannot be enabled while collection is disabled")
            send = False
            send_origin = "collection_denied"

        result[name] = ProviderPolicy(name, bool(collect), bool(show), send, send_origin, _CAPABILITIES[name])

    if env_used and file_used:
        origin = "policy_file+environment"
    elif file_used:
        origin = "policy_file"
    elif env_used:
        origin = "environment"
    else:
        origin = "defaults"
    status = policy_file_status or ("ok" if policy_file is not None else "absent")
    return PolicyMap(result, {"origin": origin, "policyFile": status, "warnings": tuple(notes)})


def load_policies_resilient(environ: Mapping[str, str] | None = None, *,
                            policy_path: Path | str | None = None,
                            claude_consent: bool = False) -> Mapping[str, ProviderPolicy]:
    """Startup loader for the windowed app: never raises, reports problems via metadata."""
    file_result = read_policy_file(policy_path, claude_consent=claude_consent)
    warnings = (file_result.warning,) if file_result.warning else ()
    try:
        return load_policies(environ, policy_file=file_result.policy,
                             policy_file_status=file_result.status, warnings=warnings)
    except ProviderPolicyError as exc:
        # Conflicting or malformed environment flags: fail closed, visibly.
        locked = {name: ProviderPolicy(name, False, False, False, "default", _CAPABILITIES[name])
                  for name in PROVIDERS}
        return PolicyMap(locked, {
            "origin": "environment_error",
            "policyFile": file_result.status,
            "warnings": warnings + (f"provider environment flags rejected ({exc}); all providers disabled",),
        })


def _startup_policies() -> Mapping[str, ProviderPolicy]:
    from . import product
    path = product.policy_file_path()
    consent = product.claude_consent_present() if path is not None else False
    return load_policies_resilient(policy_path=path, claude_consent=consent)


POLICIES = _startup_policies()


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
    meta = getattr(source, "meta", None) or {"origin": "injected", "policyFile": "absent", "warnings": ()}
    return {"schemaVersion": 1, "generation": generation, "providers": rows,
            "origin": meta["origin"], "policyFile": meta["policyFile"],
            "warnings": list(meta["warnings"])}
