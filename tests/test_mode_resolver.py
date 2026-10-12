from __future__ import annotations

import pytest

from config import Config
from mode_resolver import ModeResolver, OperatingMode
from paper_execution import PaperExecutor
from position_manager import PositionManager
from storage import Storage


def test_paper_is_the_only_entry_enabled_mode(cfg, storage):
    resolver = ModeResolver(cfg, storage)
    assert resolver.current() == OperatingMode.PAPER
    assert resolver.can_open_position() == (True, "PAPER_MODE_ALLOWED")
    assert resolver.snapshot()["live_authorized"] is False


def test_live_request_is_observable_but_never_authorized(tmp_db):
    cfg = Config(db_path=tmp_db, mode="LIVE", live_trading_enabled=True).validate()
    storage = Storage(tmp_db)
    try:
        resolver = ModeResolver(cfg, storage)
        assert resolver.current() == OperatingMode.LIVE_BLOCKED
        assert resolver.can_open_position()[0] is False
        assert resolver.snapshot()["live_authorized"] is False
    finally:
        storage.close()


def test_watch_only_blocks_new_simulated_entries(tmp_db):
    cfg = Config(db_path=tmp_db, mode="WATCH_ONLY").validate()
    storage = Storage(tmp_db)
    resolver = ModeResolver(cfg, storage)
    pm = PositionManager(cfg, storage, PaperExecutor(cfg), mode_resolver=resolver)
    try:
        assert resolver.current() == OperatingMode.WATCH_ONLY
        opened = pm.open_position("mint", "TST", "TEST", "technical", 1.0,
                                  0.9, 1.1, 1.2, 1, "test", {}, {})
        assert opened is None
        assert storage.recent_logs(component="position_manager", limit=5)[0]["event"] == "ENTRY_MODE_BLOCKED"
    finally:
        storage.close()


def test_safe_stop_persists_across_database_reopen(tmp_db):
    cfg = Config(db_path=tmp_db).validate()
    storage = Storage(tmp_db)
    resolver = ModeResolver(cfg, storage)
    resolver.report_runtime_failure("ACCOUNTING_DIVERGENCE")
    assert resolver.current() == OperatingMode.SAFE_STOP
    storage.close()
    reopened = Storage(tmp_db)
    try:
        restarted = ModeResolver(cfg, reopened)
        assert restarted.current() == OperatingMode.SAFE_STOP
        assert restarted.can_open_position()[0] is False
    finally:
        reopened.close()


def test_safe_stop_reset_requires_confirmation_and_reconciliation(cfg, storage):
    resolver = ModeResolver(cfg, storage)
    resolver.engage_kill_switch()
    with pytest.raises(PermissionError):
        resolver.reset_safe_stop(operator_confirmed=False, reconciliation_passed=True)
    assert resolver.current() == OperatingMode.SAFE_STOP
    assert resolver.reset_safe_stop(operator_confirmed=True, reconciliation_passed=True) is True
    assert resolver.current() == OperatingMode.PAPER


def test_environment_kill_switch_latches_safe_stop(tmp_db):
    cfg = Config(db_path=tmp_db, kill_switch=True).validate()
    storage = Storage(tmp_db)
    try:
        resolver = ModeResolver(cfg, storage)
        assert resolver.current() == OperatingMode.SAFE_STOP
        assert resolver.can_open_position()[0] is False
    finally:
        storage.close()


def test_unverified_simulated_close_latches_safe_stop(cfg, storage, executor, monkeypatch):
    resolver = ModeResolver(cfg, storage)
    pm = PositionManager(cfg, storage, executor, mode_resolver=resolver)
    pos = pm.open_position("mint-close-test", "TST", "TEST", "technical", 100.0,
                           90.0, 110.0, 120.0, 1.0, "test", {}, {})
    assert pos is not None
    actual_get = storage.get_position
    calls = {"n": 0}

    def stale_on_close_verification(position_id):
        calls["n"] += 1
        # manage() now reads once for execution-mode reconciliation; the fourth read is post-close verification.
        if calls["n"] == 4:
            return pos
        return actual_get(position_id)

    monkeypatch.setattr(storage, "get_position", stale_on_close_verification)
    pm.manage(pos["id"], 89.0)
    assert resolver.current() == OperatingMode.SAFE_STOP
    assert storage.kv_get(resolver.SAFE_STOP_KEY) == "UNVERIFIED_CLOSE"


def test_live_is_authorized_only_when_runtime_adapter_is_ready(tmp_db):
    cfg = Config(db_path=tmp_db, mode="LIVE", live_trading_enabled=True,
                 solana_live_key_configured=True, live_execution_ready=True).validate()
    storage = Storage(tmp_db)
    try:
        resolver = ModeResolver(cfg, storage)
        assert resolver.current() == OperatingMode.LIVE
        assert resolver.snapshot()["live_authorized"] is True
        assert resolver.can_open_position() == (True, "LIVE_MODE_ALLOWED")
    finally:
        storage.close()
