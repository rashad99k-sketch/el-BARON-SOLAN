"""Application entry point. One explicit initialization path. Never imports a network client at module scope."""
from __future__ import annotations
import argparse
import datetime as dt
import logging
import os
import sys
import threading
import time
from typing import Dict

from config import Config, load_config
from dashboard import build_dashboard
from discovery_service import DiscoveryService
from intelligence import Intelligence
from market_data import DexScreenerProvider, GeckoTerminalProvider, MarketDataService, MockProvider
from paper_execution import PaperExecutor
from solana_live_execution import SolanaLiveExecutor, configured_private_key
from position_manager import PositionManager
from risk_engine import RiskEngine
from storage import Storage
from wallet_watch import WalletWatch
from launch_stream import PumpPortalLaunchStream
from solana_rpc import SolanaMintSafetyProvider
from rugcheck_provider import RugCheckProvider
from jupiter_provider import JupiterSellRouteProvider
from helius_wallet_poller import HeliusWalletPoller
from event_bus import BusEvent, LaunchEventBus, Priority
from retry_policy import ReasonAggregator, RetryPolicy, RetryScheduler
from sniper import FastLaunchSniper
from continuation import ContinuationWatcher
from position_monitor import PositionMonitor
from social_radar import SocialTrendRadar
from mode_resolver import ModeResolver

log = logging.getLogger("grok6")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


