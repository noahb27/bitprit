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
- [ ] Verify the paper worker and CLI on fixtures and with an empty-database smoke run.
- [ ] Collect timestamped books for both sides, spot observations, matched finalized results and applicable fee regimes across separate chronological sessions. Retain pre-close book coverage through each forecast horizon and track missing samples.
- [ ] Predeclare a later holdout and collect fresh forward paper evidence. Establish market/day coverage and uncertainty before drawing empirical conclusions.
- [ ] Exercise the Windows launcher and backup restore on a Windows machine.
