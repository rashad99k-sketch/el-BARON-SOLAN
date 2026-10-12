# Wallet intelligence and early-launch research

## Supplied repository findings
The supplied `meme-coin-trading-bot` describes a curated wallet pool and short rolling clusters of distinct buyers across Pump.fun/Raydium/Pump AMM/LaunchLab, followed by safety and risk gates. Its own strategy notes state the approach backtested but did not work live. Treat it as a hypothesis, not evidence of profitability.

## Additional public repos reviewed
- https://github.com/agentcomponents/solana-trading-bot — DexScreener discovery, Jupiter tradeability, RugCheck/GoPlus safety, SQLite positions and paper mode. README claims 299 tests; not independently run in this task.
- https://github.com/AFKmoney/pumpfun-agent — launch detection, copy trading, order-flow, wallet profile and dashboard/API concepts. Audit maturity and live-order controls before reuse.
- https://github.com/DracoR22/handi-cat_wallet-tracker — wallet activity notifications for Pump.fun/Raydium/Jupiter/PumpSwap.
- https://github.com/justFiveDev/Solana-Copy-Trading-Bot — WebSocket transactionSubscribe wallet monitoring and trade parsing.
- https://github.com/bljuane/sol-bot-dashboard — wallet PnL/trade dashboard; README notes limitations in mark-to-market and FIFO classification.
- https://github.com/BaileyOnBlockchain/gary-solana-sniper — launch detection and dashboard concept; README describes a snapshot, not proof of production completeness.

## Changes in this package
- Added persistent tracked-wallet watchlist, dashboard tables, and API endpoints.
- Activity is displayed only when verified events are ingested by a future RPC/Geyser adapter; this package does not yet connect to Helius/Yellowstone/Geyser or subscribe to Pump.fun program events.
- No private key is requested and no live orders/copy trades are submitted. Wallet performance metrics remain unknown until verified transaction history is supplied.
- Blind copy-trading is unsafe: late detection, wallet splitting, bundled launch buys, wash trading, creator wallets, sell liquidity and fees can erase apparent edge.
