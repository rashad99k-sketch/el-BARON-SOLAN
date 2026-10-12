# GROK6 V6 — Runtime output review

This review is grounded in the dashboard text supplied by the operator on 2026-10-11. It is not a claim that the same live environment was reproduced in the build container.

## What the displayed state means

- The web dashboard and scanner are running; it reports 185 persisted candidate records and a recent scan.
- The runtime has five connected/healthy sources, but the original UI counted Helius wallet monitoring in the health denominator even though Helius is optional. V6 excludes Helius from the core-source percentage while still showing its configuration state.
- `JUPITER_API_KEY` is not configured. GROK6's current safety policy requires a per-token sell-route result, so the route field remains unknown and paper entries must stay blocked. No gate was bypassed.
- Missing market liquidity and missing authority/route data were all rendered under broad `NEEDS_REFRESH` decisions. The original pipeline panel therefore mislabeled many safety-blocked candidates as market verification work. V6 separates first unresolved market work from safety-evidence work and excludes terminal rejects/expired/no-chase outcomes from active stages.
- The counts labelled “decisions · last hour” are recorded decision/reason occurrences across repeated scans, not unique token counts. A candidate may contribute more than once as the scanner rechecks it.
- Helius being `NOT_CONFIGURED` does not block paper entry by itself. Jupiter is a global blocker in the current implementation; individual candidates can still fail safety even after Jupiter is configured.

## V6 changes

1. Corrected pipeline stage classification using the stored safety `missing_fields`, current market liquidity, decision state, and actual open positions.
2. Removed terminal rejection outcomes from active stage counts.
3. Removed optional wallet polling from application health state evaluation and its percentage denominator.
4. Added an explicit `PAPER ENTRY BLOCKED` banner for missing Jupiter configuration. The banner says to configure the key in local environment settings, and reiterates that every token must still pass route and critical safety checks.
5. Kept live execution hard-disabled and did not alter the trade-entry, risk, or paper position-management logic.

## Verification in the build container

- `python -m compileall -q .`: PASS
- `node --check static/js/command_center.js`: PASS
- `python -m pytest -q`: 54 passed
- `python -m solana_app --smoke --provider mock`: PASS
- Flask HTTP/browser runtime and live provider queries: NOT RUN in this container (Flask is not installed here and live network/API access was not verified).

## Required operator action

Configure a valid `JUPITER_API_KEY` in a local `.env`/environment, restart the app, and observe fresh per-token results. Do not paste the secret into chat or commit it to the repository. Helius can remain unconfigured if wallet tracking is not needed. A working API key is necessary for this gate in the current implementation but is not sufficient to approve a token.
