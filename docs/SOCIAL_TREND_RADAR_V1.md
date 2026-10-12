# Social Trend Radar V1 — implementation and limits

## What was added
- Read-only polling module `social_radar.py` for X recent search API and Telegram Bot API `getUpdates`.
- Deduplication, bounded in-memory cache, source attribution, timestamps, post links where available, basic `$TICKER` and Solana-like address extraction, recency-weighted mention ranking.
- Dashboard endpoints: `/api/social-radar` and `/api/social-radar/health`.
- Social Trends dashboard tab with provider status, ranked mentions and latest source evidence.
- Health integration. Social providers are optional and do not make core health unhealthy merely because credentials are absent.
- Credentials are configured as environment secrets and are redacted from `/api/settings`.
- Paper-only invariant retained; social signals never authorize trades or bypass safety checks.

## Configure on Render
Set `X_API_BEARER_TOKEN` to an X API bearer token with access to recent search.
Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_IDS` to the bot token and comma-separated allowed chat IDs/usernames.
The bot can only read messages Telegram delivers to it. Add it to the selected channel/group with the appropriate permissions. Telegram Bot API `getUpdates` cannot be used simultaneously with a webhook for the same bot; resolve webhook/polling conflicts before enabling.
Do not put real tokens in `.env.example`, source code, screenshots, or Git.

## Important limitations
- X API access, quotas and search availability depend on the developer account's current plan and permissions.
- Telegram updates are not a universal public-channel firehose. This integration only sees updates delivered to the bot and only selected chats are accepted when an allowlist is set.
- Current trend ranking is a simple recency-weighted mention count, not an ML classifier, bot/spam detector, sentiment model, or validated alpha signal.
- This first version stores social messages in memory; a process restart clears its cache and historical ranking. Telegram offset is also in memory.
- The module extracts contract-like strings and cashtags, but does not resolve a ticker to a verified token contract. Never trade from a ticker mention alone.
- No live provider calls were made during offline tests. Credentials and external connectivity must be configured and verified in deployment.
- Real trading remains disabled. Social trend evidence cannot bypass liquidity, mint/freeze authority, sell route, risk, or entry-window checks.
