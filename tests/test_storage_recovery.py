def test_positions_persist(cfg, storage, executor, pm, tmp_db):
    pm.open_position("M", "S", "EARLY_MOMENTUM", "technical",
                     entry_ref_price=1.0, stop_price=0.9,
                     tp1_price=1.05, tp2_price=1.2,
                     qty=1.0, entry_reason="x", safety={})
    storage.close()
    from storage import Storage
    s2 = Storage(tmp_db)
    opens = s2.list_positions(statuses=["OPEN"])
    assert len(opens) == 1
    s2.close()


def test_daily_lock_survives_restart(cfg, storage, tmp_db):
    storage.get_or_init_daily("2025-01-01", 20.0)
    storage.update_daily("2025-01-01", realized_pnl=-2.0, locked=True)
    storage.close()
    from storage import Storage
    s2 = Storage(tmp_db)
    ds = s2.get_or_init_daily("2025-01-01", 20.0)
    assert ds["locked"] == 1
    s2.close()
