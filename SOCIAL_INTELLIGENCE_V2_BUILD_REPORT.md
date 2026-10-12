# GROK6 V7 — Social Intelligence V2 Build Report

## What changed
- Added an advisory social-evidence evaluator to `SocialTrendRadar.evaluate_candidate()`.
- Evaluator matches social posts to a candidate by exact mint string or ticker, filters to a six-hour observation window, and reports:
  - mention counts in the last 15 minutes and 15–60 minute comparison window;
  - number of distinct social sources;
  - transparent, capped evidence score;
  - exact-mint social-text match indicator;
  - matched evidence references and timestamps.
- Connected `Intelligence._persist_decision()` to save social evidence with the candidate decision payload.
- Captures a social-evidence snapshot in paper-position metadata at entry.
- Added read-only endpoints:
  - `GET /api/social-radar/evaluate?mint=<MINT>&symbol=<SYMBOL>`
  - `GET /api/social-radar/performance`
- Performance endpoint summarizes only paper positions that actually contain an entry-time social snapshot, including closed count, positive realized PnL count, win rate, and summed realized PnL.

## Safety boundaries
- Social score is explicitly `ADVISORY_ONLY`; it does not approve entries, override token-safety requirements, validate the mint on-chain, prove liquidity, prove a Jupiter sell route, or bypass entry-window checks.
- Real trading remains disabled.
- Social ticker/mint text matches are clues only, not authoritative token identity.
- The performance endpoint is descriptive and observational; it does not prove that social evidence caused a trade outcome.
- Historical positions without a social snapshot are excluded from the social-linked outcome sample.

## Validation
- `pytest -q`: **83 passed, 1 skipped** after the environment-ordering regression fix (details in `GROK6_ENV_AND_SOCIAL_TEST_FIX_REPORT.md`)
- `python -m compileall -q .`: **passed**
- `python -m solana_app --smoke --provider mock`: **passed** (`SMOKE OK: all components initialized.`)
- No real X or Telegram network requests were made as part of these tests; live-source credentials and access still require environment-specific validation.

## Operational follow-up
1. Configure X/Telegram credentials and channel allowlist in the deployment environment.
2. Check `/api/social-radar/health` for actual source polling status.
3. Use `/api/social-radar/evaluate` to inspect a candidate's evidence without authorizing a trade.
4. Collect a meaningful sample of paper positions before evaluating whether social scores correlate with outcomes.
5. Keep all safety gates and paper-only execution in place during the measurement period.
