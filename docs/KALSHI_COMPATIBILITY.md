# Kalshi KXBTC15M compatibility

## Diagnosis

PR #2 deployed `cfe84f6` and reports that 24 observations, forecasts and decisions survived a Railway redeployment. Its source report calls the paper execution guard. That guard reports `UNSUPPORTED_MARKET` when the series differs from KXBTC15M, when the face value differs from $1, or when `can_close_early` is true. The old health log omits face value and the early-close flag. [PR #2](https://github.com/noahb27/bitprit/pull/2)

The November 19 Windows recording of Kalshi ticker `KXBTC15M-26NOV191600-00` resolves the three inputs for a live contract in this series.

| Check | Recorded value |
| --- | --- |
| Series and market | `KXBTC15M` and `KXBTC15M-26NOV191600-00` |
| Face value | `1.0000` USD |
| Early close | `can_close_early=true` |
| Live status | `active`; the exact-ticker request was made after the order book and was 0.337 seconds old |
| Book | 100 levels per side; local request age 0.770 seconds |
| Target and source | $62,047.82 per BTC, CF Benchmarks |
| Fee metadata | `quadratic`, multiplier `1` |
| Published close | 4 p.m. Eastern, with 124.464 seconds remaining in the last sample |

The third condition alone reproduces the old rejection for that recorded market. The Windows probe on `51f3ce5` passed with empty source and paper policy failure lists. A row from the running Railway database is needed to confirm the deployed worker's exact instance of the same failure. The Windows observations establish the API behavior for this market.

## Execution and settlement

- The public series listing chooses markets for `KXBTC15M`. The recorder accepts binary markets and scopes the book request to the exact market ticker. It matches the event to the exact ticker and verifies the event's series on the fee request. Health output includes both tickers, the open and close, the face value, the strike comparison, and the fee event.
- The issuer's schema gives each market a lifecycle `status`, a `can_close_early` flag and an optional `early_close_condition`. For an early-close-capable market, the recorder makes a fresh `GET /markets/{ticker}` after receipt of each book. The quote guard requires active status, a request made after book receipt, and the same market, event, opening time and closing time. Missing, mismatched, stale or inactive confirmations block the simulated fill. Snapshots recorded before the correction lack that confirmation and remain ineligible for costed fills on early-close markets. [Kalshi SDK 3.36.0](https://pypi.org/project/kalshi-python-sync/3.36.0/) · [Get Market](https://docs.kalshi.com/api-reference/market/get-market)
- Trading uses the API's open and close times. An order must stay inside its simulated fill window and stop before the configured pre-close buffer, which defaults to 15 seconds. Checks apply to both legs and again at the fill. If the venue omits the exchange order-book timestamp, the local request-start and receipt times support the freshness check. The exchange-side book age and market changes between requests remain uncertain. The $5 entry ceiling applies to full-size fills including estimated fee and slippage. [Kalshi order books](https://docs.kalshi.com/getting_started/orderbook_responses)
- The official API defines `floor_strike`, `strike_type`, rule text, `finalized` status, the outcome and the settlement timestamp. Settlement uses the finalized Yes or No for the exact recorded ticker and event, with the payout checked against the $1 binary contract. An earlier close reported with a finalized result is accepted only for a matching opening time and an early-close-capable market. Coinbase remains a spot proxy. The official expiration value, when available, supports a basis diagnostic. The benchmark averaging interval and tie treatment for the recorded contract still require a read of its published terms or stored rules. [Contract terms URL returned by Kalshi](https://assets.kalshi.com/contract_terms/CRYPTO.pdf) · [Kalshi crypto market guidance](https://help.kalshi.com/en/articles/13823838-crypto-markets)
- The SDK identifies `quadratic` with the General Trading Fees Table and documents the series multiplier and event overrides. The recorder verifies the fee event against the market and applies an override when both override fields are present. The versioned simulation estimates each taker leg with `0.07 × multiplier × contracts × price × (1 − price)` rounded up to a cent per price level, plus slippage. Account-level rounding adjustments and rebates can make actual fees differ from the estimate. Current fee schedule details need direct verification before interpreting realized paper returns. [Kalshi fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)

The Codex proxy returned 403 for the live Kalshi API, the contract PDF, the help page, and the fee schedule. The current official Python SDK was accessible and supplies the API evidence above.

## Railway acceptance check

Use the existing worker's service shell to inspect one stored market and its fee metadata. This command opens SQLite read-only.

```sh
python -c 'import json,sqlite3; db=sqlite3.connect("file:/data/quarter-hour.sqlite3?mode=ro",uri=True); row=db.execute("SELECT payload FROM observations WHERE ticker IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone(); s=json.loads(row[0]); print(json.dumps({k:s[k] for k in ("at","market","fees")},indent=2))'
```

After a reviewed deployment to the existing worker, verify the same ticker and event, a fresh post-book status request when early close is allowed, and empty source and paper policy failure lists on an eligible sample. Near-close observations can pass feed readiness while correctly blocking paper execution. Check a subsequent rollover and match its finalized result to the captured window. Keep `WAIT / UNTRAINED` as the expected behavior until there is evidence for an eligible model. See the [cloud milestones](PLAN.md#cloud-milestones).
