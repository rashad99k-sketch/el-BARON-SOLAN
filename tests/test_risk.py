def test_sizing_and_exposure(risk):
    d = risk.evaluate("2025-01-01", entry_price=1.0, stop_price=0.9)
    assert d.approved
    # risk per trade 0.5% of $20 = $0.10; stop distance 0.10 => qty=1.0
    assert abs(d.qty - 1.0) < 1e-6
    # notional cap $2 => qty<=2
    assert d.notional <= 2.0 + 1e-9


def test_position_cap_enforced(cfg, storage, executor, pm, risk):
    # open 6 tiny positions to hit the cap
    for i in range(6):
        pm.open_position(f"M{i}", f"S{i}", "EARLY_MOMENTUM", "technical",
                         entry_ref_price=1.0, stop_price=0.5, tp1_price=1.1, tp2_price=1.2,
                         qty=0.1, entry_reason="test", safety={})
    d = risk.evaluate("2025-01-01", entry_price=1.0, stop_price=0.5)
    assert not d.approved
    assert d.reason in ("MAX_OPEN_POSITIONS", "MAX_EXPOSURE", "AGGREGATE_RISK_EXCEEDED")


def test_daily_lock_blocks(cfg, storage, risk):
    ds = storage.get_or_init_daily("2025-01-01", cfg.initial_equity)
    storage.update_daily("2025-01-01", realized_pnl=-2.0, locked=True)
    d = risk.evaluate("2025-01-01", entry_price=1.0, stop_price=0.9)
    assert not d.approved
    assert d.reason == "DAILY_LOSS_LOCK"


def test_portfolio_snapshot_tracks_mark_to_market_and_entry_fee(cfg, storage, pm, risk):
    pos = pm.open_position("PNL-MINT", "PNL", "EARLY_MOMENTUM", "technical",
                           entry_ref_price=1.0, stop_price=0.9,
                           tp1_price=1.2, tp2_price=1.4,
                           qty=1.0, entry_reason="test", safety={})
    assert pos is not None
    entry_fill = next(f for f in storage.list_fills(pos["id"]) if f["kind"] == "ENTRY")

    # At the executed fill price, gross mark PnL is zero; paid entry fee remains a cost.
    storage.update_position(pos["id"], last_mark_price=pos["entry_price"], last_mark_ts=__import__("time").time())
    flat = risk.portfolio_snapshot()
    assert abs(flat["gross_unrealized_pnl"]) < 1e-9
    assert abs(flat["unrealized_pnl"] + entry_fill["fee"]) < 1e-9
    assert abs(flat["equity"] - (cfg.initial_equity - entry_fill["fee"])) < 1e-8

    storage.update_position(pos["id"], last_mark_price=pos["entry_price"] * 1.10,
                            last_mark_ts=__import__("time").time())
    moved = risk.portfolio_snapshot()
    expected_gross = (pos["entry_price"] * 1.10 - pos["entry_price"]) * pos["remaining_qty"]
    assert abs(moved["gross_unrealized_pnl"] - expected_gross) < 1e-8
    assert moved["unrealized_pnl"] > flat["unrealized_pnl"]
    assert abs(moved["equity"] - (cfg.initial_equity + moved["realized_pnl"] + moved["unrealized_pnl"])) < 1e-8


def test_open_position_with_unavailable_mark_makes_equity_unavailable(cfg, storage, pm, risk):
    pos = pm.open_position("NO-MARK", "NM", "EARLY_MOMENTUM", "technical", 1.0, 0.9,
                           1.1, 1.2, 0.1, "test", {})
    assert pos is not None
    storage.update_position(pos["id"], last_mark_price=None, last_mark_ts=None)
    snap = risk.portfolio_snapshot()
    assert snap["valuation_complete"] is False
    assert snap["equity"] is None
    assert snap["unrealized_pnl"] is None
    decision = risk.evaluate("2025-01-01", entry_price=1.0, stop_price=0.9)
    assert decision.approved is False
    assert decision.reason == "POSITION_VALUATION_UNAVAILABLE"
