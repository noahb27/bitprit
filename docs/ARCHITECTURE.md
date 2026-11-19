# Data contract

Quarter Hour is a single-machine Python process with a read/write collector and a local CLI. SQLite uses WAL with full synchronous writes and numbered migrations. A separate SQLite lease prevents duplicate workers on one database and releases automatically on process termination. Terminal shutdown, Windows sleep, or loss of connectivity leaves an explicit discontinuity. Raw errors, current source ages and WAIT reasons are inspectable from the CLI.

| Records | Contract |
| --- | --- |
| \`markets\` | Exact KXBTC15M market and event tickers, official open and close, first receipt and last result check |
| \`raw_responses\` | Public route, request/receipt times, transport status, response data and HTTP age/date when returned |
| \`observations\` | Ordered snapshot with exact identity, rule text and strike, both-side bids and timestamp provenance, spot, fee metadata, flags and gap marker |
| \`results\` | Finalized binary official outcome matched to the recorded market and event, receipt and settlement times, and reported expiration value when usable |
| \`features\` / \`forecasts\` | Per-observation feature version, frozen artifact reference, outputs, input facts and model readiness |
| \`artifacts\` / \`evaluations\` | Immutable model, settings, SDK-independent inference payload, dataset digests and sealed holdout grade |
| \`decisions\` / \`fills\` / \`trades\` | Immutable strategy explanations, timed intents and simulated executions with fee, spread, slippage and dollars across both legs |
| \`worker\` / \`paper_state\` / \`quality\` | Worker lifecycle, single persisted open position or pending entry, and source/continuity events |

All times are UTC epoch seconds internally. Order and cash arithmetic uses decimal dollars, and exports represent these amounts as strings. The collector keeps local request and receipt times, preserves cache receipt times and records exchange timestamps when the provider supplies them. It rejects a nonzero HTTP cache age. A public book without an exchange timestamp is timed from local request start. This bounds local data age under a fresh response assumption. The upstream book's true age remains unknown. Forecast features require observable trailing spot history and exclude partial history after gaps. The learner does not use retrospective candles.

Kalshi's public API schema and complementary bid/ask rule were checked against its official synchronous Python SDK 3.36.0. Metadata pins the exact series and event and applies the current event override, when present, on top of series fees. The recorder retains the terms URL, rule text and declared settlement sources. The live contract terms, fee schedule and feed responses still need local verification. Determine final outcomes from the finalized market record. Consult the current sources for benchmark and averaging rules. [Kalshi SDK](https://pypi.org/project/kalshi-python-sync/3.36.0/) · [Kalshi order books](https://docs.kalshi.com/getting_started/orderbook_responses) · [Kalshi crypto terms guidance](https://help.kalshi.com/en/articles/13823838-crypto-markets) · [Kalshi fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf) · [Coinbase Exchange ticker](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-ticker)

## Simulation

A Yes ask at \`1 - No bid\` consumes the same depth as the opposing bid. Sells consume the owned side's bids. Both legs walk levels. Every fill is full size in whole contracts, even when fractional depth contributes across multiple price levels. The versioned taker estimate is \`ceil_to_cent(0.07 × schedule_multiplier × size × price × (1 − price))\` per swept price level, plus configurable slippage. Maker mechanics, account-level rounding credits and books between polls are outside the approximation. An unfamiliar fee regime, fee waiver, invalid book or uncertain market identity blocks the affected fill.

A price or time trigger creates an intent. Execution waits for the first eligible new observation after latency, checks the exact market, and expires on the fill deadline or pre-close buffer. Entry sizing and costs are checked again at execution. The entry cannot cost more than the signal quote or the current cap. A failed stop can leave the paper position open, and the price of a later executable sale can be worse than the trigger. Risk exits reevaluate realized P&L against the current owned bids. Gaps cancel outstanding intents, and post-close positions await official settlement. The settlement-only research policy holds by design. Every mode and stress case shares the cap and fill logic.

The process uses public GET requests and local simulation. The supplied Node recorder and the project's research workers can use separate databases. The import opens a standalone Node backup read-only and preserves legacy provenance. Exports, backups and user settings are ignored by Git. Training and policy comparisons stream depth from SQLite. For long research jobs, use a backup copy so live collection can checkpoint its WAL. Use a bounded date range when saving every replay decision to a trace.
