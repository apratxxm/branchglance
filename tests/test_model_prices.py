import pytest

from token_tracker import providers

# Models the user runs or can select (official pricing pages checked 2026-11-02).
KNOWN_CLAUDE = [
    "claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
    "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5", "claude-sonnet-5-5", "claude-sonnet-5",
    "claude-sonnet-4-6", "claude-sonnet-4-5", "claude-haiku-5-5", "claude-haiku-4-5",
]
KNOWN_CODEX = [
    "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-sol", "gpt-6-luna", "gpt-6-astra",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini",
    "gpt-5.3-codex",
]


@pytest.mark.parametrize("model", KNOWN_CLAUDE)
def test_every_known_claude_model_has_a_price(model):
    assert not providers._model_unpriced(model, providers.CLAUDE_PRICING), model


@pytest.mark.parametrize("model", KNOWN_CODEX)
def test_every_known_codex_model_has_a_price(model):
    assert not providers._model_unpriced(model, providers.CODEX_PRICING), model


@pytest.mark.parametrize("model", KNOWN_CLAUDE)
def test_claude_id_variants_resolve_to_the_same_row(model):
    base = providers._price_for(model, providers.CLAUDE_PRICING)
    for variant in (model + "-20251001", model + "[1m]", "anthropic/" + model,
                    model.upper(), model + providers.LONG_CONTEXT_SUFFIX):
        assert providers._price_for(variant, providers.CLAUDE_PRICING) == base, variant


@pytest.mark.parametrize("model", KNOWN_CODEX)
def test_codex_id_variants_resolve_to_the_same_row(model):
    base = providers._price_for(model, providers.CODEX_PRICING)
    for variant in (model + "-2026-03-05", "openai/" + model, model.upper(),
                    model + providers.LONG_CONTEXT_SUFFIX):
        assert providers._price_for(variant, providers.CODEX_PRICING) == base, variant


@pytest.mark.parametrize("unknown", ["claude-opus-6", "claude-haiku-3-5", "claude-sonnet-5-5-preview",
                                     "gpt-6.1-terra-preview", "gpt-5.5-pro", "gpt-6.1-sol-unlisted"])
def test_unknown_or_unlisted_models_are_flagged_unpriced(unknown):
    table = providers.CLAUDE_PRICING if unknown.startswith("claude") else providers.CODEX_PRICING
    assert providers._model_unpriced(unknown, table)


def test_every_alias_target_and_long_context_key_exists_in_a_table():
    for alias, target in providers.PRICE_MODEL_ALIASES.items():
        assert target in providers.CLAUDE_PRICING or target in providers.CODEX_PRICING, alias
    assert set(providers.CODEX_LONG_CONTEXT) <= set(providers.CODEX_PRICING)


def test_price_rows_have_the_expected_shape():
    assert all(len(rates) == 5 and all(value > 0 for value in rates)
               for rates in providers.CLAUDE_PRICING.values())
    assert all(len(rates) == 3 and all(value > 0 for value in rates)
               for rates in providers.CODEX_PRICING.values())


def test_verified_claude_rows_match_the_official_table():
    # Sonnet 5.5 cache hits are 0.05x input ($0.10/MTok), not 0.1x.
    assert providers._price_for("claude-sonnet-5-5", providers.CLAUDE_PRICING) == (2, .1, 2.5, 4, 10)
    # Haiku 5.5 base row covers prompts up to 100K tokens.
    assert providers._price_for("claude-haiku-5-5", providers.CLAUDE_PRICING) == (.1, .01, .125, .2, .5)
    assert providers.HAIKU_5_5_LONG_PROMPT == (.5, .05, .625, 1, 2.5)


def test_haiku_unsized_aggregate_remains_low_estimate():
    model = "claude-haiku-5-5"
    assert providers._price_estimate(model, {"total_tokens": 100, "sized_total_tokens": 99}) == "low"
    assert providers._price_estimate(model, {"total_tokens": 100, "sized_total_tokens": 100}) is None


def test_verified_codex_rows_match_the_official_table():
    assert providers._price_for("gpt-6.1-terra", providers.CODEX_PRICING) == (1, .05, 5)
    assert providers._price_for("gpt-5.4", providers.CODEX_PRICING) == (2.5, .25, 15)
    assert providers.CODEX_LONG_CONTEXT["gpt-5.6-terra"] == (272_000, 2.0, 1.5)
    assert "gpt-5.4-mini" not in providers.CODEX_LONG_CONTEXT


def _payload_models(tmp_path, provider, daily):
    from datetime import datetime, timezone

    from token_tracker import insights

    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    usage = {"input_tokens": 1000, "cached_tokens": 0, "output_tokens": 0,
             "cache_5m_tokens": 0, "cache_1h_tokens": 0, "total_tokens": 1000}
    snapshot = {"providers": [{"name": provider, "_insightsData": {
        "dailyModels": {"2026-11-02": {model: dict(usage) for model in daily}}, "hourlyModels": {}}}]}
    peaks = insights.LimitPeakStore(tmp_path / "peaks.jsonl", owner_check=lambda: False)
    result = insights.build_insights(snapshot, "30d", now=now, peak_store=peaks)["providers"][0]
    return {row["name"]: row for row in result["models"]}


def test_payload_flags_unpriced_models_and_marks_haiku_55_as_low_estimate(tmp_path):
    rows = _payload_models(tmp_path, "Codex", ["gpt-6.1-terra", "gpt-6.1-terra-preview"])
    assert rows["gpt-6.1-terra"]["unpriced"] is False
    assert rows["gpt-6.1-terra"]["apiEquivalentCost"] is not None
    assert "estimate" not in rows["gpt-6.1-terra"]
    assert rows["gpt-6.1-terra-preview"]["unpriced"] is True
    assert rows["gpt-6.1-terra-preview"]["apiEquivalentCost"] is None

    claude = _payload_models(tmp_path, "Claude", ["claude-haiku-5-5-20251001", "claude-opus-5-5"])
    assert claude["claude-haiku-5-5-20251001"]["unpriced"] is False
    assert claude["claude-haiku-5-5-20251001"]["estimate"] == "low"
    assert claude["claude-opus-5-5"]["unpriced"] is False
    assert "estimate" not in claude["claude-opus-5-5"]


def test_long_context_suffix_is_a_model_key_not_a_price_row():
    assert providers._normalise_model_id("gpt-6.1-terra" + providers.LONG_CONTEXT_SUFFIX) == "gpt-6.1-terra"
    assert providers._model_unpriced("gpt-6-astra|unlisted", providers.CODEX_PRICING)
