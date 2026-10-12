# GROK6 Command Center V7 — ZIP package

1. Extract the archive into a normal folder.
2. Run `run_windows.bat` on Windows. On first run it creates `.env` from `.env.example` and opens it for you to add credentials; it then installs requirements, reports configuration status without printing secrets, runs smoke checks/tests, and only then launches the dashboard.
3. Open `http://127.0.0.1:8080`.
4. Your real keys belong in your local `.env` or deployment environment, never in `.env.example`. `.env` is ignored by Git and excluded from this ZIP. Run `python env_check.py` to check which integrations are configured without exposing values.

Default mode: PAPER. `WATCH_ONLY` disables new simulated entries; `LIVE` or `GROK6_LIVE_TRADING=true` resolves to `LIVE_BLOCKED`. Live execution is hard-disabled. `GROK6_KILL_SWITCH=true` latches `SAFE_STOP` persistently and blocks new entries.

V7 brings event-driven launch intake, prioritized candidate evaluation, bounded retry scheduling, continuation classification, a dedicated position monitor, a fast-launch paper runner profile, and expanded dashboard telemetry.

See `V7_IMPLEMENTATION_REPORT.md` for prior V7 source changes and `GROK6_MULTICHAIN_SAFETY_LEDGER_IMPLEMENTATION_2026-10-12.md` for the runtime-mode resolver, verified operation ledger, additional test results, and the features that remain unimplemented (including EVM adapters and live execution).
