# Live Dashboard Diagnosis — 2026-10-11

Source: dashboard text provided by the user (runtime timestamp `2026-10-11 03:50:52Z`). This is a reading of that runtime report, not a claim that the same runtime was reproduced locally.

## Observed runtime values
- Pump.fun stream: connected, 78 events.
- DexScreener: connected, 416 requests.
- RPC mint safety: degraded by `TypeError: TokenSnapshot.__init__() missing 1 required positional argument: 'honeypot_flag'`.
- RugCheck: connected; 33 requests; local rate guard shown.
- Jupiter sell-route: not configured; `JUPITER_API_KEY` missing.
- Helius wallet poller: not configured; API key missing.
- Decisions in last hour: 405; 391 `NEEDS_REFRESH`, 14 `REJECTED`.
- Paper portfolio: $20 virtual equity, 0 open paper positions, $0 PnL.
- Common block reason: missing critical safety data. Additional reported gaps included `deployer_pct`, holder concentration, mint/freeze authority, sell-route evidence and unavailable market snapshots.

## Root causes addressed in V4
1. The RPC adapter's `TokenSnapshot` call omitted the required `honeypot_flag` constructor argument. The exception was caught by the adapter and returned no RPC snapshot, so valid mint/freeze authority evidence never reached the merge layer. V4 now includes `honeypot_flag` as explicit MISSING evidence; it does not infer a honeypot-negative result from RPC.
2. Safety scoring divided by a fixed count that included optional/unavailable evidence, so advisory gaps could indirectly keep an otherwise critical-gate-clean candidate from passing the minimum score. V4 scores only evaluated checks and applies a small penalty for missing advisory concentration fields. Those fields remain visibly missing.
3. Provider UI interpreted `api_key_configured=false` as a missing-key error even for PumpPortal's free launch/migration subscriptions. V4 includes the correct note and an explicit `api_key_required=false`.

## Important remaining configuration and data constraints
- `sell_route_exists` remains a critical paper-safety gate. With `JUPITER_API_KEY` blank, the bot should continue to block paper entries; set a valid key according to Jupiter's current official docs before expecting this gate to pass. Do not bypass the gate just to create trades.
- Helius wallet tracking is optional and remains disabled until `HELIUS_API_KEY` is configured.
- `deployer_pct` is not inferable from a creator address alone; no percentage is invented.
- RugCheck may not return `topHolders` for every new mint; absent values stay unknown.
- Some brand-new Pump.fun tokens do not yet have a DexScreener pair/snapshot. Those candidates must wait for usable market data or expire under bounded lifecycle policy.
- Classic SPL authority parsing is supported; Token-2022 layouts are still reported unsupported by this adapter and must not be treated as safe.

## Operational next steps
1. Replace the running folder with this V4 archive after backing up `grok6.db`, `wallet_watch.json`, and the previous release.
2. Add `JUPITER_API_KEY` in the environment (not in source control). Optionally add `HELIUS_API_KEY` for watched-wallet polling.
3. Run the Windows launcher, which installs dependencies and runs checks.
4. Confirm `/api/providers/health` shows no RPC `TokenSnapshot` TypeError and watch `/api/rejections/by-reason?minutes=60` after a few scan cycles.
5. Keep paper mode and live execution disabled.
