"""Normalized usage fields, pricing and fact keys for the work ledger.

The reducers and price tables are reused from providers.py and claude_provider.py.
This module only maps their outputs onto ledger fields:

- inputTotal includes every input token; cachedRead and the cache-write tiers are subsets.
- outputTotal includes reasoning output; total = inputTotal + outputTotal.
- A breakdown the provider did not report is None (unknown), never 0.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from . import providers
from .claude_provider import _usage_values

CLAUDE = "Claude"
CODEX = "Codex"
PROVIDERS = (CLAUDE, CODEX)
STANDARD_TIER = "standard"
LONG_CONTEXT_TIER = "long-context"

# Nullable breakdowns (None = not reported). Reasoning output never changes the cost,
# because it is already inside outputTotal.
NULLABLE_FIELDS = ("cached_read", "cache_write_5m", "cache_write_1h", "cache_write_unknown", "reasoning_output")
COST_NULLABLE_FIELDS = {
    CLAUDE: ("cached_read", "cache_write_5m", "cache_write_1h", "cache_write_unknown"),
    CODEX: ("cached_read",),
}
# Cumulative Codex counters, in the order the reducer compares them.
COUNTER_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens", "total_tokens")
_MODEL_TEXT = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_|"


def count(value: Any) -> int | None:
    """A non-negative integer from provider JSON, else None (bools and text are rejected)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def raw_model(value: Any) -> str:
    """Model ids are kept verbatim, but only when they look like an id (no free text)."""
    if isinstance(value, str) and 0 < len(value) <= 64 and all(char in _MODEL_TEXT for char in value):
        return value
    return "unknown"


def fact_key(provider: str, *parts: Any) -> str:
    """Opaque dedup key. Hashing keeps provider response ids out of the store."""
    text = "\x1f".join([provider, *(str(part) for part in parts)])
    return hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=16).hexdigest()


def claude_usage(usage: Any) -> dict[str, Any] | None:
    """Normalized fields for one Claude response usage block; None when it holds no tokens."""
    if not isinstance(usage, dict):
        return None
    input_tokens, output_tokens, cache_read, write_5m, write_1h = _usage_values(usage)
    breakdown = isinstance(usage.get("cache_creation"), dict) and bool(usage["cache_creation"])
    if breakdown:
        cache_5m, cache_1h, cache_unknown = write_5m, write_1h, 0
    elif count(usage.get("cache_creation_input_tokens")) is not None:
        # Total cache writes without a tier split: the tier is unknown, not zero.
        cache_5m = cache_1h = None
        cache_unknown = write_5m
    else:
        cache_5m = cache_1h = cache_unknown = None
    cached = cache_read if count(usage.get("cache_read_input_tokens")) is not None else None
    input_total = input_tokens + (cached or 0) + (cache_5m or 0) + (cache_1h or 0) + (cache_unknown or 0)
    fields = {
        "input_total": input_total, "output_total": output_tokens, "total": input_total + output_tokens,
        "cached_read": cached, "cache_write_5m": cache_5m, "cache_write_1h": cache_1h,
        "cache_write_unknown": cache_unknown, "reasoning_output": None,
    }
    return fields if fields["total"] > 0 else None


def codex_usage(delta: Mapping[str, int], present: frozenset[str]) -> dict[str, Any]:
    """Normalized fields for one Codex counter delta (see providers._counter_delta)."""
    input_tokens = int(delta["input_tokens"])
    output_tokens = int(delta["output_tokens"])
    cached = min(int(delta["cached_input_tokens"]), input_tokens) if "cached_input_tokens" in present else None
    cache_unknown = (min(int(delta["cache_write_input_tokens"]), input_tokens)
                     if "cache_write_input_tokens" in present else None)
    reasoning = (min(int(delta["reasoning_output_tokens"]), output_tokens)
                 if "reasoning_output_tokens" in present else None)
    # Codex has no 5m/1h cache tiers, so those are zero by definition, not unknown.
    return {
        "input_total": input_tokens, "output_total": output_tokens, "total": input_tokens + output_tokens,
        "cached_read": cached, "cache_write_5m": 0, "cache_write_1h": 0,
        "cache_write_unknown": cache_unknown, "reasoning_output": reasoning,
    }


def codex_long_rule(model: str) -> tuple[int, float, float] | None:
    """The long-context rule that applies to this model, as the reducer finds it."""
    return next((rule for name, rule in providers.CODEX_LONG_CONTEXT.items()
                 if providers._price_for(model, {name: ()}) is not None), None)


def codex_price_tier(model: str, last_input: int | None) -> str:
    rule = codex_long_rule(model)
    if rule and (last_input or 0) > rule[0]:
        return LONG_CONTEXT_TIER
    return STANDARD_TIER


def price_usd(provider: str, model: str, tier: str, sums: Mapping[str, int]) -> float | None:
    """API-equivalent cost of summed normalized tokens; None when the model has no list price.

    Mirrors providers._costs. Nullable fields are summed as 0 by the caller, so
    completeness is reported separately.
    """
    if provider == CLAUDE:
        rates = providers._price_for(model, providers.CLAUDE_PRICING)
        if not rates:
            return None
        cached = sums["cached_read"]
        write_5m, write_1h, write_unknown = sums["cache_write_5m"], sums["cache_write_1h"], sums["cache_write_unknown"]
        uncached = sums["input_total"] - cached - write_5m - write_1h - write_unknown
        return (uncached * rates[0] + cached * rates[1] + (write_5m + write_unknown) * rates[2]
                + write_1h * rates[3] + sums["output_total"] * rates[4]) / 1_000_000
    rates = providers._price_for(model, providers.CODEX_PRICING)
    if not rates:
        return None
    if tier == LONG_CONTEXT_TIER:
        rule = codex_long_rule(model)
        if rule:
            rates = (rates[0] * rule[1], rates[1] * rule[1], rates[2] * rule[2])
    cached = sums["cached_read"]
    uncached = sums["input_total"] - cached
    return (uncached * rates[0] + cached * rates[1] + sums["output_total"] * rates[2]) / 1_000_000


def pricing_version() -> str:
    """Changes whenever a rate, multiplier or alias changes, so stored costs can be re-derived."""
    payload = json.dumps({
        "claude": providers.CLAUDE_PRICING, "codex": providers.CODEX_PRICING,
        "codexLongContext": providers.CODEX_LONG_CONTEXT, "aliases": providers.PRICE_MODEL_ALIASES,
    }, sort_keys=True, default=list)
    return "p-" + hashlib.blake2b(payload.encode("utf-8"), digest_size=6).hexdigest()
