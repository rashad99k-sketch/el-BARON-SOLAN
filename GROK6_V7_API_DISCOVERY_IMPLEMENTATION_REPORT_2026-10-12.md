# GROK6 V7 API Discovery & Agent Provenance — Implementation Report

Date: 2026-10-12

## Changes made

- `market_data.py`
  - Expanded `DexScreenerProvider.discover()` from only latest token profiles to combine latest profiles, latest boosts, and latest community takeovers.
  - Deduplicated by mint and recorded source provenance and available boost/takeover metadata.
  - Added per-endpoint 60-second caching to reduce public API request volume.
  - Added `GeckoTerminalProvider` for Solana new-pool discovery plus a secondary market snapshot path. New-pool market attributes are reused as partial cached snapshots to avoid an extra request; token-pools requests use a conservative local request guard for the public beta quota. Unknown fields remain missing and no safety or sell-route evidence is fabricated.
  - Enhanced `MarketDataService.discover()` to preserve multiple source labels for duplicate candidates.
- `solana_app.py`
  - Wires GeckoTerminal into normal `auto`/`dexscreener` mode. It is configurable with `GROK6_GECKOTERMINAL_ENABLED`.
- `intelligence.py`
  - Preserves discovery source evidence and metadata in the candidate lifecycle and passes it to the agent team.
- `agent_team.py`
  - Adds discovery provenance to each agent's evidence: source names/count, event type, event age when available, direct-launch-stream marker, and multi-source corroboration reason code. It does not increase strategy scores just because multiple discovery feeds mention a token.
  - YUZU records observed price/volume windows and a volume acceleration ratio when calculable; MOMO records liquidity, buys/sells, buy ratio, and 5-minute turnover relative to liquidity; SORA treats missing 5-minute price-change data as unverified instead of declaring timing `PASS`.
- `intelligence.py`
  - When critical safety evidence is missing, the six agents now inspect available data in advisory mode and each result carries `entry_authorized=false` plus the blockers. The decision remains `NEEDS_REFRESH`, and no entry can be opened through this branch.
  - RIN now summarizes recent verified BUY/SELL events for the candidate mint from explicitly watched wallets. It stays advisory-only and does not claim a wallet is profitable or a whale.
- `intelligence.py` / `solana_app.py`
  - Connects the existing WalletWatch event buffer to candidate evaluation, filters evidence to the candidate mint and a recent time window, and passes the summary to RIN.
- `dashboard.py`
  - Exposes GeckoTerminal provider health in `/api/system/status`.
- `provider_smoke.py`
  - Adds a read-only GeckoTerminal live smoke check and renames the DEX Screener count to reflect that it is a multi-feed discovery count.
- `.env.example`
  - Documents the GeckoTerminal feature toggle.
- `tests/test_provider_integrations.py`
  - Added tests for DEX multi-feed deduplication/caching, GeckoTerminal pool parsing/caching/disable behavior, and agent source provenance.
- `docs/OPEN_SOURCE_API_INTEGRATION_2026-10-12.md`
  - Records official provider docs, GitHub research leads, API caveats, and the live-test limitation.

## Verification

- Command: `python -m pytest -q`
- Result after this extension: **110 passed, 1 skipped**.
- The recorded starting baseline was 101 passed, 1 skipped. Nine new tests are now passing across discovery, fallback market snapshots, agent evidence, and rate-guard behavior.
- Skip reason from `python -m pytest -q -rs`: `tests/test_v7_application_wiring.py:38` could not import Flask because Flask is not installed in this execution environment. `Flask==3.0.3` is listed in `requirements.txt`; install project requirements in a suitable environment to run this integration test.
- No live swaps, transaction signing, or transaction broadcasting were added.
- Live read-only smoke command attempted: `python provider_smoke.py --live --no-pumpportal --timeout 5`.
- Result: the execution environment could not resolve `api.dexscreener.com` or `api.geckoterminal.com` (DNS/network unavailable). Both live checks returned `DEGRADED` and zero candidates. This does not establish that the providers are down; live connectivity remains unverified and must be tested from a network-enabled deployment environment.
- PumpPortal, Helius, RugCheck, and Jupiter live connectivity was not tested in this smoke run. No API keys were provided, and no secret was added to the project.

## Remaining tasks / limitations

- Configure `HELIUS_API_KEY` and `JUPITER_API_KEY` as deployment secrets if you want the optional wallet and sell-route checks to operate. Keep credentials out of source control.
- Run the read-only smoke tests from Render or another network-enabled environment, then inspect the provider health endpoint and sanitized logs.
- Review the current Jupiter portal and provider endpoint before changing API versions; the implementation does not silently migrate the existing adapter.
- GeckoTerminal is a rate-limited fallback, not a guaranteed real-time feed for every candidate. A new-pool response may lack volume or price-change fields, and its public request budget means not every token can receive a separate token-pools lookup each minute.
- The new-pool feed is not a substitute for complete token safety or valid sell-route evidence.
- Social posts and multi-source discovery do not prove whale profitability, token legitimacy, or future returns.
- Live trading remains disabled. This build is not certified for real-money execution based on the current evidence.
