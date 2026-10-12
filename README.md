# GROK6 Command Center V7 — Solana Fast Launch Paper Trading

GROK6 is a Flask dashboard and Solana token-trading research engine with Paper mode by default and an optional local Jupiter Swap API V2 live execution adapter. A non-placeholder local private key requests LIVE automatically; the app authorizes it only when signing support loads, the signer parses correctly, the optional public address matches, and `JUPITER_API_KEY` is configured. No private key means PAPER by default. LIVE sends swaps on Solana mainnet; it is real-money execution, not a dry run.

## Requirements

- Python 3.10+
- Internet access for live providers. PumpPortal launch/migration discovery can be attempted without an API key; its metered token/account trade streams are intentionally not used. Jupiter quote-only route checks require `JUPITER_API_KEY`. Helius wallet polling is optional and requires `HELIUS_API_KEY`. The public Solana RPC is used by default but may be rate-limited. Offline tests use fixtures and do not make live network calls.

## Windows quick start

1. Extract the ZIP into a normal folder.
2. Double-click `run_windows.bat`.
3. Wait for dependency installation, smoke checks, and tests to finish.
4. Open `http://127.0.0.1:8080` after the server starts.

The launcher stops if smoke checks or tests fail. Keep the terminal window open while using the dashboard.

## Manual start

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m solana_app --smoke
python -m pytest -q
python -m solana_app
```

Health endpoint: `http://127.0.0.1:8080/health`.

## Configuration

The Windows launcher creates a project-root `.env` from `.env.example` on first launch and opens it in Notepad. Paste your own provider credentials into the matching blank values, save the file, and close Notepad. The application explicitly reads `.env` from the project folder at startup; external deployment variables (e.g. Render) take precedence. Restart GROK6 after editing `.env` so the new values are loaded.

**🟣 Local Phantom live setup:** open `.env` and find the purple-marked `GROK6_SOLANA_PRIVATE_KEY=` line. Paste the exported base58 private key from a **separate, low-balance trading wallet** into that line on your own computer. Never paste it into chat or upload `.env` to GitHub. You may also fill `GROK6_PHANTOM_PUBLIC_ADDRESS=` as an extra check; if supplied, it must match the key. A public address alone does not authorize trades. When the key is present, the app requests LIVE automatically and uses a separate database (`GROK6_LIVE_DB_PATH`) so old paper positions do not get mistaken for real on-chain positions. If the key is missing, the app defaults to PAPER; if signing support, Jupiter API access, or address validation fails, it shows LIVE_BLOCKED rather than pretending a swap happened. `.env` is plain text, so the purple marker is a visual divider rather than colored font.

Run `python env_check.py` to see which integrations are configured. It reports only status and never prints credentials. Keep `.env` private and do not upload it or share screenshots containing secrets. Paper mode uses `GROK6_DB_PATH` (default `./grok6.db`); a LIVE request uses `GROK6_LIVE_DB_PATH` (default `./grok6_live.db`). A valid local signer, matching public address if set, and `JUPITER_API_KEY` are required for LIVE. The default order-size cap is `$2` per entry, with a `0.03 SOL` reserve for network costs. The executor uses native SOL to buy tokens through Jupiter, then sells tokens back to SOL; the wallet must therefore hold SOL for both trading and fees. Actual trade fills and exits are recorded only after Jupiter reports a successful execution result. On restart, persisted LIVE positions are checked against on-chain token balances; mismatches block automatic management and latch SAFE_STOP. Do not interpret configured credentials as proof of network or balance readiness; verify `/api/system/status` and logs on your local machine.

## Optional real-time launch and on-chain checks

