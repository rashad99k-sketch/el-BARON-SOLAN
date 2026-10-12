# GROK6 V7 — Open-Source Research and API Integration Notes

Date: 2026-10-12

## What was added in this implementation

1. **DEX Screener multi-feed discovery** in `market_data.py`: latest token profiles, latest token boosts, and latest community takeovers are combined and deduplicated by Solana mint. Each candidate carries `source_evidence`; each feed is cached for 60 seconds to reduce repeated requests. These feeds are discovery hints, not safety checks or evidence of profitability.
2. **GeckoTerminal discovery and market fallback** in `market_data.py`: read-only discovery from `GET https://api.geckoterminal.com/api/v2/networks/solana/new_pools`, with the documented API-version header, a minimum 60-second cache, and a conservative local request guard around the public beta quota. When the new-pool response includes market attributes, GROK6 reuses them as a partial cached snapshot; a token-pools endpoint can provide a secondary market snapshot when allowed by the rate guard. Missing fields remain missing, and this source never fabricates token-safety or sell-route evidence.
3. **Application wiring** in `solana_app.py`: GeckoTerminal is enabled by default in `auto`/`dexscreener` provider mode and can be disabled with `GROK6_GECKOTERMINAL_ENABLED=false`.
4. **Source provenance and richer agent evidence** in `intelligence.py` and `agent_team.py`: agents receive source count, source names, event type, approximate event age, and whether the primary source is the direct PumpPortal launch stream. YUZU records observed momentum/volume windows, MOMO records liquidity, transaction flow, buy ratio, and turnover-vs-liquidity evidence, and SORA does not claim timing is verified when the 5-minute change is missing. If critical safety evidence is missing, the six agents now produce explicitly downgraded `ADVISORY` reviews with blockers attached instead of appearing as empty `WAITING` nodes; this does not authorize entries or weaken the central safety gate. Multiple discovery sources do not automatically increase strategy scores.
5. **Wallet evidence for RIN**: the agent can now summarize fresh verified BUY/SELL events from configured watched wallets for the exact candidate mint. This is advisory only; no wallet profitability or whale status is inferred.
6. **Health reporting** in `dashboard.py` and live smoke-test visibility in `provider_smoke.py`.

## Official provider documentation reviewed

- DEX Screener API reference: https://docs.dexscreener.com/api/reference
  - Latest token profiles: `/token-profiles/latest/v1` (60 requests/minute documented).
  - Latest token boosts and community takeovers: documented discovery feeds (60 requests/minute documented).
  - Pair/token lookup endpoints: up to 300 requests/minute for documented pair/token endpoints.
- PumpPortal real-time data: https://pumpportal.fun/data-api/real-time/
  - `subscribeNewToken` and `subscribeMigration` are described as free discovery subscriptions.
  - Token/account trade subscriptions are metered and require a key and funded linked wallet. GROK6 does not enable those metered subscriptions here.
- GeckoTerminal public API: https://api.geckoterminal.com/docs/index.html
  - Public API is beta, approximately 10 calls/minute, and responses are documented as cached for one minute. GROK6 caches new-pool discovery for at least 60 seconds.
- Helius documentation: https://www.helius.dev/docs/getting-data and https://www.helius.dev/pricing
  - Existing `helius_wallet_poller.py` remains read-only. Free-plan quotas/features can change; verify the current account dashboard before sizing polling.
  - Helius' newer Parsed Events/Streams products are documented separately and are not silently substituted for the existing Enhanced Transactions poller in this change.
- Jupiter Swap API: https://developers.jup.ag/docs/api-reference/swap/v1/swap
  - Current documentation requires an `x-api-key` for the documented Swap V1 endpoint; Jupiter notes the older Metis API is superseded by Swap V2. GROK6's existing provider uses its configured endpoint and must be reviewed against the current Jupiter portal before any API migration. No signing or broadcasting was added.

## Open-source projects reviewed as research leads

These are reference implementations, not trusted dependencies. Their code was not copied wholesale into GROK6; integrating a repository requires license review, dependency review, and tests against GROK6's interfaces.

- https://github.com/ricoboost/meme-coin-trading-bot — launch monitoring, configurable filters, developer-wallet tracking, paper mode, and safety checks. Its README labels the project as not actively maintained; use as a design reference only.
- https://github.com/dnafund/Degen-Scanner — PumpPortal launch scanning, Helius-based developer-wallet tracing, and wallet-cluster analysis. Treat cluster heuristics as hypotheses requiring validation, not proof of insider behavior.
- https://github.com/galvinlam/meme-sniper — screening/signal engine architecture, state machines, rule-based candidate scoring, and separation between observation and trading.
- https://github.com/dunkin-dee/meme-sniper — launch recorder and paper-trading research approach; emphasizes measuring current behavior before claiming a strategy edge.
- https://github.com/Hashdevlol/pumpfun-sniper — token scoring concepts involving developer history, liquidity, holder distribution, and social signals; educational/reference use only.

## YouTube and Discord handling

YouTube videos and Discord communities can help discover implementation ideas, known API changes, and operational issues, but they are not canonical market-data feeds. A video or chat message must not be treated as proof that an endpoint, quota, or feature is current. This implementation does not scrape YouTube/Discord or introduce an unofficial scraper. If social intelligence is added later, it must use authorized APIs/bot access, source timestamps, deduplication, rate-limit handling, and an explicit low-confidence label. Social messages must never override on-chain safety gates or directly authorize trades.

## Live-test limitation for this build environment

A read-only smoke test was attempted for DEX Screener and GeckoTerminal, but this environment could not resolve the external hostnames (DNS/network unavailable). Both providers therefore reported `DEGRADED`; zero live candidates were received. This is **not** a provider-side failure verdict, and it is **not** a successful live API verification. Run `python provider_smoke.py --live --no-pumpportal --timeout 5` from a network-enabled machine to verify live connectivity. No live transactions were attempted.

## Safety boundary

- Paper mode remains the only execution path; live trading remains disabled.
- No private keys or seed phrases are required for discovery.
- Missing or stale safety evidence must not be converted to a safety pass.
- Multi-source discovery is provenance/corroboration, not a profit guarantee.
