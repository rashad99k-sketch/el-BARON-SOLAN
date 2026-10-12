def test_tp1_closes_half(cfg, storage, executor, pm):
    pos = pm.open_position("M", "S", "EARLY_MOMENTUM", "technical",
                           entry_ref_price=1.0, stop_price=0.9,
                           tp1_price=1.05, tp2_price=1.2,
                           qty=1.0, entry_reason="x", safety={})
    assert pos is not None
    assert pos["status"] == "OPEN"
    # trigger TP1
    pm.manage(pos["id"], price=1.06)
    p = storage.get_position(pos["id"])
    assert p["tp1_done"] == 1
    assert p["status"] == "TP1_HIT"
    assert abs(p["remaining_qty"] - 0.5) < 1e-9


def test_tp2_closes_remaining(cfg, storage, executor, pm):
    pos = pm.open_position("M", "S", "EARLY_MOMENTUM", "technical",
                           entry_ref_price=1.0, stop_price=0.9,
                           tp1_price=1.05, tp2_price=1.2,
                           qty=1.0, entry_reason="x", safety={})
    pm.manage(pos["id"], price=1.06)     # TP1
    pm.manage(pos["id"], price=1.30)     # TP2
    p = storage.get_position(pos["id"])
    assert p["status"] == "CLOSED"
    assert p["exit_reason"] == "TP2"
    assert p["remaining_qty"] == 0.0


def test_duplicate_exit_prevented(cfg, storage, executor, pm):
    pos = pm.open_position("M", "S", "EARLY_MOMENTUM", "technical",
                           entry_ref_price=1.0, stop_price=0.9,
                           tp1_price=1.05, tp2_price=1.2,
                           qty=1.0, entry_reason="x", safety={})
    pm.manage(pos["id"], price=1.30)
    pm.manage(pos["id"], price=1.30)
    fills = storage.list_fills(pos["id"])
    exit_fills = [f for f in fills if f["kind"] != "ENTRY"]
    # Either TP1+TP2, or single TP2; never a third exit
    assert len(exit_fills) <= 2


def test_stop_loss_closes(cfg, storage, executor, pm):
    pos = pm.open_position("M", "S", "EARLY_MOMENTUM", "technical",
                           entry_ref_price=1.0, stop_price=0.9,
                           tp1_price=1.05, tp2_price=1.2,
                           qty=1.0, entry_reason="x", safety={})
    pm.manage(pos["id"], price=0.85)
    p = storage.get_position(pos["id"])
    assert p["status"] == "CLOSED"
    assert p["exit_reason"] in ("STOP_LOSS", "TRAILING_EXIT")


def test_missing_price_is_not_exit(cfg, storage, executor, pm):
    pos = pm.open_position("M", "S", "EARLY_MOMENTUM", "technical",
                           entry_ref_price=1.0, stop_price=0.9,
                           tp1_price=1.05, tp2_price=1.2,
                           qty=1.0, entry_reason="x", safety={})
    pm.manage(pos["id"], price=None)
    p = storage.get_position(pos["id"])
    assert p["status"] == "OPEN"


def test_duplicate_mint_open_is_blocked_atomically(cfg, storage, pm):
    first = pm.open_position("DUP-MINT", "DUP", "EARLY_MOMENTUM", "technical",
                             entry_ref_price=1.0, stop_price=0.9,
                             tp1_price=1.05, tp2_price=1.2,
                             qty=0.1, entry_reason="first", safety={})
    second = pm.open_position("DUP-MINT", "DUP", "EARLY_CONTINUATION", "fast_launch",
                              entry_ref_price=1.0, stop_price=0.9,
                              tp1_price=1.05, tp2_price=1.2,
                              qty=0.1, entry_reason="duplicate event", safety={})
    assert first is not None
    assert second is None
    assert len(storage.list_positions(statuses=["OPEN", "TP1_HIT"])) == 1
    assert len([f for f in storage.list_fills() if f["kind"] == "ENTRY"]) == 1


def test_manager_hard_cap_cannot_be_bypassed_by_direct_open_calls(cfg, storage, pm):
    cfg.max_total_open_positions = 2
    one = pm.open_position("CAP-1", "C1", "EARLY_MOMENTUM", "technical", 1.0, 0.9,
                           1.1, 1.2, 0.1, "test", {})
    two = pm.open_position("CAP-2", "C2", "EARLY_MOMENTUM", "technical", 1.0, 0.9,
                           1.1, 1.2, 0.1, "test", {})
    three = pm.open_position("CAP-3", "C3", "EARLY_MOMENTUM", "technical", 1.0, 0.9,
                             1.1, 1.2, 0.1, "test", {})
    assert one is not None and two is not None
    assert three is None
    assert len(storage.list_positions(statuses=["OPEN", "TP1_HIT"])) == 2


def test_realized_losses_update_daily_lock(cfg, storage, pm, risk):
    # A synthetic stop-loss move larger than the configured daily lock must persist the lock.
    cfg.daily_realized_loss_lock = -0.01
    pos = pm.open_position("LOSS", "LOSS", "EARLY_MOMENTUM", "technical",
                           entry_ref_price=1.0, stop_price=0.9,
                           tp1_price=1.05, tp2_price=1.2,
                           qty=1.0, entry_reason="test", safety={})
    pm.manage(pos["id"], price=0.80)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    day = datetime.now(ZoneInfo(cfg.daily_boundary_tz)).date().isoformat()
    daily = storage.get_or_init_daily(day, cfg.initial_equity)
    assert daily["locked"] == 1
    assert daily["realized_pnl"] <= cfg.daily_realized_loss_lock
    decision = risk.evaluate(day, entry_price=1.0, stop_price=0.9)
    assert decision.approved is False
    assert decision.reason == "DAILY_LOSS_LOCK"