- `PUMPPORTAL_ENABLED=true` starts a background PumpPortal `subscribeNewToken` + `subscribeMigration` stream using one WebSocket. A key is not required for these discovery subscriptions. `PUMPPORTAL_API_KEY` is optional; this project does not subscribe to metered `subscribeTokenTrade` / `subscribeAccountTrade`. The dashboard exposes connection state and buffered event count; network connectivity is not confirmed by offline tests.
- `SOLANA_RPC_URL` overrides the public mainnet RPC for `getAccountInfo` checks. Classic SPL mint/freeze authorities and decimals are read; Token-2022/non-standard layouts are explicitly reported as unsupported rather than decoded incorrectly.
- `RUGCHECK_API_KEY` is an optional header for RugCheck report enrichment; its response `rugged` flag is never treated as a honeypot flag.
- The safety provider uses Jupiter Swap API V2 `GET /order` without `taker` for quote-only sell-route checks. The separate live executor uses `/order` with the verified wallet, signs the returned transaction locally, and submits it to `/execute`; Jupiter reports execution status/signature. Quotes are not guarantees of successful execution, and API failures are not treated as proof that a token has no route.
- `HELIUS_API_KEY` enables conservative polling of explicitly watched public wallet addresses. Initial polling establishes a signature baseline; older history is not silently classified as new activity. Polling only infers BUY/SELL when token-transfer direction is explicit; it does not fabricate PnL. API limits and plan features must be checked in the Helius dashboard.
- Configure keys/endpoints in your shell or deployment environment; do not commit secrets into `.env.example` or source control.

## Data limitations

Discovery combines the PumpPortal launch/migration buffer with DexScreener token profiles and token-pair snapshots. DexScreener supplies price/liquidity/volume/transaction snapshots and may provide `pairCreatedAt`; it does not provide verified top-holder distribution, deployer concentration, a direct honeypot flag, or historical OHLCV through this adapter. `Observed candles` are built only from snapshots received by GROK6 after startup and are labelled `not exchange OHLCV`. Safety gates remain closed while critical evidence is missing. Helius wallet history is optional and subject to provider limits/cursor gaps; no wallet profitability label or PnL is inferred from isolated swap events.

Discovery breadth depends on the upstream endpoint and is not a guarantee that every newly launched Solana pool will be found. Price data and paper fills are for research and can differ materially from live execution.

## Runtime safety and financial operation journal

- `GROK6_KILL_SWITCH=true` engages a persistent `SAFE_STOP` latch in the SQLite store. It blocks new entries while position monitoring/exits continue. The latch stays engaged after restart until the operator turns the environment switch off and performs an explicit confirmed reset after reconciliation through the resolver API. No public dashboard reset endpoint is exposed.
- `GET /api/system/status`, `GET /health`, and the dashboard mode stamp show the authoritative resolved mode and its reasons. In LIVE, `live_authorized` is true only after local signer and Jupiter configuration checks succeed; actual swaps still require funded balances, live prices, routes, and on-chain confirmation.
- Financial operation intents use durable operation IDs and idempotency keys. `PENDING`, `UNKNOWN`, and `PARTIAL` operations cannot affect the verified operation PnL ledger. Confirmation requires a matching transaction hash plus provider confirmation evidence; PnL can be applied once, and its fee/slippage/gas amounts must match the confirmed record.
- `GET /api/operations` is a read-only view of this separate operation ledger. It is not connected to a live swap adapter, and it is not the same accounting source as the existing simulated-position portfolio. This separation avoids counting the same simulated PnL twice.

Default paper configuration starts with $20 virtual equity, $2 maximum notional per position, $6 total exposure, six total positions, 0.5% risk per trade, 2% aggregate open risk, and a -$1 daily realized-loss lock. Risk limits are not evidence that the strategy is profitable.

## Render

Use `render.yaml` as a deployment starting point. Configure `GROK6_DB_PATH` on a mounted persistent disk. Verify the selected Render plan supports the disk configuration before deploying; the project remains paper-only.

## Tests

Run `python -m pytest -q`. Tests use a mock market-data provider and do not submit real orders.


## Diagnosing candidates that do not trade

Run `python diagnose_waiting.py --db ./grok6.db` after the application has written candidate decisions. The report groups the latest decision per mint and shows the most frequent block reasons. If the database is elsewhere, pass `--db` or set `GROK6_DB_PATH`.

