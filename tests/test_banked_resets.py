from token_tracker.providers import _banked_resets

NOW = 1_800_000_000


def credit(**fields):
    return {"id": "x", "resetType": "codexRateLimits", "status": "available",
            "grantedAt": NOW - 100, "expiresAt": NOW + 86400, "title": "Full reset (Weekly + 5 hr)", **fields}


def test_counts_only_available_unexpired_resets_soonest_first():
    result = {"rateLimitResetCredits": {"availableCount": 4, "credits": [
        credit(expiresAt=NOW + 9000, title="Later"),
        credit(expiresAt=NOW + 100, title="Sooner"),
        credit(status="redeemed"),
        credit(expiresAt=NOW - 1),
    ]}}
    banked = _banked_resets(result, now=NOW)
    assert banked == {"available": 2, "items": [
        {"title": "Sooner", "expiresAt": NOW + 100}, {"title": "Later", "expiresAt": NOW + 9000}]}


def test_missing_or_malformed_block_reports_nothing():
    assert _banked_resets({}, now=NOW) is None
    assert _banked_resets(None, now=NOW) is None
    assert _banked_resets({"rateLimitResetCredits": {"credits": "bad"}}, now=NOW) is None
    assert _banked_resets({"rateLimitResetCredits": {"credits": [None, credit(expiresAt="soon")]}}, now=NOW) == {
        "available": 1, "items": [{"title": "Full reset (Weekly + 5 hr)", "expiresAt": None}]}


def test_empty_list_is_zero_not_unknown():
    assert _banked_resets({"rateLimitResetCredits": {"availableCount": 0, "credits": []}}, now=NOW) == {
        "available": 0, "items": []}
