def test_utc_day_key_does_not_require_system_zoneinfo_database(pm, cfg):
    cfg.daily_boundary_tz = "UTC"
    day = pm._day_key()
    assert len(day) == 10
    assert day[4] == "-" and day[7] == "-"
