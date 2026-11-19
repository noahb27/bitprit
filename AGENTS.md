# Project: Quarter Hour research ecosystem

## Purpose

Build a local research and paper-trading ecosystem for Kalshi's 15-minute Bitcoin Up/Down markets. The user's intended opportunity includes buying a cheap Up or Down contract before a recovery in its resale price, then selling before trading closes. A contract need not ultimately win for a pre-close resale to be profitable.

Estimate and test a possible edge. Never describe software tests, historical fitting, high directional accuracy, or a high potential payout as proof of profitability.

## User constraints and scope

- Windows-first, single-computer operation. The collector must run independently of an open browser and persist data across process restarts.
- Hard maximum entry spend: $5 per trade, including estimated entry fees and a configurable slippage allowance. Size whole contracts to fit. If one contract cannot fit, choose WAIT. Users may lower the cap; model output cannot raise it.
- One open paper position at a time initially. Spending is a cap, not a requirement to use the full amount.
- Paper trading and public-data recording only in this implementation. No real orders, account funding, order-capable credentials, or automatically enabled live execution. Real-money execution is a separate future scope requiring explicit user instruction.
- No paid data or paid LLM API dependency without explicit user direction. An agent developing this project is separate from the numerical models running it.
- Keep the hosted app and the local ecosystem distinct. Do not change or publish an existing site unless the user requests it.

## Begin by inspecting

Read the actual repository, nested instructions, tests, documentation, and any user-provided recorder or dataset before proposing changes. Preserve existing working behavior and user edits. Do not assume code described in an earlier chat is present in this repository.

If a local recorder package is supplied, inspect its engine, database schema, sampling, timestamps, cost assumptions, and tests. Reuse proven components where appropriate; document any migration and preserve existing records. Do not pretend that browser-local history and a separate SQLite database are already synchronized.

Prefer the established stack. For a new repository, explain the chosen stack briefly and keep collection, storage, numerical learning, and serving simple. A single computer with modular workers and SQLite is sufficient initially; avoid unnecessary distributed infrastructure.

## Required components

1. Collector: obtain Bitcoin spot observations, Kalshi contract metadata, executable bid/ask depth, source timestamps, receipt timestamps, rules, fee metadata, and official finalized results. Save both usable and unusable observations, with quality flags and WAIT reasons.
2. Shared storage: durable, versioned records of markets, observations, features, immutable predictions, finalized labels, model artifacts, decisions, paper fills, costs, and data-quality events. Support migrations, exports, and consistent backups.
3. Settlement learner: estimate a calibrated probability of the market's official Yes/Up outcome using information available at prediction time.
4. Resale learner: estimate the distribution of executable exit value and net return for buying either side now and selling at fixed future horizons such as 30, 60, and 120 seconds. Optionally model target-before-stop probability with an explicit observation-order and timeout definition.
5. Evaluator: produce chronological offline evaluations and forward paper results with realistic execution, simple baselines, uncertainty, and coverage reporting. Own the model-promotion criteria.
6. Predictor and decision worker: consume a frozen, versioned model and current observations. Compare BUY YES, BUY NO, HOLD, SELL, and WAIT. Reevaluate open positions as new observations arrive. Log the inputs, model version, uncertainty, costs, and reasons for each decision.
7. Independent policy layer: enforce the $5 cap, full-size liquidity checks, quote freshness, position limits, market identity, trading deadlines, and paper-only operation regardless of model output.
8. Local dashboard/CLI: start, stop, inspect health, replay recorded curves, inspect individual decisions and exits, export data, and back up storage. Collection must continue with the browser closed.

These are worker responsibilities, not a requirement to run multiple chatbots or call an LLM for every quote.

## Prediction and learning rules

- Distinguish Bitcoin spot direction, final contract outcome, and pre-close contract resale value. Define targets and horizons explicitly.
- Evaluate both sides symmetrically. Cheap Up can be a candidate during a decline, and cheap Down can be a candidate during a rise. Low price alone never establishes an edge.
- Features may include trailing returns at several horizons, volatility, distance from the official target, time remaining, spread, depth/imbalance, and recent contract-price changes. Every feature must be reconstructible from observations available at that decision time.
- Future observed quotes and finalized settlement outcomes are labels, never inputs to earlier predictions. Preserve original predictions when grading them.
- Start with transparent baselines. Compare a simple statistical model with a suitable tabular model before adding more complex sequence models.
- Do not manufacture a trained model when observed data is absent. Implement runnable training/evaluation paths, use clearly labeled synthetic fixtures only in tests, and display UNTRAINED or insufficient evidence honestly.
- Retraining creates a challenger artifact. It must not silently overwrite the active model. Store training period, split boundaries, features, settings, metrics, and provenance with each artifact.
- Promotion criteria must be written before inspecting the final test period. Reserve an untouched later period for final evaluation and gather fresh forward evidence after selection. Repeated experimentation must not turn the final test set into a tuning set.

## Market-data correctness

