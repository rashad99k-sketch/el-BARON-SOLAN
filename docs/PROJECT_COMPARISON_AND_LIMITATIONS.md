# GROK6 final review: source comparison and operating limits

## Decision

Use `grok6_rebuilt` as the primary application because it has a single `Intelligence` orchestrator, explicit missing/stale/error data states, one paper executor, persisted position lifecycle, risk limits, and a dashboard API. Do not merge the legacy project's live transaction path into this build.

## What was inspected

### `meme-coin-trading-bot-main`
- Larger research project with wallet-cluster signals, collectors, Yellowstone/gRPC dependencies, a JSONL-oriented dashboard, and optional live execution.
- Its own README describes it as a research snapshot and says its strategy did not work live.
- It requires external infrastructure and prebuilt wallet-pool data for its primary wallet-cluster signal; that is not an immediately usable, self-contained new-pair hunter.
- We did not copy private-key/signing/live-execution components into GROK6.

### `GROK6_SOLANA_MEME_PAPER_REBUILT`
- Has a direct application entry point, market-data provider abstraction, safety evaluator, strategy engine, six-position risk caps, paper fills with modeled costs, SQLite persistence, position manager, and responsive Flask dashboard.
- This is the better base for the requested paper-first system.
- The prior import failure was caused by Flask not being installed in the current test environment; Flask is listed in `requirements.txt`.
- The UTC boundary error was hardened in `position_manager.py`: UTC day boundaries now use Python's standard-library UTC timezone and no longer depend on the OS timezone database. Other IANA time zones continue to use `ZoneInfo` and `tzdata`.

## Critical market-data limitation

DexScreener's mapped pair response currently provides price, liquidity, volume, price change, and transaction counts. It does not provide verified holder concentration, deployer concentration, mint authority, freeze authority, honeypot status, or full OHLCV in this adapter. Those fields remain explicitly missing. The strategy must keep a candidate in `WAITING` when critical safety evidence is missing; it must not silently assume safety or manufacture a chart.

The next production-quality integration should add an independently verified Solana safety provider and a source for actual candle/transaction history, then record source, timestamp, age, and confidence for each field. Provider-specific coverage and API limits must be tested before claiming that every new launch is detected.

## Dashboard behavior

The dashboard is designed as an operational terminal, not a performance promise:
- health, scanner state, market-data state, and last scan age;
- virtual equity, realized/unrealized PnL, win rate, profit factor, drawdown, exposure, and daily loss lock;
- candidate radar with decision and reason codes;
- open positions and trade anatomy with entry, stop, TP1, TP2, and trailing-stop levels when present;
- persisted decision/runtime logs and CSV export where available;
- explicit paper-only labels and no live execution path.

Candlesticks must only appear when actual OHLCV is available. When it is absent, the chart should say so rather than display invented candles.

## Safety defaults

- Paper mode only; live trading remains disabled.
- No wallet seed phrase or private key is required.
- Maximum 6 open positions; per-position notional and aggregate exposure caps apply.
- Missing critical safety data blocks entry.
- Fees, slippage, and price impact are included in paper fills.
- A paper win rate or PnL is not evidence of live profitability.

## Validation status for this package

- 20 tests not requiring Flask: expected to pass in the supplied environment.
- Full test suite requires dependencies in `requirements.txt`, including Flask. Flask could not be installed in the current environment because the package index was unreachable, so the full import smoke test and actual Flask server/browser run remain unverified here.
- External live provider behavior was not exercised; no claim is made that this package has been proven profitable or ready for live trading.
