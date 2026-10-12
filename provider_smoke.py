"""Explicit read-only live provider smoke test for GROK6.

No orders, wallet signing, or transactions are submitted. Run with --live to acknowledge
that external APIs may consume plan quota. Provider results are printed with statuses only;
secrets and full watched wallet addresses are never printed.
"""
from __future__ import annotations
import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from helius_wallet_poller import HeliusWalletPoller
from jupiter_provider import JupiterSellRouteProvider
from launch_stream import PumpPortalLaunchStream
from market_data import DataStatus, DexScreenerProvider, GeckoTerminalProvider, MarketDataService
from rugcheck_provider import RugCheckProvider
from solana_rpc import SolanaMintSafetyProvider
from wallet_watch import WalletWatch


def _result(result):
    return {"status": getattr(result.status, "value", str(result.status)),
            "value": result.value, "source": result.source, "reason": result.reason}


def run(args) -> int:
    print("GROK6 LIVE READ-ONLY PROVIDER CHECK")
    print("No swaps/orders are submitted; external calls can consume provider quotas.")
    problems = 0

    # PumpPortal launch/migration data API: one socket for both subscriptions.
    if args.no_pumpportal:
        print("PumpPortal: SKIPPED (--no-pumpportal)")
    else:
        stream = PumpPortalLaunchStream()
        stream.start()
        try:
            time.sleep(max(1.0, args.watch_seconds))
        finally:
            stream.stop()
        health = stream.health()
        print("PumpPortal:", json.dumps(health, sort_keys=True))
        if health.get("state") in {"DEGRADED", "DISABLED"}:
            problems += 1

    dexscreener = DexScreenerProvider(timeout=args.timeout, max_retries=1)
    try:
        discovered = dexscreener.discover(limit=5)
        print("DexScreener:", json.dumps({"state": dexscreener.health()["state"],
              "solana_discovery_candidates": len(discovered), "health": dexscreener.health()}, sort_keys=True))
        if dexscreener.health()["state"] != "CONNECTED":
            problems += 1
    except Exception as exc:
        print(f"DexScreener: ERROR {type(exc).__name__}: {exc}")
        problems += 1

    gecko = GeckoTerminalProvider(timeout=args.timeout)
    try:
        gecko_candidates = gecko.discover(limit=5)
        print("GeckoTerminal:", json.dumps({"state": gecko.health()["state"],
              "new_solana_pools_received": len(gecko_candidates), "health": gecko.health()}, sort_keys=True))
        if gecko.enabled and gecko.health()["state"] != "CONNECTED":
            problems += 1
    except Exception as exc:
        print(f"GeckoTerminal: ERROR {type(exc).__name__}: {exc}")
        problems += 1

    if args.mint:
        rpc = SolanaMintSafetyProvider(timeout=args.timeout)
        rugcheck = RugCheckProvider(timeout=args.timeout, max_retries=1)
        jupiter = JupiterSellRouteProvider(timeout=args.timeout, paper_size_usd=args.paper_size_usd,
                                           min_request_interval_seconds=0.0, max_checks_per_cycle=1)
        market = MarketDataService([dexscreener, rpc, rugcheck, jupiter], cache_ttl=0, stale_after=60)
        snap = market.snapshot(args.mint)
        if snap is None:
            print("Mint checks: NO_SNAPSHOT")
            problems += 1
        else:
            output = {name: _result(getattr(snap, name)) for name in (
                "price_usd", "liquidity_usd", "decimals", "mint_authority_disabled",
                "freeze_authority_disabled", "rugged_flag", "sell_route_exists",
                "sell_route_price_impact_pct")}
            output["pair_age_seconds"] = snap.pair_age_seconds
            print("Mint evidence:", json.dumps(output, sort_keys=True, default=str))
            if snap.sell_route_exists.status != DataStatus.VALID or snap.sell_route_exists.value is not True:
                problems += 1
    else:
        print("Mint safety + Jupiter quote: SKIPPED (provide --mint to test one real mint)")

    if args.wallet:
        if not os.getenv("HELIUS_API_KEY", "").strip():
            print("Helius wallet poll: NOT_CONFIGURED (set HELIUS_API_KEY); request not sent")
            problems += 1
        else:
            with tempfile.TemporaryDirectory(prefix="grok6-smoke-") as td:
                watch = WalletWatch(str(Path(td) / "wallets.json"))
                watch.add(args.wallet, "smoke-check")
                poller = HeliusWalletPoller(watch, timeout=args.timeout, min_request_interval_seconds=0)
                result = poller.poll_wallet(args.wallet)
                print("Helius:", json.dumps({"result": result, "health": poller.health(),
                      "wallet": args.wallet[:6] + "…" + args.wallet[-4:]}, sort_keys=True, default=str))
                if result.get("status") in {"ERROR", "GAP_DETECTED"}:
                    problems += 1
    else:
        print("Helius wallet poll: SKIPPED (provide --wallet and configure HELIUS_API_KEY)")

    print(f"RESULT: {'CHECKS HAVE BLOCKERS' if problems else 'REQUESTS DID NOT REPORT ERRORS'}; this is not a trading/profitability validation.")
    return 1 if problems else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicitly allow real read-only provider requests")
    parser.add_argument("--mint", help="Solana token mint to query for market/risk/Jupiter quote evidence")
    parser.add_argument("--wallet", help="public Solana wallet address to poll via Helius (uses API credits)")
    parser.add_argument("--watch-seconds", type=float, default=5.0, help="PumpPortal observation window (default: 5s)")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--paper-size-usd", type=float, default=2.0)
    parser.add_argument("--no-pumpportal", action="store_true")
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("This script makes external read-only requests. Re-run with --live after checking provider quotas.")
    if args.mint and not (32 <= len(args.mint.strip()) <= 44):
        parser.error("--mint must be a 32–44 character base58-looking Solana address")
    if args.wallet and not (32 <= len(args.wallet.strip()) <= 44):
        parser.error("--wallet must be a 32–44 character base58-looking Solana address")
    if args.paper_size_usd <= 0:
        parser.error("--paper-size-usd must be positive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