## Provider checks and API diagnostics

- `GET /api/providers/health` shows source state, recent successful response age, request count, and provider error details.
- `GET /api/rejections/by-reason?minutes=60` summarizes persisted outcomes from the selected time window.
- `GET /api/observed-candles/<mint>` returns locally observed bars with explicit coverage. It does not fabricate missing candles.
- `GET /api/wallet-watch` returns wallet cursor/poller health and observed events. Cursor gaps are reported as `DEGRADED`.

See `docs/API_INTEGRATION_GUIDE.md` for official docs, signup locations, environment variables, and limitations; `docs/OPEN_SOURCE_AUDIT.md` for the current source-reuse research scope; `docs/COMMUNITY_RESEARCH.md` for public Discord/YouTube developer resources; and `docs/IMPLEMENTATION_REPORT_V3.md` for the local test results and remaining live checks.

`NEEDS_REFRESH` means a usable market snapshot could not be obtained. `WAITING` means the candidate was evaluated but required evidence is still missing. These are intentionally different states.

The Solana public mainnet RPC is used by default for classic SPL mint/freeze authority checks and may be rate-limited. Set `SOLANA_RPC_URL` to a provider endpoint to override it. RPC mint data does **not** prove a token is sellable and does not supply pool liquidity or honeypot status; those checks remain blocked until supported data sources return verified values. Token-2022 mint layouts remain unsupported unless explicitly decoded.


## Runtime Fix V4
See `RUNTIME_FIX_V4_REPORT.md` for the repair to the RPC `TokenSnapshot` constructor, the safety scoring fix for missing advisory fields, and the PumpPortal key-status display correction. Required missing evidence (including a configured Jupiter sell-route check) continues to block paper entries rather than being silently bypassed.

## Command Center V6 integration

The V6 interface is the default `/` dashboard. It includes Overview, Launch Radar, Agent Brain, Wallet Intelligence, Trade Anatomy, Positions, Runtime Logs, and Settings tabs. The cards/tables are populated from application state and these read-only endpoints:

- `/health`, `/api/status`, `/api/providers/health`
- `/api/candidates`, `/api/candidates/pipeline`, `/api/rejections/by-reason`
- `/api/agents/status`, `/api/portfolio`, `/api/positions`, `/api/fills`, `/api/logs`
- `/api/wallet-watch`, `/api/charts/<mint>`, `/api/settings`, `/api/operations`

Price charts use GROK6-collected observed samples and are explicitly labelled **not exchange OHLCV**. Unknown or absent data renders as `N/A`, `UNKNOWN`, or `NOT_CONFIGURED`; no synthetic metrics are added to the dashboard. Existing dashboard assets are retained for audit/rollback, while `templates/base.html` and `templates/index.html` now load `static/css/command_center.css` and `static/js/command_center.js`.

The `TokenSnapshot` schema now permits a minimal `TokenSnapshot(mint=...)` with all unobserved evidence defaulting to `DataStatus.MISSING`. It does not map missing data to `False` or numeric zero. Regression coverage is in `tests/test_token_snapshot_regression.py`.

## Verification status for this V6 package

The source tree passed Python compilation, JavaScript syntax checking, `python -m solana_app --smoke --provider mock`, and `python -m pytest -q` in the build environment. The Flask HTTP server itself could not be launched in that environment because Flask was not installed and package-index DNS/network access was unavailable. On Windows, `run_windows.bat` installs `requirements.txt`, runs smoke checks and tests, and only then starts the server. Internet access is required for dependency installation and real upstream provider data.


## V6 diagnostics from the first real dashboard run
The V6 dashboard clarifies two common states: a candidate with missing market liquidity is shown at Verify; a candidate with market data but missing mint/freeze authority or Jupiter sell-route evidence is shown at Safety. Final rejects and expired setups do not inflate active-stage counts. Helius wallet tracking is optional. If `JUPITER_API_KEY` is not configured, the dashboard shows `PAPER ENTRY BLOCKED`; this is intentional fail-closed behavior, not a reason to bypass the safety gate.

