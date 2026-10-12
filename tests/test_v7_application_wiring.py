"""Integration regressions for the V7 event ingress and dashboard routes."""
from __future__ import annotations

import time
import pytest

from solana_app import Application


@pytest.fixture(autouse=True)
def isolate_application_wiring_tests_from_runtime_wallet(monkeypatch, tmp_path):
    """Keep wiring tests deterministic even when the developer has LIVE keys set.

    These tests verify the default PAPER wiring and read-only wallet registration;
    they must not borrow a developer's local signer or persistent wallet-watch file.
    Individual tests can still set GROK6_PHANTOM_PUBLIC_ADDRESS explicitly.
    """
    monkeypatch.delenv("GROK6_SOLANA_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("GROK6_MODE", "PAPER")
    monkeypatch.setenv("GROK6_LIVE_TRADING", "false")
    monkeypatch.delenv("GROK6_PHANTOM_PUBLIC_ADDRESS", raising=False)
    monkeypatch.setenv("GROK6_WALLET_WATCH_PATH", str(tmp_path / "isolated-wallet-watch.json"))


def test_launch_event_is_persisted_and_prioritized_before_provider_work(cfg):
    app = Application(cfg, provider_mode="mock")
    mint = "So11111111111111111111111111111111111111112"
    event = {
        "mint": mint,
        "symbol": "TEST",
        "name": "Test Candidate",
        "source": "pumpportal_new_token",
        "event_type": "NEW_TOKEN",
        "launch_ts": time.time(),
        "received_ts": time.time(),
        "raw_event_fields": ["mint", "symbol"],
    }
    try:
        assert app.launch_stream.ingest(event) is True
        stored = app.storage.get_candidate(mint)
        assert stored is not None
        assert stored["payload"]["discovery"].get("source_event_id")
        assert app.bus.health()["queue_depth"] == 1
        assert app.bus.health()["enqueued"] == 1
        assert app.cfg.live_trading_enabled is False
    finally:
        app.bus.stop(timeout=0.1, drain=False)
        app.market.close()
        app.storage.close()


def test_dashboard_v7_routes_are_wired_and_live_execution_stays_disabled(cfg):
    pytest.importorskip("flask")
    app = Application(cfg, provider_mode="mock")
    from dashboard import build_dashboard

    state = {
        "cfg": app.cfg, "storage": app.storage, "market": app.market,
        "intelligence": app.intelligence, "observed_candles": app.intelligence.candles,
        "discovery": app.discovery, "wallet_watch": app.wallet_watch,
        "wallet_poller": app.wallet_poller, "launch_stream": app.launch_stream,
        "risk": app.risk, "pm": app.pm, "position_monitor": app.position_monitor,
        "today": app.today, "bus": app.bus, "sniper": app.sniper,
        "continuation": app.continuation, "retries": app.retries,
        "reasons": app.reasons, "started_at": app._started_at,
    }
    try:
        client = build_dashboard(state).test_client()
        for endpoint in ("/health", "/api/system/status", "/api/fast-path/status",
                         "/api/continuation", "/api/positions", "/api/operations",
                         "/api/social-radar", "/api/social-radar/health"):
            response = client.get(endpoint)
            assert response.status_code == 200, (endpoint, response.get_data(as_text=True))
        system = client.get("/api/system/status").get_json()
        fast = client.get("/api/fast-path/status").get_json()
        assert system["live_trading_enabled"] is False
        assert fast["live_execution_enabled"] is False
        assert client.get("/api/social-radar/health").get_json()["live_execution_enabled"] is False
        assert client.get("/api/operations").get_json()["live_execution_enabled"] is False
        settings = client.get("/api/settings").get_json()
        assert settings["social_x_bearer_token"] in ("", "CONFIGURED")
        assert settings["social_telegram_bot_token"] in ("", "CONFIGURED")
        assert system["position_monitor"]["mode"] == "PAPER_ONLY"
    finally:
        app.market.close()
        app.storage.close()


def test_env_phantom_public_address_is_auto_watched_read_only(cfg, monkeypatch, tmp_path):
    address = "So11111111111111111111111111111111111111112"
    monkeypatch.setenv("GROK6_PHANTOM_PUBLIC_ADDRESS", address)
    monkeypatch.setenv("GROK6_WALLET_WATCH_PATH", str(tmp_path / "watched-wallets.json"))
    app = Application(cfg, provider_mode="mock")
    try:
        wallets = app.wallet_watch.list()
        assert any(w["address"] == address and w["label"] == "Phantom wallet (read-only)" for w in wallets)
        assert app.mode_resolver.snapshot()["live_authorized"] is False
        assert app.cfg.live_trading_enabled is False
    finally:
        app.market.close()
        app.storage.close()


def test_invalid_env_phantom_address_does_not_crash_application(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("GROK6_PHANTOM_PUBLIC_ADDRESS", "not-a-solana-address")
    monkeypatch.setenv("GROK6_WALLET_WATCH_PATH", str(tmp_path / "watched-wallets.json"))
    app = Application(cfg, provider_mode="mock")
    try:
        assert app.wallet_watch.list() == []
        assert app.mode_resolver.snapshot()["live_authorized"] is False
    finally:
        app.market.close()
        app.storage.close()
