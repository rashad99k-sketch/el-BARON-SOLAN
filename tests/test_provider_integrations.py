"""Deterministic adapter tests. These use fixtures, not live provider connections."""
from __future__ import annotations
import time

import pytest

from market_data import DataStatus, _snapshot_from_dict
from rugcheck_provider import RugCheckProvider
from jupiter_provider import JupiterSellRouteProvider, QUOTE_URL
from helius_wallet_poller import HeliusWalletPoller
from wallet_watch import WalletWatch
from observed_candles import ObservedCandleBuilder


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        return response


def _snap(**overrides):
    data = {
        "mint": "TokenMint111111111111111111111111111111111",
        "symbol": "TEST", "name": "Test",
        "price_usd": 0.0005, "liquidity_usd": 20000,
        "volume_5m_usd": 10000, "volume_1h_usd": 100000,
        "price_change_5m_pct": 2.0, "price_change_1h_pct": 8.0,
        "decimals": 6,
    }
    data.update(overrides)
    return _snapshot_from_dict(data, "fixture")


def test_rugcheck_rugged_is_not_inferred_as_honeypot():
    session = FakeSession([FakeResponse(payload={
        "rugged": False,
        "mintAuthority": None,
        "freezeAuthority": None,
        "topHolders": [{"pct": 10}, {"pct": 5}],
        "risks": [],
    })])
    provider = RugCheckProvider(api_key="", max_retries=1, session=session)
    snap = _snap()
    provider.enrich_snapshot(snap.mint, snap)

    assert snap.rugged_flag.status == DataStatus.VALID
    assert snap.rugged_flag.value is False
    assert snap.honeypot_flag.status == DataStatus.MISSING
    assert snap.mint_authority_disabled.status == DataStatus.VALID
    assert snap.mint_authority_disabled.value is True
    assert snap.freeze_authority_disabled.value is True
    assert snap.holders_top10_pct.value == pytest.approx(0.15)


def test_jupiter_uses_swap_v2_and_token_smallest_units_without_signing():
    session = FakeSession([FakeResponse(payload={
        "outAmount": "123456", "router": "metis", "transaction": None,
    })])
    provider = JupiterSellRouteProvider(
        api_key="test-key", paper_size_usd=2.0,
        min_request_interval_seconds=0, session=session,
    )
    snap = _snap()
    provider.enrich_snapshot(snap.mint, snap)

    assert snap.sell_route_exists.status == DataStatus.VALID
    assert snap.sell_route_exists.value is True
    assert snap.sell_route_price_impact_pct.status == DataStatus.MISSING
    assert session.calls[0][0] == QUOTE_URL == "https://api.jup.ag/swap/v2/order"
    call = session.calls[0][1]
    assert call["params"]["amount"] == "4000000000"
    assert call["params"]["inputMint"] == snap.mint
    assert call["headers"]["x-api-key"] == "test-key"
    assert "taker" not in call["params"]


def test_jupiter_missing_api_key_is_missing_evidence_not_a_pass():
    provider = JupiterSellRouteProvider(api_key="", min_request_interval_seconds=0,
                                        session=FakeSession([]))
    snap = _snap()
    provider.enrich_snapshot(snap.mint, snap)
    assert snap.sell_route_exists.status == DataStatus.MISSING
    assert snap.sell_route_exists.reason == "JUPITER_API_KEY_NOT_CONFIGURED"


def test_helius_wallet_poller_baselines_then_emits_only_new_swap(tmp_path):
    wallet = "11111111111111111111111111111111"
    watch = WalletWatch(str(tmp_path / "wallets.json"))
    watch.add(wallet, "fixture wallet")
    token = "TokenMint111111111111111111111111111111111"
    base_tx = {"signature": "SIG_OLD", "type": "SWAP", "timestamp": 100,
               "tokenTransfers": [{"mint": token, "toUserAccount": wallet, "fromUserAccount": "OtherWallet"}]}
    new_tx = {"signature": "SIG_NEW", "type": "SWAP", "timestamp": 200,
              "tokenTransfers": [{"mint": token, "toUserAccount": wallet, "fromUserAccount": "OtherWallet"}]}
    session = FakeSession([
        FakeResponse(payload=[base_tx]),
        FakeResponse(payload=[new_tx, base_tx]),
    ])
    poller = HeliusWalletPoller(watch, api_key="test-key", min_request_interval_seconds=0,
                                session=session)

    assert poller.poll_wallet(wallet)["status"] == "BASELINED"
    assert watch.events() == []
    result = poller.poll_wallet(wallet)
    assert result["status"] == "OK"
    assert result["events"] == 1
    events = watch.events()
    assert len(events) == 1
    assert events[0]["signature"] == "SIG_NEW"
    assert events[0]["side"] == "BUY"
    assert watch.get_cursor(wallet) == "SIG_NEW"