class Application:
    """Wires all components. Single initialization path used by both the server and tests."""

    def __init__(self, cfg: Config, provider_mode: str = "auto"):
        self.cfg = cfg
        # A real local signing key requests LIVE automatically. Keep the historical
        # paper DB separate so stale simulated positions can never be sold on-chain.
        key_configured = bool(configured_private_key())
        env_live_requested = (key_configured or bool(cfg.live_trading_enabled)
                              or str(cfg.mode or "PAPER").upper() == "LIVE")
        cfg.solana_live_key_configured = key_configured
        if key_configured:
            cfg.mode = "LIVE"
            cfg.live_trading_enabled = True
        if env_live_requested:
            cfg.db_path = os.environ.get("GROK6_LIVE_DB_PATH", "./grok6_live.db").strip() or "./grok6_live.db"
        self.storage = Storage(cfg.db_path)
        self.storage.log("INFO", "app", "STORAGE_READY", f"db={cfg.db_path}")

        self.launch_stream = PumpPortalLaunchStream()
        providers = self._build_providers(provider_mode)
        # Put launch events first so fresh-launch metadata is preserved during provider merge.
        if provider_mode != "mock":
            providers.insert(0, self.launch_stream)
            rpc_provider = SolanaMintSafetyProvider(timeout=self.cfg.provider_timeout_seconds)
            if rpc_provider.rpc_url:
                providers.append(rpc_provider)
                self.storage.log("INFO", "app", "RPC_SAFETY_READY", "Solana mint authority checks enabled")
            else:
                self.storage.log("WARN", "app", "RPC_SAFETY_DISABLED", "SOLANA_RPC_URL not configured; mint/freeze authority remain unknown")
            # These providers only enrich evidence; they never place or sign transactions.
            providers.append(RugCheckProvider(timeout=self.cfg.provider_timeout_seconds, max_retries=1))
            providers.append(JupiterSellRouteProvider(timeout=self.cfg.provider_timeout_seconds,
                paper_size_usd=self.cfg.max_position_notional))
        self.market = MarketDataService(providers,
                                        cache_ttl=float(cfg.scan_interval_seconds) / 3.0,
                                        stale_after=float(cfg.scan_interval_seconds) * 3,
                                        price_cache_ttl=float(cfg.position_price_cache_ttl_seconds),
                                        provider_timeout=float(cfg.provider_timeout_seconds) + 3.0)

        # A private key requests LIVE. It is authorized only when the local signer,
        # configured public address (if provided), and Jupiter API dependency validate.
        if key_configured:
            self.executor = SolanaLiveExecutor(cfg)
            cfg.live_execution_ready = bool(self.executor.ready)
            cfg.live_execution_blocker = "" if self.executor.ready else str(self.executor.reason)
            cfg.live_wallet_public_address = str(self.executor.public_address or "")
            self.storage.log(
                "INFO" if self.executor.ready else "ERROR", "live_executor",
                "LIVE_EXECUTOR_READY" if self.executor.ready else "LIVE_EXECUTOR_BLOCKED",
                "Local Jupiter execution adapter readiness checked; private key value was not logged.",
                {"ready": self.executor.ready, "reason": self.executor.reason,
                 "wallet_address": self.executor.public_address or None},
            )
        else:
            self.executor = PaperExecutor(cfg)
            cfg.live_execution_ready = False
            cfg.live_execution_blocker = "SOLANA_PRIVATE_KEY_NOT_CONFIGURED"
            cfg.live_wallet_public_address = ""
        self.mode_resolver = ModeResolver(cfg, self.storage)
        self.pm = PositionManager(cfg, self.storage, self.executor, mode_resolver=self.mode_resolver)
        self.position_monitor = PositionMonitor(cfg, self.storage, self.market, self.pm)
        self.social_radar = SocialTrendRadar(cfg, timeout=cfg.provider_timeout_seconds)
        self.risk = RiskEngine(cfg, self.storage)
        self.intelligence = Intelligence(cfg, self.storage, self.market, self.risk, self.pm)
        self.intelligence.social_radar = self.social_radar
        self.discovery = DiscoveryService(cfg, self.intelligence, self.storage)
        self.wallet_watch = WalletWatch(os.environ.get("GROK6_WALLET_WATCH_PATH", "./wallet_watch.json"))
        # Optional .env public address: register it for read-only wallet intelligence.
        # A public address cannot authorize transactions; the LIVE safety gate remains unchanged.
        phantom_address = (str(getattr(self.executor, "public_address", "") or "").strip()
                           or os.environ.get("GROK6_PHANTOM_PUBLIC_ADDRESS", "").strip())
        placeholder_addresses = {
            "PASTE_YOUR_PUBLIC_SOLANA_ADDRESS_HERE",
            "YOUR_SOLANA_PUBLIC_ADDRESS",
            "PASTE_YOUR_SOLANA_ADDRESS_HERE",
            "CHANGE_ME",
        }
        if phantom_address and phantom_address.upper() not in {x.upper() for x in placeholder_addresses}:
            try:
                self.wallet_watch.add(phantom_address, "Phantom wallet (LIVE signer)" if getattr(self.executor, "ready", False) else "Phantom wallet (read-only)")
                self.storage.log(
                    "INFO", "wallet_watch", "PHANTOM_PUBLIC_ADDRESS_LOADED",
                    "Phantom address registered for wallet intelligence.",
                )
            except ValueError:
                self.storage.log(
                    "WARN", "wallet_watch", "PHANTOM_PUBLIC_ADDRESS_INVALID",
                    "GROK6_PHANTOM_PUBLIC_ADDRESS is not a valid-looking Solana public address; ignored.",
                )
        self.intelligence.wallet_watch = self.wallet_watch
        self.wallet_poller = HeliusWalletPoller(
            self.wallet_watch,
            timeout=self.cfg.provider_timeout_seconds,
            poll_interval_seconds=float(os.getenv("GROK6_HELIUS_POLL_INTERVAL_SECONDS", "30")),
            max_wallets_per_cycle=int(os.getenv("GROK6_HELIUS_MAX_WALLETS_PER_CYCLE", "10")),
        )

        # V7: event-driven launch path. The launch stream callback only enqueues an event;
        # provider I/O and strategy evaluation run on dedicated workers, not the WS thread.
        self.reasons = ReasonAggregator(storage=self.storage)
        self.retries = RetryScheduler(
            RetryPolicy(base_delay_seconds=cfg.retry_base_delay_seconds,
                        max_delay_seconds=cfg.retry_max_delay_seconds,
                        max_attempts=cfg.retry_max_attempts),
            storage=self.storage,
        )
        self.intelligence.retry_scheduler = self.retries
        self.intelligence.reason_aggregator = self.reasons
        self.continuation = ContinuationWatcher(cfg, self.intelligence, self.storage,
                                                retries=self.retries, reasons=self.reasons)
        self.sniper = FastLaunchSniper(cfg, self.intelligence, self.storage,
                                       self.retries, self.reasons, self.continuation)
        self.bus = LaunchEventBus(self._handle_bus_event,
                                  max_queue_size=cfg.event_queue_size,
                                  workers=cfg.event_workers,
                                  storage=self.storage)
        self.launch_stream.add_event_listener(self._on_launch_event)
        self._retry_stop = threading.Event()
        self._retry_thread: threading.Thread | None = None
        self._started_at = time.time()

        # recover any previously open positions
        opens = self.pm.reconcile()
        self.storage.log("INFO", "app", "RECOVERED",
                         f"recovered {len(opens)} open positions")

    def _on_launch_event(self, payload: dict) -> None:
        """Persist the new candidate immediately, then enqueue without provider I/O."""
        try:
            event = BusEvent.from_provider(payload, priority=Priority.HIGH)
            # Make the event identity persistent with the candidate. This is a short local
            # SQLite write; market/RPC/Jupiter calls remain on bus workers, never the WS thread.
            event.payload["source_event_id"] = event.event_id
            self.intelligence.register_launch_event(event.payload)
            before_full = int(self.bus.health().get("dropped_on_full") or 0)
            accepted = self.bus.enqueue(event, priority=Priority.HIGH)
            after_health = self.bus.health()
            if not accepted:
                is_full = int(after_health.get("dropped_on_full") or 0) > before_full
                self.storage.patch_candidate_payload(event.mint, {"sniper": {
                    "state": "QUEUE_SATURATED" if is_full else "DUPLICATE_EVENT",
                    "event_id": event.event_id, "last_ingress_at": time.time()}})
                self.storage.log("WARN" if is_full else "INFO", "event_bus",
                                 "QUEUE_SATURATED" if is_full else "DUPLICATE_EVENT",
                                 f"{event.kind} {event.mint} was not queued",
                                 {"mint": event.mint, "event_id": event.event_id,
                                  "queue_depth": after_health.get("queue_depth")})
                if is_full:
                    retry = self.retries.schedule(event.mint, "QUEUE_SATURATED")
                    self.storage.patch_candidate_payload(event.mint, {"retry_state": {
                        "attempts": retry.attempts, "next_retry_at": retry.next_retry_at,
                        "exhausted": retry.exhausted, "last_reason": retry.last_reason}})
        except Exception as exc:
            self.storage.log("ERROR", "event_bus", "INGRESS_ERROR",
                             f"{type(exc).__name__}: {exc}")

    def _handle_bus_event(self, event: BusEvent) -> None:
        if event.kind in {"NEW_TOKEN", "MIGRATION", "RETRY"}:
            self.sniper.handle_event(event)
        else:
            self.continuation.handle_event(event)

    def _retry_loop(self) -> None:
        while not self._retry_stop.is_set():
            try:
                for mint, retry_state in self.retries.claim_due():
                    candidate = self.storage.get_candidate(mint)
                    if not candidate:
                        self.retries.clear(mint)
                        continue
                    saved = candidate.get("payload") or {}
                    discovery = saved.get("discovery") or {}
                    payload = {**discovery, "mint": mint,
                               "pool_id": candidate.get("pool_id") or discovery.get("pool_id"),
                               "symbol": candidate.get("symbol") or discovery.get("symbol"),
                               "event_type": discovery.get("event_type") or "NEW_TOKEN",
                               "retry_attempt": retry_state.attempts}
                    event = BusEvent(mint=mint, kind="RETRY", source="retry_policy",
                                     event_id=f"retry:{mint}:{retry_state.attempts}:{int(time.time()*1000)}",
                                     payload=payload, received_at=time.time(), priority=Priority.NORMAL)
                    if not self.bus.enqueue(event, priority=Priority.NORMAL):
                        self.retries.release_after_enqueue_failure(mint, 1.0)
            except Exception as exc:
                self.storage.log("ERROR", "retry_policy", "RETRY_LOOP_ERROR",
                                 f"{type(exc).__name__}: {exc}")
            self._retry_stop.wait(0.25)

    def _start_retry_worker(self) -> None:
        if self._retry_thread and self._retry_thread.is_alive():
            return
        self._retry_stop.clear()
        self._retry_thread = threading.Thread(target=self._retry_loop, name="grok6-retry-dispatch", daemon=True)
        self._retry_thread.start()

    def _stop_runtime_workers(self) -> None:
        self._retry_stop.set()
        if self._retry_thread:
            self._retry_thread.join(timeout=3.0)
            self._retry_thread = None
        self.continuation.stop(timeout=3.0)
        self.position_monitor.stop(timeout=3.0)
        self.bus.stop(timeout=5.0, drain=True)

    def _build_providers(self, mode: str):
        providers = []
        if mode in ("auto", "dexscreener"):
            try:
                # A live launch must fail fast on a transient DEX error; the bounded
                # per-candidate retry scheduler retries asynchronously instead of burning
                # several provider timeout periods inside the hot launch path.
                providers.append(DexScreenerProvider(
                    timeout=self.cfg.provider_timeout_seconds,
                    max_retries=1,
                ))
                self.storage.log("INFO", "app", "PROVIDER_READY", "dexscreener provider initialized")
            except Exception as e:
                self.storage.log("WARN", "app", "PROVIDER_UNAVAILABLE",
                                 f"dexscreener unavailable: {e}")
            if os.getenv("GROK6_GECKOTERMINAL_ENABLED", "true").lower() not in {"0", "false", "no", "off"}:
                try:
                    providers.append(GeckoTerminalProvider(timeout=self.cfg.provider_timeout_seconds))
                    self.storage.log("INFO", "app", "PROVIDER_READY", "geckoterminal discovery provider initialized (60s cached public feed)")
                except Exception as e:
                    self.storage.log("WARN", "app", "PROVIDER_UNAVAILABLE",
                                     f"geckoterminal unavailable: {e}")
        if mode in ("auto", "mock") or not providers:
            providers.append(MockProvider())
            self.storage.log("INFO", "app", "PROVIDER_READY", "mock provider initialized")
        return providers

    def today(self) -> str:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    def run_server(self) -> None:
        state = {
            "cfg": self.cfg,
            "mode_resolver": self.mode_resolver,
            "executor": self.executor,
            "storage": self.storage,
            "market": self.market,
            "intelligence": self.intelligence,
            "observed_candles": self.intelligence.candles,
            "discovery": self.discovery,
            "wallet_watch": self.wallet_watch,
            "wallet_poller": self.wallet_poller,
            "launch_stream": self.launch_stream,
            "risk": self.risk,
            "pm": self.pm,
            "position_monitor": self.position_monitor,
            "today": self.today,
            "bus": self.bus,
            "sniper": self.sniper,
            "continuation": self.continuation,
            "retries": self.retries,
            "reasons": self.reasons,
            "started_at": self._started_at,
            "social_radar": self.social_radar,
        }
        app = build_dashboard(state)
        self.bus.start()
        self._start_retry_worker()
        self.continuation.start()
        self.position_monitor.start()
        self.launch_stream.start()
        self.wallet_poller.start()
        self.social_radar.start()
        self.discovery.start()
        self.storage.log("INFO", "app", "SERVER_START",
                         f"http://{self.cfg.host}:{self.cfg.port}; mode={self.mode_resolver.current().value}; live_execution={'ENABLED' if self.mode_resolver.snapshot().get('live_authorized') else 'NOT_AUTHORIZED'}")
        try:
            app.run(host=self.cfg.host, port=self.cfg.port, debug=False, use_reloader=False, threaded=True)
        finally:
            self.discovery.stop()
            self.wallet_poller.stop()
            self.social_radar.stop(timeout=3.0)
            self.launch_stream.stop()
            self.position_monitor.stop(timeout=3.0)
            self._stop_runtime_workers()
            self.market.close()
            self.storage.close()


def smoke_check(cfg: Config, provider_mode: str = "mock") -> int:
    """Initialize everything without submitting a single paper order, then exit 0/1."""
    try:
        app = Application(cfg, provider_mode=provider_mode)
        # Exercise the pure-function path (no server, no orders)
        app.intelligence.health()
        app.market.health()
        app.discovery.health()
        app.social_radar.health()
        app.social_radar.stop(timeout=0.1)
        app.market.close()
        app.storage.close()
        print("SMOKE OK: all components initialized.")
        return 0
    except Exception as e:
        print(f"SMOKE FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="grok6")
    parser.add_argument("--smoke", action="store_true",
                        help="Initialize without starting the server, then exit.")
    parser.add_argument("--provider", choices=["auto", "dexscreener", "mock"],
                        default="auto")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    configure_logging(cfg.log_level)

    if args.smoke:
        return smoke_check(cfg, provider_mode=args.provider)

    app = Application(cfg, provider_mode=args.provider)
    app.run_server()
    return 0


if __name__ == "__main__":
    sys.exit(main())
