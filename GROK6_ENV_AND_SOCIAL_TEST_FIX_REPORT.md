# GROK6 V7 — Environment Loading and Social Radar Regression Fix

## Findings

The failing assertion was caused by `SocialTrendRadar` using `cfg_value or os.getenv(...)`. If an earlier test loaded a developer's `.env` into `os.environ`, an explicitly blank social credential in the test config silently fell back to the process credential. The missing-credentials test could therefore become order/environment dependent and report `DEGRADED` instead of `NOT_CONFIGURED`.

## Changes made

1. `social_radar.py`
   - Added a configuration lookup helper that respects an explicitly present empty config value.
   - Direct environment fallback is used only for older config-like objects that do not contain that field.
   - Applied the same behavior to X, Telegram token, Telegram chat allowlist, and health state.
   - Added a regression test that simulates credentials inherited from process environment and asserts no external calls are made when the supplied config explicitly has empty credentials.

2. `config.py`
   - Loads `.env` from the project directory explicitly, regardless of the shell's current working directory.
   - Retains `override=False`, so deployment/shell environment variables take precedence over `.env` values.

3. `run_windows.bat`
   - Creates `.env` from `.env.example` only when `.env` is missing.
   - Opens the new file in Notepad and waits for the user to save/close it before continuing.
   - Runs `env_check.py` to display configuration status without printing secrets before smoke checks/tests.

4. `env_check.py`
   - New safe diagnostics command; reports whether provider keys/URLs are configured without revealing values.
   - Makes clear that `CONFIGURED` does not establish that a key is valid or the service is reachable.

5. `README.md` and `ZIP_PACKAGE_README.md`
   - Document local `.env` creation, configuration, restart requirement, and safe diagnostics.

## Validation

- `python -m compileall -q .`: PASS
- `python env_check.py`: PASS; values were not printed
- `python -m solana_app --smoke --provider mock`: PASS (`SMOKE OK: all components initialized.`)
- `pytest -q`: **83 passed, 1 skipped**
- Repeated the full test suite with a temporary project `.env` containing only a synthetic X token: **83 passed, 1 skipped**. The temporary `.env` was deleted afterward and is not included in the package.

## Safe setup

- The ZIP intentionally does not contain `.env` or real credentials.
- On first Windows launch, `run_windows.bat` copies `.env.example` to `.env` and opens it for editing. Add your real values to `.env`, save, and restart GROK6.
- Required names for the configured integrations include `X_API_BEARER_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`, `PUMPPORTAL_ENABLED`, `PUMPPORTAL_API_KEY` (optional for launch/migration discovery), `SOLANA_RPC_URL`, `RUGCHECK_API_KEY`, `JUPITER_API_KEY`, and `HELIUS_API_KEY` (optional wallet polling).
- Real API connectivity was not claimed or tested by these offline tests. Use the project's read-only provider checks and dashboard health after configuring credentials. Real trading remains disabled; no safety gates are bypassed.