def test_helius_reports_cursor_gap_instead_of_claiming_complete_coverage(tmp_path):
    wallet = "11111111111111111111111111111111"
    watch = WalletWatch(str(tmp_path / "wallets.json"))
    watch.add(wallet)
    watch.set_cursor(wallet, "SIG_TOO_OLD")
    session = FakeSession([FakeResponse(payload=[
        {"signature": "SIG_NEW", "type": "TRANSFER", "timestamp": 200, "tokenTransfers": []}
    ])])
    poller = HeliusWalletPoller(watch, api_key="test-key", min_request_interval_seconds=0,
                                session=session)
    result = poller.poll_wallet(wallet)
    assert result["status"] == "GAP_DETECTED"
    assert poller.health()["cursor_gaps"] == 1
    assert poller.health()["state"] == "DEGRADED"


def test_observed_candles_never_synthesize_missing_history():
    candles = ObservedCandleBuilder(bucket_seconds=60)
    candles.add_sample("MINT", 1.0, ts=120)
    candles.add_sample("MINT", 1.1, ts=121)
    assert candles.coverage("MINT")["status"] == "INSUFFICIENT_HISTORY"
    candles.add_sample("MINT", 1.2, ts=180)
    candles.add_sample("MINT", 1.3, ts=240)
    coverage = candles.coverage("MINT")
    assert coverage["status"] == "OK"
    assert coverage["bars"] == 3
    bars = candles.get_bars("MINT")
    assert bars[0]["open"] == 1.0
    assert bars[0]["high"] == 1.1
    assert bars[0]["low"] == 1.0
    assert bars[0]["close"] == 1.1
    assert all(b["source"] == "grok6-collected" for b in bars)


def test_jupiter_http_rejection_is_unknown_not_proven_no_route():
    session = FakeSession([FakeResponse(status_code=400, payload={"error": "Invalid mint"})])
    provider = JupiterSellRouteProvider(api_key="test-key", min_request_interval_seconds=0,
                                        session=session)
    snap = _snap()
    provider.enrich_snapshot(snap.mint, snap)
    assert snap.sell_route_exists.status == DataStatus.MISSING
    assert snap.sell_route_exists.value is None
    assert "JUPITER_REQUEST_REJECTED_HTTP_400" in snap.sell_route_exists.reason


def test_wallet_events_persist_and_are_deduplicated_after_reload(tmp_path):
    path = tmp_path / "wallets.json"
    wallet = "11111111111111111111111111111111"
    event = {"wallet": wallet, "signature": "SIG_1", "side": "BUY",
             "mint": "TokenMint111111111111111111111111111111111", "ts": 123.0,
             "amount_sol": None, "source": "fixture"}
    watch = WalletWatch(str(path))
    watch.add(wallet)
    watch.ingest_verified_event(event)

    restored = WalletWatch(str(path))
    assert len(restored.events()) == 1
    assert restored.list()[0]["buy_events"] == 1
    restored.ingest_verified_event(event)
    assert len(restored.events()) == 1
    assert restored.list()[0]["buy_events"] == 1


def test_removing_wallet_removes_its_persisted_events(tmp_path):
    path = tmp_path / "wallets.json"
    wallet = "11111111111111111111111111111111"
    watch = WalletWatch(str(path))
    watch.add(wallet)
    watch.ingest_verified_event({"wallet": wallet, "signature": "SIG_1", "side": "BUY",
        "mint": "TokenMint111111111111111111111111111111111", "ts": 123.0,
        "amount_sol": None, "source": "fixture"})
    assert watch.remove(wallet) is True
    restored = WalletWatch(str(path))
    assert restored.list() == []
    assert restored.events() == []


