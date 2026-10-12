from __future__ import annotations

import pytest

from compounding import SmartCompoundingEngine


def test_equity_only_changes_after_verified_operation_pnl(storage):
    engine = SmartCompoundingEngine(storage, starting_capital=20)
    assert float(engine.verified_trading_equity()) == pytest.approx(20)
    op = storage.create_financial_operation(chain="solana", token_address="mint", side="BUY",
                                            size_native=1, size_usd=10)
    # A deposit or submitted/pending operation is not represented as trading profit.
    assert float(engine.verified_trading_equity()) == pytest.approx(20)
    storage.transition_financial_operation(op["operation_id"], "CONFIRMED", tx_hash="confirmed-hash",
                                           fees_usd=0.2, slippage_usd=0.1, gas_usd=0.1,
                                           verification_evidence={"provider": "test-adapter", "status": "FINALIZED",
                                                                  "tx_hash": "confirmed-hash", "commitment": "finalized"})
    storage.apply_verified_operation_pnl(op["operation_id"], gross_pnl_usd=2,
                                         fees_usd=0.2, slippage_usd=0.1, gas_usd=0.1)
    assert float(engine.verified_trading_equity()) == pytest.approx(21.6)
    assert float(engine.risk_budget(0.005)) == pytest.approx(0.108)


def test_position_size_uses_lowest_risk_liquidity_and_exposure_cap(storage):
    engine = SmartCompoundingEngine(storage, starting_capital=20)
    result = engine.size_position(
        risk_pct=0.005, stop_distance_fraction=0.10,
        liquidity_usd=100_000, adapter_max_size_usd=10,
        max_position_usd=5, total_exposure_usd=1,
        max_total_exposure_usd=6, min_economic_size_usd=0.5,
    )
    assert result["ok"] is True
    assert result["size_usd"] == pytest.approx(1.0)
    assert result["candidates_usd"]["from_risk"] == "1.00"
    assert result["candidates_usd"]["from_exposure_headroom"] == "5.00"


def test_invalid_stop_and_insufficient_minimum_reject(storage):
    engine = SmartCompoundingEngine(storage, starting_capital=20)
    args = dict(risk_pct=0.005, stop_distance_fraction=0.0, liquidity_usd=1000,
                adapter_max_size_usd=10, max_position_usd=5, total_exposure_usd=6,
                max_total_exposure_usd=6, min_economic_size_usd=0.5)
    assert engine.size_position(**args)["reason"] == "INVALID_STOP_DISTANCE_FRACTION"
    args["stop_distance_fraction"] = 0.1
    assert engine.size_position(**args)["reason"] == "INSUFFICIENT_ECONOMIC_SIZE"


def test_risk_pct_cannot_become_martingale(storage):
    engine = SmartCompoundingEngine(storage, starting_capital=20)
    with pytest.raises(ValueError, match="risk_pct"):
        engine.risk_budget(0.1)
