from market_data import DataResult, DataStatus
from strategy_engine import StrategyEngine, Setup


def _snap():
    return __import__("tests.test_strategy", fromlist=["_snap"])._snap(
        price_change_5m_pct=DataResult(DataStatus.VALID, 5.0),
        price_change_1h_pct=DataResult(DataStatus.VALID, 20.0),
        volume_5m_usd=DataResult(DataStatus.VALID, 40000),
        volume_1h_usd=DataResult(DataStatus.VALID, 100000),
        buys_5m=DataResult(DataStatus.VALID, 80),
        sells_5m=DataResult(DataStatus.VALID, 20),
    )


def test_fast_launch_profile_has_tp1_and_runner_activation(cfg):
    engine = StrategyEngine(cfg)
    snap = _snap()
    plan = engine.plan_entry(snap, fast_launch=True)
    assert plan.setup == Setup.EARLY_MOMENTUM
    assert not plan.rejected
    plan = engine.apply_fast_launch_runner_targets(plan, snap)
    assert round(plan.tp1_price / plan.entry_price - 1, 6) == round(cfg.fast_launch_tp1_return_pct, 6)
    assert round(plan.tp2_price / plan.entry_price - 1, 6) == round(cfg.fast_launch_runner_activation_pct, 6)
    assert "TP2_ARM_RUNNER" in plan.reasons


def test_fast_launch_no_chase_ceiling_still_applies(cfg):
    engine = StrategyEngine(cfg)
    snap = __import__("tests.test_strategy", fromlist=["_snap"])._snap(
        price_change_5m_pct=DataResult(DataStatus.VALID, 25.0),
        price_change_1h_pct=DataResult(DataStatus.VALID, 35.0),
    )
    plan = engine.plan_entry(snap, fast_launch=True)
    assert plan.rejected
    assert plan.reject_reason == "ENTRY_WINDOW_MISSED"


def test_fast_launch_runner_banks_half_then_trails_remaining(cfg, storage, executor, pm):
    pos = pm.open_position("RUNNER", "RUNNER", "EARLY_MOMENTUM", "fast_launch",
                           entry_ref_price=1.0, stop_price=0.88,
                           tp1_price=1.30, tp2_price=1.75, qty=1.0,
                           entry_reason="fast launch runner", safety={},
                           meta={"fast_launch_runner": True, "runner_enabled": True,
                                 "runner_trail_drawdown_pct": 0.22, "runner_state": "WAITING_TP1"})
    assert pos
    after_tp1 = pm.manage(pos["id"], 1.30)
    assert after_tp1["status"] == "TP1_HIT"
    assert after_tp1["tp1_done"] == 1
    assert abs(after_tp1["remaining_qty"] - pos["original_qty"] * 0.5) < 1e-8
    assert after_tp1["meta"]["runner_state"] == "WAITING_TP1"

    armed = pm.manage(pos["id"], 1.75)
    assert armed["status"] == "TP1_HIT"
    assert armed["meta"]["runner_state"] == "ACTIVE"
    assert armed["trailing_active"] == 1
    assert armed["stop_price"] > armed["entry_price"]

    peaked = pm.manage(pos["id"], 2.50)
    assert peaked["status"] == "TP1_HIT"
    assert peaked["stop_price"] >= 2.50 * (1 - 0.22) - 1e-9
    closed = pm.manage(pos["id"], 1.90)
    assert closed["status"] == "CLOSED"
    assert closed["exit_reason"] == "TRAILING_EXIT"


def test_runner_arms_when_one_price_jump_crosses_both_targets(cfg, storage, executor, pm):
    pos = pm.open_position("GAP", "GAP", "EARLY_MOMENTUM", "fast_launch",
                           entry_ref_price=1.0, stop_price=0.88,
                           tp1_price=1.30, tp2_price=1.75, qty=1.0,
                           entry_reason="fast launch runner", safety={},
                           meta={"fast_launch_runner": True, "runner_enabled": True,
                                 "runner_trail_drawdown_pct": 0.22, "runner_state": "WAITING_TP1"})
    after_gap = pm.manage(pos["id"], 1.90)
    assert after_gap["status"] == "TP1_HIT"
    assert after_gap["tp1_done"] == 1
    assert after_gap["meta"]["runner_state"] == "ACTIVE"
    assert after_gap["trailing_active"] == 1
