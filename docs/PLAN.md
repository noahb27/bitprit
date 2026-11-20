# Quarter Hour implementation plan

The checkout initially contains a README and the project specification in AGENTS.md. There is no application, recorder, CI configuration, or captured dataset. The attachment supplies a build brief. This project uses Python 3.12 and SQLite for a single-machine Windows workflow. Collection and paper execution run in a terminal process and the local CLI reads the same database. The numerical learner is an optional install. It exports versioned JSON models for the lightweight runtime.

| Milestone | Scope | Acceptance |
| --- | --- | --- |
| 1. Assessment | Inventory the checkout and define interfaces and evidence requirements | Plan is versioned and identifies missing inputs and unresolved source verification |
| 2. Recording | Public spot and market adapters, source and receipt times, durable SQLite, migrations, exports, backups and worker lifecycle | A fixture-driven collection run records depth, errors, official results and restart gaps. Export and restore checks preserve records |
| 3. Replay | Decimal order-book execution, one position, $5 hard ceiling, time and freshness limits, settlement and simple deterministic baselines | Chronological replay uses the first eligible book after simulated latency, honors size and exact market identity, records failed fills and reports coverage and net results |
| 4. Learning | Point-in-time features, settlement probabilities and side-specific resale distributions at 30, 60 and 120 seconds | Whole-market chronological splits and leakage tests, statistical and tabular candidates, validation-only calibration and selection, immutable challengers and explicit UNTRAINED output |
| 5. Paper workflow | Connect the frozen predictor, decision worker, local CLI and Windows launcher | One command starts collection and paper decisions. Forecasts, order intents, fills and health survive restarts. The CLI explains waits and exits and supports review, export and backup |

## Interfaces

- A collector writes public market definitions and snapshots keyed by the exact Kalshi ticker. It stores source provenance and receipt times even when data is invalid.
- Replay and the paper worker use the same execution and position rules. The learner reads chronological snapshots and official finalized results. The evaluator reads frozen forecasts and computes outcomes separately.
- The CLI starts the worker and reads the local store for health, decisions and market history. Exports and consistent SQLite snapshots stay outside version control.

## Progress and evidence backlog

- [x] Inspected the repository, build brief and shared workspace. No recorder or dataset was supplied.
- [ ] Verify the current contract terms, fee schedule and live responses. The official Kalshi Python SDK 3.36.0 documents the public order-book and market schemas, event fee overrides and finalized status. Cloud requests to the providers and their documentation received proxy HTTP 403.
- [x] Inspected the supplied Node recorder, verified its manifest and passed its nine bundled fixture tests. Added a read-only compatibility importer. See [recorder assessment](RECORDER.md).
- [x] Verified recording fixtures for depth, cache timestamps, invalid input, HTTP backoff, duplicate-worker prevention, restart gaps, finalized results, export and SQLite restore.
- [x] Verified replay fixtures for exact budget and fee arithmetic, full-size depth, current quote matching, latency on both legs, stop jumps, first eligible fills, gaps and expiry to official settlement.
- [x] Verified point-in-time features, horizon labels, settlement availability, chronological stages, frozen challengers, sealed holdouts and UNTRAINED on empty data using synthetic fixtures.
- [x] Verified frozen forecasts and decisions, idempotent writes, paper position recovery, exact official settlement, gated activation and the CLI backup and export paths with fixtures.
- [x] Ran the Python CLI with standard-library-only Python. The worker recorded source failures and WAIT / UNTRAINED. An empty database produced no artifact. The live-source probe correctly returned exit code 2 for cloud proxy denials.
- [x] Confirmed Python 3.12 Windows x64 wheels are available for all pinned learner dependencies.
- [x] The Windows probe captured 12 live books for KXBTC15M-26NOV191600-00 and reported matching spot and fee metadata. Split feed readiness from paper quote policy and added exact-ticker status checks for early-close markets. The updated probe and official finalization still need a local run.
- [ ] Collect timestamped books for both sides, spot observations, matched finalized results and applicable fee regimes across separate chronological sessions. Retain pre-close book coverage through each forecast horizon and track missing samples.
- [ ] Predeclare a later holdout and collect fresh forward paper evidence. Establish market/day coverage and uncertainty before drawing empirical conclusions.
- [ ] Exercise the Windows launcher and backup restore on a Windows machine.

## Cloud milestones

The baseline is PR #2 at `cfe84f6`. It adds the single Railway paper worker, persistent storage at `/data`, and periodic health logs. The PR records successful live collection and persistence across redeployment. Start with the [compatibility investigation](KALSHI_COMPATIBILITY.md).

| Milestone | Work and acceptance evidence |
| --- | --- |
| 1. Contract compatibility and live-data validation | The candidate separates feed health from paper eligibility and requires an exact-ticker activity check for early-close markets. The November 19 Windows probe passed on the same guard. Confirm the recorded Railway row, the direct contract and fee documents, and the candidate's health fields on the existing service during a reviewed deployment. |
| 2. Full market lifecycle and official results | The collector stores quotes and has exact-ticker result lookup with historical fallback. Follow a full window from the first active book through close, rollover and official finalization. Verify matching event, result, settlement receipt, payout and cutoff on an exported sample. Distinguish partial windows, unresolved results and revised close times. |
| 3. Collection quality, storage, exports and backups | Measure valid two-sided coverage, source ages, outages and growth of the SQLite database plus WAL over actual recording time. Use measured bytes per day to plan around the 500 MB volume. Restore a consistent off-volume backup locally and compare observation, result and decision counts with its export. Keep enough verified history locally for chronological splits. |
| 4. Chronological baseline and model evaluation | Fit on a local backup and keep entire markets together in training, calibration, selection and an untouched later holdout. Compare market midpoint, unchanged bid, fixed rule and no-trade baselines with the settlement and resale challengers. Grade only with the required market/day counts, both settlement outcomes, and the required executable label coverage per horizon. Preserve immutable artifacts and all evidence gaps. [Evaluation protocol](EVALUATION.md) |
| 5. Forward paper trials | Start a frozen challenger trial only after it passes the offline gates. Exercise fees, full depth, slippage, latency, stops and official settlement fallback in one paper journal. Check post-cost results and uncertainty on fresh days before any explicit promotion. The existing minimum is 20 closed trial trades across five UTC days. |
| 6. Dashboard and explanations | Build a read-only view of recordings, quote ages, model version, forecasts, simulated fills and specific WAIT reasons. Verify it explains stale data, unsupported contracts, insufficient time, price/fee/liquidity checks and the untrained state. Read data from the existing recorder or a local copy so viewing runs independently from collection. |