def test_rugcheck_documented_nested_token_fields_are_used_without_requiring_rugged():
    from config import Config
    from token_safety import SafetyEvaluator, CRITICAL_SAFETY_FIELDS

    session = FakeSession([FakeResponse(payload={
        "token": {"mintAuthority": None, "freezeAuthority": None, "decimals": 6},
        "score_normalised": 24,
        "topHolders": [{"pct": 10.0}, {"pct": 5.0}],
        "risks": [{"name": "Low Liquidity", "level": "warn", "value": "$10,000"}],
        # This documented report fixture deliberately has no top-level `rugged` property.
    })])
    provider = RugCheckProvider(api_key="", max_retries=1, min_request_interval_seconds=0, session=session)
    snap = _snap(sell_route_exists=True)
    provider.enrich_snapshot(snap.mint, snap)

    assert snap.mint_authority_disabled.status == DataStatus.VALID
    assert snap.mint_authority_disabled.value is True
    assert snap.freeze_authority_disabled.status == DataStatus.VALID
    assert snap.freeze_authority_disabled.value is True
    assert snap.decimals.value == 6
    assert snap.rugcheck_score.value == 24
    assert snap.rugcheck_risks.value[0]["name"] == "Low Liquidity"
    assert snap.rugged_flag.status == DataStatus.MISSING
    assert "rugged_flag" not in CRITICAL_SAFETY_FIELDS

    result = SafetyEvaluator(Config()).evaluate(snap)
    assert result.hard_reject is False
    assert "rugged_flag" not in result.missing_fields
    assert result.passed is True


def test_market_health_does_not_call_empty_mock_discovery_live():
    from market_data import MarketDataService, MockProvider
    service = MarketDataService([MockProvider([])])
    assert service.discover(limit=10) == []
    assert service.health()["state"] == "MOCK"
    assert service.health()["last_success_ts"] is None


def test_market_health_does_not_call_rpc_only_snapshot_market_data():
    from market_data import MarketDataService
    class RpcOnly:
        name = "rpc_only"
        def discover(self, limit=30): return []
        def snapshot(self, mint, pool_id=None):
            return _snapshot_from_dict({"mint": mint, "mint_authority_disabled": True,
                                        "freeze_authority_disabled": True}, self.name)
        def health(self): return {"state": "CONNECTED"}
    service = MarketDataService([RpcOnly()])
    snap = service.snapshot("TokenMint111111111111111111111111111111111")
    assert snap is not None
    assert service.health()["state"] == "NO_MARKET_DATA"
    assert service.health()["last_success_ts"] is None


def test_dexscreener_discovery_merges_profiles_boosts_and_takeovers():
    from market_data import DexScreenerProvider
    mint = "TokenMint111111111111111111111111111111111"
    session = FakeSession([
        FakeResponse(payload=[{"chainId": "solana", "tokenAddress": mint, "url": "profile"}]),
        FakeResponse(payload=[{"chainId": "solana", "tokenAddress": mint, "amount": 10}]),
        FakeResponse(payload=[{"chainId": "solana", "tokenAddress": mint, "claimDate": "2026-10-12T00:00:00Z"}]),
    ])
    provider = DexScreenerProvider(max_retries=1, session=session)
    candidates = provider.discover(10)
    assert len(candidates) == 1
    assert candidates[0]["mint"] == mint
    assert set(candidates[0]["source_evidence"]) == {
        "dexscreener_profile", "dexscreener_boost", "dexscreener_community_takeover"
    }
    assert candidates[0]["boost_amount"] == 10
    assert len(session.calls) == 3
    assert provider.discover(10)[0]["mint"] == mint
    assert len(session.calls) == 3  # endpoint cache avoids per-scan request amplification


