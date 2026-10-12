from position_monitor import PositionMonitor


def test_position_monitor_starts_as_stopped_until_started(cfg, storage, market, pm):
    monitor = PositionMonitor(cfg, storage, market, pm)
    try:
        health = monitor.health()
        assert health["state"] == "STOPPED"
        assert health["alive"] is False
        assert health["mode"] == "PAPER_ONLY"
    finally:
        monitor.stop()
