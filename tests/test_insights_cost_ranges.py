from datetime import datetime, timezone

from token_tracker import insights, providers


def _empty_peaks(tmp_path):
    return insights.LimitPeakStore(tmp_path / "peaks.jsonl", owner_check=lambda: False)


def _usage(tokens):
    return {"input_tokens": tokens, "cached_tokens": 0, "output_tokens": 0,
            "cache_5m_tokens": 0, "cache_1h_tokens": 0, "total_tokens": tokens}


def test_24h_range_does_not_fall_back_to_day_totals_and_marks_hour_availability(tmp_path):
    now = datetime(2026, 10, 27, 13, 35, tzinfo=timezone.utc)
    yesterday = "2026-10-26"
    snapshot = {"providers": [{
        "name": "Codex", "activity": [{"date": yesterday, "value": 100}],
        "_insightsData": {
            "dailyModels": {yesterday: {"gpt-5.6-sol": _usage(100)}},
            # Present hourly data, but all observed usage is outside the selected window.
            "hourlyModels": {"2026-10-26T08:00:00+00:00": {"gpt-5.6-sol": _usage(100)}},
        },
    }]}

    present = insights.build_insights(snapshot, "24h", now=now, peak_store=_empty_peaks(tmp_path))["providers"][0]
    assert present["totals"]["tokens"] == 0
    assert present["totals"]["apiEquivalentCost"] == 0.0
    assert all(row["tokens"] == 0 for row in present["series"])

    snapshot["providers"][0]["_insightsData"]["hourlyModels"] = {}
    empty = insights.build_insights(snapshot, "24h", now=now, peak_store=_empty_peaks(tmp_path))["providers"][0]
    assert empty["totals"]["tokens"] == 0
    assert all(row["tokens"] == 0 for row in empty["series"])

    snapshot["providers"][0]["_insightsData"].pop("hourlyModels")
    absent = insights.build_insights(snapshot, "24h", now=now, peak_store=_empty_peaks(tmp_path))["providers"][0]
    assert absent["totals"]["tokens"] is None
    assert absent["totals"]["apiEquivalentCost"] is None
    assert absent["previous"] == {"tokens": None, "apiEquivalentCost": None}
    assert all(row["tokens"] is None and row["apiEquivalentCost"] is None for row in absent["series"])


def test_prices_only_registered_ids_and_existing_model_aliases():
    known = {
        "Claude": {
            "claude-fable-5": "fable-5",
            "claude-opus-5": "opus-5",
            "claude-opus-4-8": "opus-4.8",
            "claude-sonnet-5": "sonnet-5",
            "claude-haiku-4-5": "haiku-4.5",
            "claude-haiku-4-5-20251001": "haiku-4.5",
            "claude-fable-5-1": "fable-5.1",
            "claude-opus-5-5": "opus-5.5",
            "claude-sonnet-5-5": "sonnet-5.5",
            "claude-haiku-5-5": "haiku-5.5",
        },
        "Codex": {model: model for model in providers.CODEX_PRICING},
    }
    tables = {"Claude": providers.CLAUDE_PRICING, "Codex": providers.CODEX_PRICING}
    for provider, models in known.items():
        for model, price_key in models.items():
            assert providers._price_for(model, tables[provider]) == tables[provider][price_key]
    assert providers._price_for("GPT-5.6-SOL", providers.CODEX_PRICING) == providers.CODEX_PRICING["gpt-5.6-sol"]
    # Opus 5.5 is its own price row, not Opus 5's (the old substring matcher conflated them).
    assert providers._price_for("claude-opus-5-5", providers.CLAUDE_PRICING) == (4, .2, 5, 8, 20)

    for unknown in ("gpt-5.6-sol-unlisted-premium", "gpt-6.astra", "gpt6astra",
                    "gpt-6-astra|unlisted"):
        assert providers._price_for(unknown, providers.CODEX_PRICING) is None
        cost = providers._costs({"total_tokens": 1_000_000}, {unknown: _usage(1_000_000)}, providers.CODEX_PRICING)
        assert cost["pricingCoverage"] == 0
        assert cost["unpricedModels"] == [{"model": unknown, "tokens": 1_000_000}]
        assert cost["apiEquivalentCost"] is None
        assert cost["estimated"] is True


def test_insights_unknown_derived_model_is_unpriced(tmp_path):
    now = datetime(2026, 10, 27, 13, 35, tzinfo=timezone.utc)
    unknown = "gpt-5.6-sol-unlisted-premium"
    snapshot = {"providers": [{
        "name": "Codex", "_insightsData": {
            "dailyModels": {"2026-10-27": {unknown: _usage(1_000_000)}},
            "hourlyModels": {},
        },
    }]}
    provider = insights.build_insights(snapshot, "30d", now=now, peak_store=_empty_peaks(tmp_path))["providers"][0]
    assert provider["totals"]["apiEquivalentCost"] is None
    assert provider["totals"]["costPartial"] is True
    assert provider["totals"]["unpricedModels"] == [unknown]


def test_codex_long_context_rows_keep_their_registered_price(tmp_path):
    now = datetime(2026, 10, 27, 13, 35, tzinfo=timezone.utc)
    long_context = "gpt-6-astra" + providers.LONG_CONTEXT_SUFFIX
    snapshot = {"providers": [{
        "name": "Codex", "_insightsData": {
            "dailyModels": {"2026-10-27": {long_context: _usage(300_000)}},
            "hourlyModels": {},
        },
    }]}
    assert providers._price_for(long_context, providers.CODEX_PRICING) == providers.CODEX_PRICING["gpt-6-astra"]
    provider = insights.build_insights(snapshot, "30d", now=now, peak_store=_empty_peaks(tmp_path))["providers"][0]
    assert provider["totals"]["apiEquivalentCost"] is not None
    assert provider["totals"].get("costPartial") is None
    assert provider["totals"].get("unpricedModels") is None
