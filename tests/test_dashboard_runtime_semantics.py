"""Regression tests for V6 runtime-stage labels and readiness semantics."""
from dashboard import _candidate_pipeline_bucket, _paper_readiness


class FakeProvider:
    def __init__(self, name, state, configured):
        self.name = name
        self._state = state
        self._configured = configured

    def health(self):
        return {"state": self._state, "configured": self._configured}


class FakeMarket:
    def __init__(self, providers):
        self.providers = providers


def test_missing_critical_safety_is_not_mislabelled_as_market_enrichment():
    candidate = {
        "mint": "MINT-A",
        "payload": {
            "decision": "NEEDS_REFRESH",
            "market": {"liquidity_usd": 15000},
            "safety": {"passed": False, "missing_fields": ["sell_route_exists", "freeze_authority_disabled"]},
            "reasons": ["MISSING_CRITICAL_SAFETY_DATA", "MISSING_SELL_ROUTE_EVIDENCE"],
        },
    }
    assert _candidate_pipeline_bucket(candidate, set()) == "NEEDS_RISK_CHECK"


def test_missing_liquidity_is_first_unresolved_market_stage():
    candidate = {
        "mint": "MINT-B",
        "payload": {
            "decision": "NEEDS_REFRESH",
            "market": {"liquidity_usd": None},
            "safety": {"passed": False, "missing_fields": ["liquidity_usd", "freeze_authority_disabled"]},
            "reasons": ["MISSING_CRITICAL_SAFETY_DATA", "MISSING_LIQUIDITY_USD"],
        },
    }
    assert _candidate_pipeline_bucket(candidate, set()) == "NEEDS_MARKET_ENRICHMENT"


def test_final_rejections_do_not_remain_in_active_pipeline():
    candidate = {"mint": "MINT-C", "payload": {"decision": "REJECTED", "safety": {"passed": False}}}
    assert _candidate_pipeline_bucket(candidate, set()) is None


def test_open_position_is_classified_from_actual_position_set():
    candidate = {"mint": "MINT-D", "payload": {"decision": "NEEDS_REFRESH"}}
    assert _candidate_pipeline_bucket(candidate, {"MINT-D"}) == "PAPER_OPEN"


def test_missing_jupiter_key_blocks_paper_readiness_without_exposing_secret():
    market = FakeMarket([FakeProvider("jupiter_sell_route", "NOT_CONFIGURED", False)])
    result = _paper_readiness({"market": market})
    assert result["state"] == "BLOCKED"
    assert result["blockers"] == ["JUPITER_API_KEY_NOT_CONFIGURED"]
    assert result["live_execution_enabled"] is False
    assert result["blockers"] == ["JUPITER_API_KEY_NOT_CONFIGURED"]  # identifier only; no credential value


def test_configured_jupiter_is_not_mistaken_for_token_level_approval():
    market = FakeMarket([FakeProvider("jupiter_sell_route", "NOT_CHECKED", True)])
    result = _paper_readiness({"market": market})
    assert result["state"] == "CONFIGURED_NOT_TOKEN_VERIFIED"
    assert "each candidate" in result["note"].lower()
    assert result["live_execution_enabled"] is False
