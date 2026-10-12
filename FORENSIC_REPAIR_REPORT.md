# GROK6 V7 — Forensic Repair & Validation Report

## Scope

This repair package was built from the available GROK6 Command Center V7 project archive, not from the user's live Windows folder or its live runtime database. Changes are confined to paper execution, position monitoring/accounting, candidate/agent diagnostics, reason-code normalization, dashboard display, and regression tests. Real execution remains disabled.

## Verified test results

- `python -m compileall -q .`: PASS
- `python -m pytest -q`: **77 passed, 1 skipped**
- `node --check static/js/command_center.js`: PASS
- `python -m solana_app --smoke --provider mock`: `SMOKE OK: all components initialized.`
- Added `tests/test_position_monitor.py` to verify the monitor explicitly reports STOPPED before startup rather than implying it is healthy.
- The single skipped test is the Flask HTTP route wiring/integration test because Flask is unavailable in this build environment. Consequently, no claim is made that a real Flask server/browser session was validated here.
- No live PumpPortal stream, DexScreener, Solana RPC, RugCheck, Jupiter or Helius requests were used to claim a successful provider integration. Mock smoke and automated tests are not live-provider evidence.

## Source-level fixes implemented

### 1. Paper entry idempotency and capacity

- `PositionManager.open_position` now serializes local entry decisions and refuses to open a second active position for the same mint.
- `Storage.insert_open_position_guarded` applies the mint-level check and global open-position cap inside a SQLite `BEGIN IMMEDIATE` transaction, and records the position plus ENTRY fill atomically.
- A race/retry is prevented at the persistence boundary rather than relying only on the scanner or event bus.
- Existing historical duplicate positions are **not deleted, merged, or rewritten**. The code prevents new duplicate opens; any already-persisted duplicates still require review against the local DB and ledger.

### 2. Position mark freshness

- `PositionMonitor` passes the provider observation timestamp through to `PositionManager` rather than assigning the monitor's current clock to an old quote. A VALID price without a timestamp is not treated as fresh.
- Missing, invalid, stale, or future observation timestamps are rejected for position management and produce explicit log events.
- Monitor health reports `RUNNING`, `DEGRADED`, or `STOPPED`, including last tick age. The application health endpoint can now reflect a stopped/unavailable monitor instead of implicitly treating it as healthy.

### 3. PnL, fees and equity accounting

- `RiskEngine.portfolio_snapshot` is the central snapshot used by the dashboard and risk engine.
- Realized PnL is summed across all positions, including partially realized PnL on a position that remains open after TP1. Counting only closed rows would lose legitimate TP1 results.
- Unrealized PnL is calculated on the remaining quantity from the last mark, with the entry-fee allocation for the remaining quantity deducted once. Exit fees that have already been realized are not subtracted a second time.
- PnL is marked unavailable if an open position has no valid mark; stale marks are separately counted and new entries are blocked while valuation is stale/unavailable.
- Position endpoint includes mark state, mark age, realized PnL and unrealized PnL.

### 4. Agent status transparency

- The agent status API differentiates stored actual evaluations from entries waiting behind incomplete critical safety data.
- A missing or incomplete safety review is no longer presented as a numeric `0.00` agent score that could be mistaken for a genuine evaluation.
- Consensus score is derived from recorded agent scores rather than using the overall candidate score as a proxy.

### 5. Sell-route reason semantics

- Missing evidence for a sell-route check is now named `MISSING_SELL_ROUTE_EVIDENCE`.
- A confirmed negative route result remains a distinct `NO_SELL_ROUTE` condition.
- Legacy `MISSING_SELL_ROUTE_EXISTS` records are normalized for API rejection aggregation. Provider connectivity still does not imply that a particular token has a valid current sell route.

### 6. Position dashboard

- Position rows include unique position IDs, mark age/quality, realized PnL and unrealized PnL.
- Trade Anatomy selection uses position ID, allowing two persisted positions with the same mint to be inspected individually if old duplicates exist.

## Important interpretation of the user's observed values

A `-$0.40` combined PnL with **zero open positions** is not expected to move every refresh: if all trades are closed and no new fills occur, realized PnL and equity should remain fixed. The live-changing portion is unrealized PnL while positions remain open and valid marks update. The correct acceptance test is to change a position's mark in a controlled test, verify its unrealized PnL/equity update, and then verify that closed-only portfolio PnL remains stable until another fill occurs.

The screenshots alone do not prove why the earlier EINSTEIN positions were created. The source-level fix prevents a new open position for an already-open mint, but this package does not automatically reconcile or close existing duplicate rows in the user's local database.

## Remaining verification limits

- The local runtime DB on the user's PC was not available to this build; its open positions, fills, and stored PnL were not directly reconciled.
- The Flask HTTP route test is skipped because Flask is missing in this environment. Install `requirements.txt` in a local virtual environment and rerun the test suite before deploying.
- No claim is made that the live Jupiter route is now available for every token. A connected provider can still return no route, incomplete evidence, a timeout or a rate-limit response for an individual token.
- Current screen values (candidate counts, retry backlog, agent counts, decision latency) must be rechecked in the user's running instance after updating the source code.
- This is not a profitability certification. It does not establish win rate or guarantee returns.

## Safe deployment procedure

1. Stop the currently running paper bot before replacing source files.
2. Back up the entire existing project folder, particularly `grok6.db`, WAL/SHM files, `.env`, and local logs.
3. Extract this ZIP into a separate new folder first; do not overwrite the live project as the first step.
4. Copy the user's existing `.env` into the test folder manually; do not copy secrets into `.env.example` or GitHub.
5. Install dependencies from `requirements.txt`, then run `python -m compileall -q .`, `python -m pytest -q`, `node --check static/js/command_center.js`, and `python -m solana_app --smoke --provider mock`.
6. Keep `GROK6_MODE=PAPER` and live execution disabled. Do not let the new process share a live SQLite database with another running bot process.
7. Validate the dashboard and mark-to-market behavior before relying on the updated instance.

## Files materially changed

- `storage.py`
- `position_manager.py`
- `position_monitor.py`
- `risk_engine.py`
- `intelligence.py`
- `token_safety.py`
- `dashboard.py`
- `static/js/command_center.js`
- `templates/index.html`
- `tests/test_position_manager.py`
- `tests/test_risk.py`
- `tests/test_dashboard_runtime_semantics.py`
- `tests/test_reason_normalization.py`

Additional test and build evidence is summarized in `BUILD_AND_TEST_REPORT.md`.