- Verify current contract rules and API schemas using official primary documentation. Identify the exact series, event, and market ticker; do not grade one window with another window's result.
- Respect the contract's settlement benchmark and averaging rules. Exchange spot is a proxy; disclose and measure basis error where data permits.
- Use the actual market open and close times. A rebound after trading closes cannot rescue the contract.
- Use the owned side's executable bids for selling. Derive asks from opposite bids only according to the documented binary orderbook relationship.
- Preserve the original source/receipt timestamps of cached responses. A fresh dashboard render cannot make an old quote fresh.
- Validate prices, quantities, timestamps, spreads, and candle completeness. Record gaps, rate limits, errors, restarts, and unavailable data explicitly.
- Use bounded requests, caching, backoff, and graceful shutdown. Choose sampling frequency for the hypothesis; a five-second recorder cannot demonstrate captures of opportunities that occur entirely between its observations.

## Execution and cost model

- Buy from observable asks and sell into observable bids, walking depth for the requested quantity. Include the applicable fees on both legs, rounding rules, spread, slippage, and execution delay. Version and disclose approximations to the venue's actual fills and fees.
- Define partial-fill behavior before evaluating. Rejecting unavailable full-size fills is acceptable for the first version; pretending a full fill occurred is not.
- Process observations in chronological order. Profit/stop exits use the first eligible observed executable quote after the trigger and simulated latency, not a retrospectively chosen peak or the exact stop price if the quote jumped past it.
- Separate settlement P&L, fixed-horizon counterfactual returns, and the actual paper strategy's entry/exit P&L in reporting.
- If collection stops, record a gap. Do not reconstruct missed fills from a later candle's high/low. Expired positions without observed exits await official settlement and are labeled as carried to settlement.
- Keep paper positions and their original settings across restarts. Prevent duplicate collectors and duplicate automatic entries caused by restarts.
- Give timing and uncertainty buffers different names. Display the arithmetic and the actual failed checks. WAIT must distinguish stale/missing data, insufficient time, cost/price conditions, liquidity, and an untrained model.

## Testing and evaluation

- Use chronological train/validation/test partitions with entire markets kept together. Group at the day/session level where appropriate and purge boundary overlap for forward labels. Fit preprocessing and calibration only on the training/appropriate validation data.
- Do not randomly scatter samples from one window into training and testing. Thousands of related ticks are not thousands of independent trials.
- Compare settlement-only, resale-only, and combined decisions on the same held-out periods. Include no-trade and simple fixed-rule baselines.
- Report net P&L after costs, average gains/losses, drawdown, losing streaks, turnover, trade count, exposure, fill coverage, and results by period, side, price band, and volatility. Also report probability calibration/Brier or log loss and uncertainty for forecast outputs.
- Stress test worse fees/slippage, delayed execution, missing ticks, thin books, one-sided books, stale metadata, restarts, and volatile periods. Report concentration of profits in a few trades and uncertainty using sensible market/day grouping.
- Use meaningful unit and integration tests for immutable predictions, market/result matching, time boundaries, no future-data leakage, chronological splits, cost arithmetic, budget sizing, gaps, persistence, replay, and model versioning.
- A passing unit suite establishes implementation checks, not trading profitability. Passing historical evaluation is not a guarantee of future results.

## Implementation workflow

Work in coherent milestones:
1. Inspect and document the existing state, requirements, interfaces, and acceptance checks.
2. Make collection, storage, restart behavior, data-quality reporting, and exports reliable.
3. Implement a deterministic replay engine and baseline evaluations before selecting complex models.
4. Add the settlement and resale training/evaluation pipelines with frozen artifacts and honest insufficient-data behavior.
5. Integrate decision/policy workers, forward paper execution, the local interface, and Windows packaging.

Implement authorized work without repeated confirmations for routine reversible choices. Ask only when a material missing requirement cannot be inferred. If an observed-data requirement prevents empirical training or promotion, complete the supporting code and state the precise evidence still missing.

Keep progress, decisions, limitations, and commands understandable. At each completed milestone, document what changed, what was tested, and what remains. Commit coherent changes and use a reviewable branch/PR when repository access supports it; do not merge or publish unrelated work.

## Repository hygiene and deliverables

- Never commit API keys, credentials, real account details, databases, raw recordings, large model binaries, or user exports. Use appropriate ignore rules and a documented local data directory.
- Keep tests and synthetic fixtures separate from real recordings. Never ship fixtures as recorded performance.
- Supply a concise README, configuration example, architecture/data-contract documentation, test commands, run commands, and Windows setup/backup instructions.
- Keep numerical training and evaluation reproducible. Record versions, random seeds where relevant, and dataset/time-range identifiers.
- Do not claim work was run on Windows, continuously deployed, empirically trained, or profitably validated unless those actions were actually verified.
- Clearly distinguish implemented and tested software from pending data collection and unproven financial performance.

## Initial acceptance checklist

- One documented command starts local collection and paper prediction; browser closure does not stop recording.
- Observations, immutable forecasts, results, decisions, positions, and errors survive restart.
- Both prediction objectives have explicit targets, reproducible pipeline commands, and appropriate baselines.
- Replay enforces depth, costs, latency, timing, exact market identity, and a maximum $5 entry.
- Tests demonstrate that the policy layer blocks oversized, stale, invalid, and unsupported decisions.
- A replay/report identifies assumptions and missing data and does not select future peaks as fills.
- The interface explains why a decision is WAIT or SELL and shows observed rather than invented performance.
- The system remains paper-only and reports model/evidence status truthfully.
