# GROK6 V7 — Solana LIVE Execution Patch (2026-10-12)

## What changed

- Added `solana_live_execution.py`, a local signing adapter for Jupiter Swap API V2 `/order` + `/execute`.
- A non-placeholder `GROK6_SOLANA_PRIVATE_KEY` requests LIVE automatically. Missing private key keeps PAPER. An invalid key, missing `solders` support, address mismatch, or missing `JUPITER_API_KEY` results in `LIVE_BLOCKED`; it does not silently simulate fills while labelled LIVE.
- The private key is read from the local environment only. It is not copied into the `Config` dataclass, API responses, logs, or this project ZIP.
- LIVE uses `GROK6_LIVE_DB_PATH` to isolate actual positions from older Paper positions.
- Actual entry and exit fills are only recorded after Jupiter returns success with signature and amounts.
- Jupiter execute transport failures are treated as ambiguous, not blindly retried; affected operations trigger SAFE_STOP/reconciliation.
- Startup reconciliation compares recorded open LIVE positions with on-chain token balances. A mismatch blocks automatic management and latches SAFE_STOP.
- Default maximum spend is USD 2 per entry, hard-clamped by the executor to USD 5, with a minimum 0.03 SOL reserve. Existing portfolio/strategy/safety checks remain in effect.

## Setup (local only)

If `HELIUS_API_KEY` is configured and `SOLANA_RPC_URL` is blank, the runtime now builds the Helius Mainnet RPC URL automatically. An explicit `SOLANA_RPC_URL` still takes precedence. If neither is configured, the fallback is the shared public Solana RPC and can be rate-limited.

1. Run `python -m pip install -r requirements.txt`.
2. Open `.env` in the project root. Fill `GROK6_SOLANA_PRIVATE_KEY` with the Base58 private key for a **separate low-balance Solana trading wallet**. Optional `GROK6_PHANTOM_PUBLIC_ADDRESS` must match the derived signer address if provided.
3. Obtain/configure `JUPITER_API_KEY`. `SOLANA_RPC_URL` is optional; blank defaults to Solana mainnet public RPC, which can be rate-limited.
4. Keep `GROK6_LIVE_MAX_TRADE_USD=2.0` and `GROK6_LIVE_MIN_SOL_RESERVE=0.03` for initial validation. No real swap should be considered tested until the local runtime confirms readiness and an operator independently checks signatures on-chain.
5. Start locally with `run_windows.bat` on Windows (it installs requirements, runs smoke/tests, and launches the app), or run `python -m solana_app` from the project directory after installing `requirements.txt`. Inspect `/api/system/status` plus logs. A configured key alone is not evidence of live readiness. The dashboard labels now reflect the resolved runtime mode and clearly distinguish configuration readiness from an actually reported successful swap.

## Critical limitations

- This offline build environment cannot verify a live mainnet swap, wallet funding, API quotas, or provider uptime. The full tests are offline tests, not a guarantee of profitable or error-free live operation.
- Stop-loss, take-profit, and trailing exits are **local software decisions** triggered by the running process and market-price feed. This patch does not create resting server-side/on-chain stop orders. If the computer, process, RPC, Jupiter, or price feed is down, those exits may not run promptly.
- The wallet must have enough SOL for the trade and transaction costs, plus the configured reserve. Trade candidates can fail execution because a route, price guard, balance, or safety gate blocks them.
- A private key grants the software the ability to spend from that address. Do not use a primary wallet, do not share the key, and never commit `.env`; `.env` is in `.gitignore`.

## Verification

- `python -m py_compile *.py tests/*.py`: passed.
- `pytest -q --disable-warnings --maxfail=1`: 119 passed, 1 skipped.
- Live signing against solders and real mainnet APIs was not executed in this offline environment. The new adapter's request flow, ambiguous-timeout handling, mode selection, and startup-balance reconciliation have offline unit coverage.
