# Supplied Windows recorder

The attached package is a standalone Node.js 24 application. The launcher runs `recorder.mjs run`, which polls Coinbase spot and closed minute candles and discovers Kalshi KXBTC15M markets. It reads both sides of the book as fixed-point bids, derives asks from opposite bids and stores samples about every five seconds. It writes source and local receipt times, market rules, features, a frozen first forecast per market, original plans, paper positions and events to `data/bitcoin.sqlite`. Its dashboard listens on the loopback interface. Closing the dashboard leaves the recorder running.

All 11 payload files matched the supplied manifest. All nine bundled Node tests passed on Linux. They cover SQLite restarts, degraded samples, original forecasts, the spend cap, settlement timing, simulated exits, gap handling and reports. They use synthetic fixtures.

## Reusing recordings

Quarter Hour can import a standalone backup of the supplied recorder into an empty research database. The importer opens the backup read-only, preserves original sample and legacy result rows, converts millisecond timestamps to seconds, and maps fixed-point book depth through the same parser the native collector uses. Keep the original backup for its candles, original forecast table, positions and events. No records have been migrated from a user database yet because the package contained code and configuration.

The older recorder timestamps book receipt and spot source time. It lacks book request timestamps, contemporaneous event fee overrides, and the original official result payload. Its verified fee flag means series data was available. It does not establish the fee regime for an event. Imported rows carry explicit flags, costed replay excludes them, and finalized results must be rechecked by exact ticker before use as labels. Its `diffusion-v1` estimates and close-based paper decisions stay in the original material for comparison. A retrospective result or the older model's score is never a feature.

The native collector extends the data contract with bounded public requests, timed event and series fees, raw response provenance and exact finalized result validation. The newer execution engine applies recorded latency on both legs. Use one collector for a given recording session. There is no browser-journal sync or automatic migration of open positions.