def test_geckoterminal_discovers_new_solana_pools_with_explicit_provenance():
    from market_data import GeckoTerminalProvider
    mint = "TokenMint111111111111111111111111111111111"
    quote = "So11111111111111111111111111111111111111112"
    session = FakeSession([FakeResponse(payload={
        "data": [{
            "id": "solana_pool123",
            "attributes": {
                "address": "pool123", "name": "TEST / SOL", "pool_created_at": "2026-10-12T00:00:00Z",
                "reserve_in_usd": "15000", "base_token_price_usd": "0.001"
            },
            "relationships": {
                "base_token": {"data": {"id": f"solana_{mint}"}},
                "quote_token": {"data": {"id": f"solana_{quote}"}}
            }
        }],
        "included": []
    })])
    provider = GeckoTerminalProvider(session=session, enabled=True, cache_ttl=60)
    candidates = provider.discover(10)
    assert len(candidates) == 1
    assert candidates[0]["mint"] == mint
    assert candidates[0]["pool_id"] == "pool123"
    assert candidates[0]["source"] == "geckoterminal_new_pool"
    assert candidates[0]["quote_mint"] == quote
    assert candidates[0]["liquidity_usd_hint"] == "15000"
    assert provider.health()["state"] == "CONNECTED"
    assert provider.discover(10)[0]["mint"] == mint
    # The discovery payload is reused as a partial market snapshot rather than
    # triggering a second request against the public rate limit.
    snap = provider.snapshot(mint, "different-provider-pool-id")
    assert snap is not None
    assert snap.price_usd.value == pytest.approx(0.001)
    assert snap.liquidity_usd.value == pytest.approx(15000)
    assert snap.sell_route_exists.status == DataStatus.MISSING
    assert len(session.calls) == 1


def test_geckoterminal_disabled_provider_makes_no_request():
    from market_data import GeckoTerminalProvider
    session = FakeSession([])
    provider = GeckoTerminalProvider(session=session, enabled=False)
    assert provider.discover(10) == []
    assert provider.health()["state"] == "DISABLED"
    assert session.calls == []


def test_agent_team_records_discovery_provenance_without_inflating_scores():
    from agent_team import AgentTeam
    from config import Config
    from strategy_engine import EntryPlan, Setup
    from token_safety import SafetyResult
    team = AgentTeam(Config().validate())
    snap = _snap()
    plan = EntryPlan(Setup.EARLY_MOMENTUM, 0.0005, 0.0004, 0.0006, 0.0007,
                     "below stop", ["TEST"], rejected=False, net_reward_risk=2.0)
    safety = SafetyResult(passed=True, hard_reject=False, score=0.9, reason_codes=[], missing_fields=[])
    momentum = {"score": 0.6, "contributions": {"fixture": 0.6}}
    out = team.run(snap, plan, safety, momentum, discovery_meta={
        "source": "dexscreener_profile",
        "source_evidence": ["dexscreener_profile", "geckoterminal_new_pool"],
        "event_type": "DISCOVERY", "received_ts": time.time(),
    })
    assert len(out) == 6
    for result in out:
        provenance = result.evidence["discovery_provenance"]
        assert provenance["source_count"] == 2
        assert provenance["multi_source_corroborated"] is True
        assert "MULTI_SOURCE_DISCOVERY_CORROBORATION" in result.reason_codes
    assert all(0.0 <= result.score <= 1.0 for result in out)


def test_rin_agent_uses_recent_verified_watched_wallet_activity_as_advisory():
    from agent_team import AgentTeam
    from config import Config
    from strategy_engine import EntryPlan, Setup
    from token_safety import SafetyResult
    team = AgentTeam(Config().validate())
    snap = _snap()
    plan = EntryPlan(Setup.EARLY_MOMENTUM, 0.0005, 0.0004, 0.0006, 0.0007,
                     "below stop", ["TEST"], rejected=False, net_reward_risk=2.0)
    safety = SafetyResult(passed=True, hard_reject=False, score=0.9, reason_codes=[], missing_fields=[])
    momentum = {"score": 0.6, "contributions": {"fixture": 0.6}}
    wallet_evidence = {"status": "OBSERVED", "recent_event_count": 3, "buy_events": 2,
                       "sell_events": 1, "unique_watched_wallets": 2,
                       "sources": ["helius-enhanced-transactions"], "coverage_state": "CONNECTED"}
    out = team.run(snap, plan, safety, momentum, wallet_evidence=wallet_evidence)
    rin = next(x for x in out if x.agent == "RIN")
    assert rin.status == "ADVISORY"  # wallet activity cannot authorize a trade
    assert "VERIFIED_WATCHED_WALLET_ACTIVITY" in rin.reason_codes
    assert rin.evidence["wallet_evidence"]["unique_watched_wallets"] == 2
    assert "profitability" in rin.evidence["interpretation"]


