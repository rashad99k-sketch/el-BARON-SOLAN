# GROK6 API Integration Guide

**Scope:** read-only market discovery and evidence collection plus paper trading. No live swap execution, signing, seed phrase, or private key is used.

**Verification level:** the provider adapters and fixtures were locally tested. This container could not reach external APIs, so no live credentials, quotas, live response shapes, or current account entitlements have been smoke-tested. Check the provider's current official documentation before production use.

## Provider matrix

| Provider | What GROK6 uses it for | Official documentation / signup | Environment variable | Key needed by this integration? | Known constraints and honest status |
|---|---|---|---|---|---|
| PumpPortal | Real-time Pump.fun `subscribeNewToken` and `subscribeMigration` discovery | [Real-time Data API](https://pumpportal.fun/data-api/real-time/) · [Fees](https://pumpportal.fun/fees/) | `PUMPPORTAL_ENABLED`, optional `PUMPPORTAL_API_KEY` | No key for these two discovery subscriptions per provider docs | One shared WebSocket is used. Metered `subscribeTokenTrade` and `subscribeAccountTrade` are not subscribed. Live connection is unverified here. |
| DexScreener | Pair enrichment: token price, liquidity, volume, short-term price change, transaction counts, and nullable pair age | [API Reference](https://docs.dexscreener.com/api/reference) | None | No key used by this adapter | The reference lists 60 req/min for profile/discovery endpoints and 300 req/min for pair/token endpoints. `pairCreatedAt` may be null. These endpoints do not supply all safety data or an OHLCV history series. |
| Solana JSON-RPC | Read token mint account authority information and classic SPL decimals | [Solana RPC overview](https://solana.com/docs/rpc) · [HTTP methods](https://solana.com/docs/rpc/http) | Optional `SOLANA_RPC_URL` | No API key on the default public endpoint; dedicated providers may require one | Default public RPC may rate-limit. Classic SPL layout is parsed; Token-2022 and unknown layouts are explicitly unsupported in this version. RPC authority evidence alone does not establish sellability. |
| RugCheck | Token report evidence: its own `rugged` classification, authority fields, and available top-holder concentration | [RugCheck API/Swagger](https://api.rugcheck.xyz/swagger/index.html) · [FluxRPC token docs](https://fluxrpc.com/docs/rugcheck/tokens) | Optional `RUGCHECK_API_KEY` | Basic call is attempted without a key; key may alter access/limits | Schema/limits must be reconfirmed on the official current docs. `rugged` is not renamed or inferred as `honeypot`. Missing report/fields remain missing. |
| Jupiter Swap API V2 | Query-only route feasibility for a hypothetical token-to-SOL sale at a configured notional amount | [Swap API V2](https://dev.jup.ag/docs/swap) · [Order endpoint](https://dev.jup.ag/docs/api-reference/swap/order) · [API Portal](https://developers.jup.ag/portal) | `JUPITER_API_KEY` | This integration requires a key, following the current reference's authentication model | Calls `GET /swap/v2/order` without `taker`; no transaction is built, signed, or sent. `amount` uses raw input-token units. A quote is not proof of future execution. HTTP request errors do not automatically become a confirmed no-route result. Price impact remains unknown unless exposed by a documented field. |
| Helius Enhanced Transactions | Optional polling of explicitly watched public wallets; deduplicated swap events and cursor-gap monitoring | [Enhanced Transactions](https://www.helius.dev/docs/enhanced-transactions/overview) · [Webhooks/WebSockets](https://www.helius.dev/solana-webhooks-websockets) · [Pricing](https://www.helius.dev/pricing) · [Dashboard](https://dashboard.helius.dev/) | `HELIUS_API_KEY`, `GROK6_HELIUS_POLL_INTERVAL_SECONDS`, `GROK6_HELIUS_MAX_WALLETS_PER_CYCLE` | Yes | This package uses bounded polling of the latest page (up to 100 transactions per watched wallet request), not a webhook/stream subscription. Provider limits and plan entitlements may change. A cursor gap is surfaced as `DEGRADED`; observed swap events do not establish wallet profitability or realized PnL. |

## Obtaining credentials safely

### PumpPortal

1. Read the provider's [Data API documentation](https://pumpportal.fun/data-api/real-time/) and [fees](https://pumpportal.fun/fees/).
2. Start with the two free discovery subscriptions only: new token and migration.
3. Leave `PUMPPORTAL_API_KEY` blank for discovery if the current provider docs still allow keyless discovery. Keep the stream switch `PUMPPORTAL_ENABLED=true`.
4. Do not enable trade/account subscriptions without explicitly reviewing meter costs and linked-wallet requirements.

### Jupiter

1. Open the [Jupiter developer portal](https://developers.jup.ag/portal).
2. Register/sign in, create an API key, and review current quotas/terms shown for that account.
3. Store it in deployment environment as `JUPITER_API_KEY`. Do not paste it into screenshots, public issues, GitHub, frontend code, or logs.
4. The current adapter makes a quote-only call and deliberately omits `taker`; there is no swap-signing or execution path.

### Helius

1. Open the [Helius dashboard](https://dashboard.helius.dev/), create/select an app, and copy the API key.
2. Store the key in the backend environment as `HELIUS_API_KEY`.
3. Configure wallet addresses using the dashboard's wallet-watch controls. The software stores public addresses only; never enter a seed phrase or private key.
4. Confirm the chosen plan's API credits, per-second limits, and retention before increasing the tracked-wallet count.

### RugCheck

1. Review the [current API schema](https://api.rugcheck.xyz/swagger/index.html) and [token report documentation](https://fluxrpc.com/docs/rugcheck/tokens).
2. Check the current authentication instructions. If using a key, store it as `RUGCHECK_API_KEY` in the server environment.
3. Confirm actual account quotas before raising local request budgets. This project's local limiter is intentionally conservative and is not a guarantee of provider entitlement.

### Solana RPC

1. For a smoke test, the public mainnet endpoint can be used through the default configuration; it may throttle requests.
2. For a continuously running scanner, choose a provider from the [Solana RPC overview](https://solana.com/docs/rpc) and use the HTTPS endpoint as `SOLANA_RPC_URL`.
3. Never add a send-transaction method to this read-only authority adapter. Check results at `confirmed` commitment, handle RPC errors, and display source/status.

## Local setup

Copy `.env.example` into your local environment configuration (do not commit a real `.env`). Install dependencies and run:

```bash
python -m pip install -r requirements.txt
python -m solana_app --smoke --provider mock
python -m pytest -q
python -m solana_app --provider auto
```

Then inspect the dashboard/API:

- `/health`
- `/api/providers/health`
- `/api/rejections/by-reason?minutes=60`
- `/api/candidates`
- `/api/wallet-watch`
- `/api/observed-candles/<mint>`

A `CONNECTED` value must come from a successful provider operation. `READY`, `NOT_CHECKED`, `NOT_CONFIGURED`, `DEGRADED`, and `DISCONNECTED` are distinct statuses; do not relabel them as healthy/live data. A blank key or offline network must remain visible as a configuration/data blocker.

## Data interpretation rules

- `rugged=false` is only the provider's `rugged` classification, not a proven non-honeypot result.
- A Jupiter quote at one moment and one size is only quote-route evidence. It does not prove an actual sell will execute at a later time.
- No route, invalid mint, malformed request, provider error, and missing key must not be collapsed into one undifferentiated false value.
- DexScreener's `pairCreatedAt` is nullable. Missing pair age remains `N/A`.
- Candles produced by `ObservedCandleBuilder` are labeled `observed candles (GROK6-collected) — not exchange OHLCV`; there is no backfill or interpolation.
- Wallet labels such as profitability, win rate, or realized PnL remain `UNKNOWN` until a reliable, sufficiently complete transaction/balance ledger exists.
- All trades in this package are paper positions. Tests use fixtures and do not submit real transactions.
