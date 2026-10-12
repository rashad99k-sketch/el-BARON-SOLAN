from market_data import TokenSnapshot, DataResult, DataStatus
from token_safety import SafetyEvaluator


def _snap(**over):
    base = dict(
        mint="MINT", pool_id="POOL", symbol="TST", name="Test",
        price_usd=DataResult(DataStatus.VALID, 0.001),
        liquidity_usd=DataResult(DataStatus.VALID, 20000),
        volume_5m_usd=DataResult(DataStatus.VALID, 5000),
        volume_1h_usd=DataResult(DataStatus.VALID, 20000),
        price_change_5m_pct=DataResult(DataStatus.VALID, 5.0),
        price_change_1h_pct=DataResult(DataStatus.VALID, 20.0),
        txns_5m=DataResult(DataStatus.VALID, 50),
        buys_5m=DataResult(DataStatus.VALID, 30),
        sells_5m=DataResult(DataStatus.VALID, 20),
        unique_buyers_5m=DataResult(DataStatus.MISSING),
        unique_sellers_5m=DataResult(DataStatus.MISSING),
        ohlcv=DataResult(DataStatus.UNSUPPORTED),
        holders_top10_pct=DataResult(DataStatus.VALID, 0.20),
        deployer_pct=DataResult(DataStatus.VALID, 0.05),
        mint_authority_disabled=DataResult(DataStatus.VALID, True),
        freeze_authority_disabled=DataResult(DataStatus.VALID, True),
        honeypot_flag=DataResult(DataStatus.VALID, False),
    )
    base.update(over)
    return TokenSnapshot(**base)


def test_confirmed_danger_rejects(cfg):
    s = _snap(honeypot_flag=DataResult(DataStatus.VALID, True))
    r = SafetyEvaluator(cfg).evaluate(s)
    assert r.hard_reject is True
    assert "HONEYPOT_FLAGGED" in r.reason_codes


def test_unknown_is_not_safe(cfg):
    s = _snap(mint_authority_disabled=DataResult(DataStatus.MISSING))
    r = SafetyEvaluator(cfg).evaluate(s)
    assert "MISSING_MINT_AUTHORITY_DISABLED" in r.reason_codes
    assert r.passed is False


def test_missing_advisory_concentration_data_does_not_veto_passed_critical_gates(cfg):
    from market_data import DataResult, DataStatus

    snap = _snap(
        holders_top10_pct=DataResult(DataStatus.MISSING, reason="not available yet"),
        deployer_pct=DataResult(DataStatus.MISSING, reason="not available yet"),
        rugged_flag=DataResult(DataStatus.MISSING, reason="optional classification unavailable"),
        honeypot_flag=DataResult(DataStatus.MISSING, reason="no dedicated provider configured"),
        sell_route_exists=DataResult(DataStatus.VALID, True),
    )
    result = SafetyEvaluator(cfg).evaluate(snap)
    assert result.hard_reject is False
    assert result.passed is True
    assert "holders_top10_pct" in result.missing_fields
    assert "deployer_pct" in result.missing_fields
    assert result.score >= 0.5