def test_intelligence_wallet_evidence_filters_by_mint_and_freshness():
    from intelligence import Intelligence
    from config import Config
    # Exercise the helper without constructing the full runtime dependency graph.
    class Watch:
        def events(self, limit=500):
            now = time.time()
            return [
                {"mint": "TARGET", "side": "BUY", "ts": now - 10, "wallet": "wallet-a", "source": "helius-enhanced-transactions"},
                {"mint": "TARGET", "side": "SELL", "ts": now - 900, "wallet": "wallet-b", "source": "helius-enhanced-transactions"},
                {"mint": "OTHER", "side": "BUY", "ts": now - 5, "wallet": "wallet-c", "source": "helius-enhanced-transactions"},
            ]
        def health(self):
            return {"state": "CONNECTED"}
    obj = object.__new__(Intelligence)
    obj.wallet_watch = Watch()
    evidence = obj._wallet_evidence_for_mint("TARGET", max_age_seconds=300)
    assert evidence["recent_event_count"] == 1
    assert evidence["buy_events"] == 1
    assert evidence["sell_events"] == 0
    assert evidence["unique_watched_wallets"] == 1


def test_geckoterminal_snapshot_provides_market_fallback_without_safety_claims():
    from market_data import GeckoTerminalProvider

    mint = "TokenMint111111111111111111111111111111111"
    session = FakeSession([FakeResponse(payload={
        "data": [{
            "id": "solana_POOL111",
            "attributes": {
                "address": "POOL111", "name": "TEST/SOL",
                "base_token_price_usd": "0.00042", "reserve_in_usd": "24500",
                "volume_usd": {"m5": "2100", "h1": "18000"},
                "price_change_percentage": {"m5": "4.2", "h1": "12.5"},
                "transactions": {"m5": {"buys": 18, "sells": 7}},
                "pool_created_at": "2026-10-12T01:00:00Z",
            },
            "relationships": {"base_token": {"data": {"id": "solana_TOKEN1"}}},
        }],
        "included": [{"id": "solana_TOKEN1", "attributes": {"address": mint, "symbol": "TEST", "name": "Test Token"}}],
    })])
    provider = GeckoTerminalProvider(timeout=1, session=session, enabled=True)
    snap = provider.snapshot(mint)

    assert snap is not None
    assert snap.symbol == "TEST"
    assert snap.price_usd.value == pytest.approx(0.00042)
    assert snap.liquidity_usd.value == pytest.approx(24500)
    assert snap.volume_5m_usd.value == pytest.approx(2100)
    assert snap.buys_5m.value == 18
    assert snap.sells_5m.value == 7
    assert snap.mint_authority_disabled.status == DataStatus.MISSING
    assert snap.sell_route_exists.status == DataStatus.MISSING
    assert len(session.calls) == 1


def test_geckoterminal_public_api_rate_guard_does_not_spam_requests():
    from market_data import GeckoTerminalProvider

    session = FakeSession([])
    provider = GeckoTerminalProvider(timeout=1, session=session, enabled=True)
    provider._last_request_at = time.time()
    assert provider.snapshot("TokenMint111111111111111111111111111111111") is None
    assert session.calls == []
    assert provider.health()["last_error"] == "LOCAL_PUBLIC_API_RATE_GUARD"


def test_sora_does_not_claim_entry_timing_when_five_minute_data_is_missing():
    from agent_team import SORA
    from config import Config
    from strategy_engine import EntryPlan, Setup
    from token_safety import SafetyResult
    snap = _snap(price_change_5m_pct=None)
    plan = EntryPlan(Setup.NONE, 0.0005, 0.0004, 0.0006, 0.0007,
                     "missing data", ["NO_SETUP"], rejected=True, reject_reason="NO_SETUP")
    safety = SafetyResult(passed=False, hard_reject=False, score=0.0,
                          reason_codes=["MISSING_LIQUIDITY"], missing_fields=["liquidity_usd"])
    result = SORA().analyze(snap, plan, safety, {"score": 0.0, "contributions": {}})
    assert result.status == "ADVISORY"
    assert "MISSING_SHORT_WINDOW_DATA" in result.reason_codes
    assert result.evidence["entry_timing"] == "UNVERIFIED"
