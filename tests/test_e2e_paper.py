"""Discover -> Validate -> Approve -> Paper Entry -> Price Update -> TP1/Stop -> Final Exit -> Persisted Trade."""
from market_data import MockProvider


def _fixture(mint="MINT-E2E", symbol="E2E", pc5=3.0, pc1h=10.0, price=1.0):
    return {
        "mint": mint, "pool_id": "POOL", "symbol": symbol, "name": symbol,
        "price_usd": price, "liquidity_usd": 50000,
        "volume_5m_usd": 40000, "volume_1h_usd": 100000,
        "price_change_5m_pct": pc5, "price_change_1h_pct": pc1h,
        "txns_5m": 100, "buys_5m": 70, "sells_5m": 30,
        "holders_top10_pct": 0.2, "deployer_pct": 0.05,
        "mint_authority_disabled": True, "freeze_authority_disabled": True,
        "honeypot_flag": False, "rugged_flag": False, "sell_route_exists": True,
        "sell_route_price_impact_pct": 0.5, "decimals": 6,
    }


def test_end_to_end_paper(intelligence, storage, market, cfg):
    market.providers[0].set_fixtures([_fixture()])
    decisions = intelligence.scan_once("2025-01-01")
    assert decisions, "expected at least one decision"
    d = decisions[0]
    assert d.decision == "APPROVED", f"expected a controlled paper entry, got {d.decision}: {d.reasons}"

    # Drive the actual paper position through TP1 + TP2 and verify persisted fills.
    opens = storage.list_positions(statuses=["OPEN"])
    assert opens, "approved decision must create a persisted open paper position"
    pos_id = opens[0]["id"]
    intelligence.pm.manage(pos_id, price=1.06)
    p = storage.get_position(pos_id)
    assert p["tp1_done"] == 1
    assert abs(p["remaining_qty"] - p["original_qty"] * 0.5) < 1e-8
    intelligence.pm.manage(pos_id, price=1.30)
    p = storage.get_position(pos_id)
    assert p["status"] == "CLOSED"
    assert p["remaining_qty"] == 0
    fills = storage.list_fills(pos_id)
    assert any(f["kind"] == "ENTRY" for f in fills)
    assert any(f["kind"] == "TP1" for f in fills)
    assert any(f["kind"] == "TP2" for f in fills)
    assert p["realized_pnl"] != 0
