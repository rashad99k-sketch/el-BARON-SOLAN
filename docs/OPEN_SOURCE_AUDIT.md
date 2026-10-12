# Open-source research and audit status

**Status: preliminary web research only; source audit is not complete.** Network access from this implementation container was unavailable, so external repositories have not been cloned, built, or tested here. No external repository code was copied into GROK6 as part of this integration patch. Do not interpret a repo description, stars, or a repository search result as proof of production readiness.

## Repositories queued for source-level review

| Project | URL | Research value | Before reuse |
|---|---|---|---|
| Sol Token Scanner | https://github.com/joseph-alexan/sol-token-scanner | Token discovery, filter pipeline, risk signals, market-data adapter ideas | Clone in isolated environment; inspect license file, source, package/dependency pins, tests, active issues, and data provenance. |
| PumpFun Watcher | https://github.com/LouisdeMagician/pumpfun-watcher | Pump.fun launch monitoring patterns | Confirm it still follows current Pump.fun launch/migration behavior; inspect reconnect, rate handling, and actual event decoding. |
| Pumpswap Watcher | https://github.com/LouisdeMagician/pumpswap-watcher | PumpSwap migration/pool tracking patterns | Validate pool parsing, source coverage, dependencies, tests, and license before integration. |
| Solana Wallet Tracker | https://github.com/BankkRoll/solana-wallet-tracker | Wallet-activity indexing and Helius-oriented tracking examples | Verify LICENSE and whether the current endpoint/provider plan supports the required history. Do not claim performance or wallet profitability without a full ledger. |
| Pump.fun Launch Watcher | https://github.com/vannamsolana/pumpfun-launch-watcher | Creator-history and early-launch monitoring patterns | Search metadata describes a live CLI and JSONL output; this implementation did not clone or validate the code. Verify its license and source before any reuse. |
| Trigger.dev | https://github.com/triggerdotdev/trigger.dev | Durable background jobs/workflows if GROK6 outgrows its local scheduler | Likely a substantial new infrastructure dependency; adopt only for a concrete retry/scheduling/observability need and inspect its license/deployment requirements. |
| Solana developer tools | https://github.com/solana-developers/solana-tools | Official/community examples and developer utilities | Inspect each subproject separately; a parent organization/repo does not confer a blanket license on all referenced projects. |

## Audit checklist for the implementation agent

1. Clone a candidate repo into an isolated temporary directory; pin the commit SHA.
2. Read `LICENSE` and `NOTICE` files before copying or adapting code. If the license is missing or unclear, do not vendor code until clarified.
3. Inspect source rather than README only. Search for signing, secret exposure, transaction sending, obfuscated payloads, telemetry, and hard-coded endpoints/keys.
4. Review lockfiles/manifests, dependency health, install hooks, CI, test coverage, recent commits, releases, and open security issues.
5. Run tests in a container with no secrets and no funded wallet. Avoid running arbitrary post-install scripts blindly.
6. Write a provenance record for every adapted file: upstream URL, commit SHA, license, changed lines/modules, and local tests.
7. Do not import an entire trading bot or live execution stack when a small read-only adapter solves the need.

## Important design conclusion

GROK6 should use small provider adapters and tests rather than wholesale copying of other bots. Current integration code is intentionally limited to read-only discovery/evidence and simulated trade management; it does not use external signing or swap execution code.
