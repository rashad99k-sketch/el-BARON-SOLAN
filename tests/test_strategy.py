from market_data import TokenSnapshot, DataResult, DataStatus
from strategy_engine import StrategyEngine, Setup
from config import Config


def _snap(**over):
    base = dict(
        mint="M", pool_id="P", symbol="S", name="N",
        price_usd=DataResult(DataStatus.VALID, 1.0),
        liquidity_usd=DataResult(DataStatus.VALID, 50000),
        volume_5m_usd=DataResult(DataStatus.VALID, 40000),
        volume_1h_usd=DataResult(DataStatus.VALID, 100000),
        price_change_5m_pct=DataResult(DataStatus.VALID, 3.0),
        price_change_1h_pct=DataResult(DataStatus.VALID, 10.0),
        txns_5m=DataResult(DataStatus.VALID, 100),
        buys_5m=DataResult(DataStatus.VALID, 70),
        sells_5m=DataResult(DataStatus.VALID, 30),
        unique_buyers_5m=DataResult(DataStatus.MISSING),
        unique_sellers_5m=DataResult(DataStatus.MISSING),
        ohlcv=DataResult(DataStatus.UNSUPPORTED),
        holders_top10_pct=DataResult(DataStatus.VALID, 0.2),
        deployer_pct=DataResult(DataStatus.VALID, 0.05),
        mint_authority_disabled=DataResult(DataStatus.VALID, True),
        freeze_authority_disabled=DataResult(DataStatus.VALID, True),
        honeypot_flag=DataResult(DataStatus.VALID, False),
    )
    base.update(over)
    return TokenSnapshot(**base)


def test_early_momentum_setup(cfg):
    e = StrategyEngine(cfg)
    p = e.plan_entry(_snap())
    assert p.setup == Setup.EARLY_MOMENTUM
    assert p.net_reward_risk > 0


def test_no_chase_rejects_extension(cfg):
    e = StrategyEngine(cfg)
    # price up 40% in 1h but 5m flat -> extension beyond threshold
    p = e.plan_entry(_snap(price_change_1h_pct=DataResult(DataStatus.VALID, 40.0),
                           price_change_5m_pct=DataResult(DataStatus.VALID, 0.5)))
    assert p.rejected
    assert p.reject_reason == "ENTRY_WINDOW_MISSED"


def test_breakout_retest(cfg):
    e = StrategyEngine(cfg)
    p = e.plan_entry(_snap(price_change_1h_pct=DataResult(DataStatus.VALID, 25.0),
                           price_change_5m_pct=DataResult(DataStatus.VALID, -2.0)))
    assert p.setup == Setup.BREAKOUT_RETEST


def test_continuation(cfg):
    e = StrategyEngine(cfg)
    p = e.plan_entry(_snap(price_change_1h_pct=DataResult(DataStatus.VALID, 12.0),
                           price_change_5m_pct=DataResult(DataStatus.VALID, 1.0)))
    assert p.setup in (Setup.EARLY_CONTINUATION, Setup.EARLY_MOMENTUM)
