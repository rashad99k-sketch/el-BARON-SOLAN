# Developer community and learning resources

**Status:** public resource links and search routes were collected. No private Discord content was accessed, and no credentials were obtained from community posts. Any undocumented code snippet should be cross-checked against current official docs and tested locally.

## Official or first-party starting points

- Solana developer portal: https://solana.com/developers
- Solana RPC overview: https://solana.com/docs/rpc
- Solana `logsSubscribe` WebSocket documentation: https://solana.com/docs/rpc/websocket/logssubscribe
- Official Solana Foundation Discord server listing: https://discord.com/servers/solana-org-1190280995971272736
- Helius WebSockets and Webhooks: https://www.helius.dev/solana-webhooks-websockets
- Helius Enhanced Transactions: https://www.helius.dev/docs/enhanced-transactions/overview
- PumpPortal real-time data and fees: https://pumpportal.fun/data-api/real-time/ and https://pumpportal.fun/fees/
- Jupiter Swap API V2: https://dev.jup.ag/docs/swap
- DexScreener API reference: https://docs.dexscreener.com/api/reference
- RugCheck API schema: https://api.rugcheck.xyz/swagger/index.html

## YouTube learning links

- [Solana RPC WebSocket API — Solandy](https://www.youtube.com/watch?v=nq4z1gMI64M): video description/chapter list covers WebSockets, `accountSubscribe`, `signatureSubscribe`, `logsSubscribe`, and other subscription methods. Published in 2024; re-check current APIs before coding.
- [How to Create WebSocket Subscriptions to Solana — QuickNode](https://www.youtube.com/watch?v=5NwFUXelPkQ): introductory tutorial for on-chain WebSocket subscriptions, published in 2024; use it as background and verify behavior against current Solana RPC docs.
- [YouTube search: PumpPortal subscribeNewToken API](https://www.youtube.com/results?search_query=PumpPortal+subscribeNewToken+API)
- [YouTube search: Helius wallet tracker Solana](https://www.youtube.com/results?search_query=Helius+wallet+tracker+Solana)
- [YouTube search: Solana getAccountInfo mint authority Token-2022](https://www.youtube.com/results?search_query=Solana+getAccountInfo+mint+authority+Token-2022)

## Search workflow to use when maintaining GROK6

1. Search exact provider endpoint/method name plus `official docs`, `breaking change`, `rate limit`, or `migration`.
2. Search GitHub code for exact API method and response field, not just the product name. Filter to recently updated repos and inspect the raw source/manifest/license.
3. Check GitHub issues/PRs for API breakage, reconnect bugs, endpoint deprecations, key leakage, and rate-limit complaints.
4. In Discord, ask a narrowly scoped technical question and request documentation/examples; never ask for private keys, third-party API credentials, or private-wallet data.
5. On YouTube, use tutorials to understand the workflow, but use official docs to decide exact parameters, authentication, quotas, and security behavior.
6. Store the URL, access date, exact claim, independent confirmation, and integration test in this document or a linked audit record.

## Community evidence policy

- A tutorial timestamp is not a source of truth for current API schema.
- A code sample from a gist/Discord/YouTube must be reviewed for error handling and secret leakage.
- Do not scrape private channels or reuse credentials found in a public paste.
- User-facing wallet analytics remain unknown unless the system has reliable and sufficiently complete evidence for them.