For the V6 changes driven by the dashboard runtime output, see `docs/RUNTIME_REVIEW_FROM_DASHBOARD_2026-10-11.md` and `V6_INTEGRATION_REPORT.md`.


## Command Center V7 — Fast Launch Sniper + Continuation Watcher

V7 adds an event-driven, high-priority path for PumpPortal launch/migration events, bounded persisted retries, reason aggregation, a continuation classifier, three-tier readiness metrics, and dedicated fast-path dashboard telemetry. The incoming launch event is persisted and queued immediately; provider I/O and strategy evaluation run on workers rather than the WebSocket receive thread. The periodic scanner remains available as a secondary discovery path.

The Fast Launch profile is available only for fresh PumpPortal events and still passes through the same authoritative safety, strategy, agent, and risk gates as other trades. New-launch entry windows and no-chase limits are enforced. Tokens with expired timing or weakening evidence are moved to continuation monitoring instead of blindly chased.

For fast-launch paper positions, the configurable model defaults to a 12% protective stop, banking half at a 30% gross price target, then arming a trailing runner at a 75% gross price target with a configurable 22% drawdown trail. These are simulation settings, not expected returns or profit guarantees; fees, slippage, liquidity gaps, and fast reversals can materially reduce realized paper PnL. Other strategy categories retain their existing exit rules.

An independent position monitor requests fresh price-only marks concurrently for up to six open positions on a default 3-second cycle. The same position lifecycle drives the selected executor: PAPER uses simulated fills; LIVE sends real Jupiter swaps for entries, TP1, stop-loss, TP2, and trailing exits. IMPORTANT: SL/TP exits are managed by this local process and price-monitor loop; no exchange-style server-side stop order is installed on-chain. If the process or internet connection stops, protective exits cannot run until the service recovers. Ambiguous execution responses freeze the affected position for reconciliation and latch SAFE_STOP to avoid blind duplicate sells. Its latency and mode are exposed through `/api/system/status` and `/api/fast-path/status`.

V7 endpoints:

- `/api/system/status` — core readiness, provider health, data completeness, event bus, sniper, retries, continuation and position-monitor state.
- `/api/fast-path/status` — priority queue metrics, event/decision latency percentiles, retry counts and paper-only status.
- `/api/continuation` — persisted continuation classifications and evidence timestamps.

### V7 validation notes

The bundled test suite and mock-mode smoke check results are recorded in `V7_IMPLEMENTATION_REPORT.md` and `BUILD_AND_TEST_REPORT.md`. Dashboard route tests are included and run when Flask is installed; the current build environment does not have Flask installed, so that one optional route-test is skipped here. The Windows launcher installs the declared dependencies before smoke tests and the main suite.

Live PumpPortal/RPC/DexScreener/Jupiter results require network access and provider availability. Missing mandatory safety evidence continues to block entries. Never put a real private key in the project archive: create/edit `.env` locally after extraction. Real mainnet execution was not testable from the offline build environment; before funding the wallet, verify signer readiness, wallet balance, Jupiter API response, and use the default `$2` max entry cap.

## API Discovery & Agent Provenance Update (2026-10-12)

This build adds cached multi-feed DEX Screener discovery, GeckoTerminal Solana new-pool discovery plus rate-limited market fallback snapshots, richer YUZU/MOMO/SORA evidence, source provenance, and RIN's advisory use of recent verified events from explicitly watched wallets. When critical safety evidence is missing, agents provide advisory-only diagnostics while the central gate still blocks entries. These feeds are not a guarantee of trade eligibility; all safety and risk gates remain in force. Live trading is optional and fail-closed on signer, configuration, or ambiguous-execution problems. See `GROK6_V7_API_DISCOVERY_IMPLEMENTATION_REPORT_2026-10-12.md` and `docs/OPEN_SOURCE_API_INTEGRATION_2026-10-12.md` for implementation details, sources, test results, and live-network limitations.
