# Quarter Hour

For the optional paper-only Railway worker, see [cloud deployment](docs/CLOUD.md).

Quarter Hour records public Kalshi 15-minute Bitcoin markets and Coinbase BTC–USD on one computer. It estimates official Yes settlement and executable pre-close resale for both Yes and No as separate research targets. The worker runs independently of a browser. All execution is paper-only with at most one open position and a hard $5 entry ceiling including estimated entry fees and slippage.

The supplied Node recorder can continue collecting into its own SQLite database. Quarter Hour reads its standalone backups through a compatibility importer. See [the recorder assessment](docs/RECORDER.md) for which old fields support research.

## Start on Windows

Install 64-bit Python 3.12 with the Windows Python launcher. In PowerShell, open the repository folder and run the following.

```powershell
py -3.12 -m venv .venv
$Py = ".\.venv\Scripts\python.exe"
& $Py -m quarter_hour run
```

You can also launch `START_RESEARCH_WINDOWS.cmd`. The terminal must stay open and Windows must stay awake and online. Stop with Ctrl+C or use `& $Py -m quarter_hour stop` in a second terminal. By default the worker records observations, forecasts and decisions in `data/quarter-hour.sqlite3`. With no activated model it records `WAIT / UNTRAINED`.

To customize settings, copy `config.example.toml` to `local.toml`. You can lower `entry_cap`. Values above $5 are rejected. Open paper positions retain their original settings after a restart. Run from the repository root so that the default data and configuration paths are consistent.

## Check the public feeds locally

A Windows run recorded the live market, both bid ladders, the spot price and fee metadata. The cloud proxy returned HTTP 403 for both live feeds and the Kalshi rules pages. Run this one-minute check on your Windows computer in a separate database.

```powershell
$Py = ".\.venv\Scripts\python.exe"
& $Py -m quarter_hour --db data\connectivity.sqlite3 run --collect-only --cycles 12 --require-sources
& $Py -m quarter_hour --db data\connectivity.sqlite3 status
```

A successful probe shows `ready_at_last_snapshot: true`, the exact KXBTC15M ticker, the published strike, counts for both bid ladders, the fee multiplier and source ages. Exit code 2 means the last cycle lacked usable data. Inspect `failures`, `data_flags` and `recent_quality`. The `paper_policy_failures` field lists additional checks that can block simulated execution, even while the public feeds are available. The market's status, face value and early-close flag appear alongside the ages. Run long enough to cross one market close and allow time for official finalization to verify result recording separately. `refresh-results --checks 12` rechecks closed tickers while the worker is stopped.

Compare the captured terms URL and rules with current Kalshi documentation before relying on the paper assumptions. Coinbase last trades are a spot proxy for the settlement benchmark. REST order books may lack an exchange timestamp. The worker records local request start and receipt and exposes the time basis it used for the age check.

## Review, export and back up

Run these commands in a second PowerShell window from the same folder.

```powershell
$Py = ".\.venv\Scripts\python.exe"
& $Py -m quarter_hour status
& $Py -m quarter_hour decisions --limit 5
& $Py -m quarter_hour trades --limit 10
& $Py -m quarter_hour report
& $Py -m quarter_hour export exports\research.jsonl
& $Py -m quarter_hour backup exports\quarter-hour-backup.sqlite3
& $Py -m quarter_hour --db exports\quarter-hour-backup.sqlite3 status
```

Use `history TICKER --csv exports/curve.csv` with a ticker from `status` to export the recorded bid, ask, spot and forecast curves. Decisions list the input ages, liquidity, timing, costs, uncertainty deductions and rejection reasons. Trades itemize gross, fee, slippage and simulated net proceeds. `report` grades original settlement forecasts and reports realized paper P&L separately. Keep backups on a local NTFS volume and choose a new destination each time. Prefer the backup command during collection so committed WAL data is included.

If you already have a recording from the supplied Node package, run `node recorder.mjs backup` in its folder. From this repository, import that standalone backup to a fresh database.

```powershell
& $Py -m quarter_hour --db data\imported-recorder.sqlite3 import-recorder "C:\path\to\bitcoin-backup.sqlite"
& $Py -m quarter_hour --db data\imported-recorder.sqlite3 refresh-results --checks 50
```

Keep the original backup for its journal, candles and paper positions. Imported samples need exact-ticker result rechecks. Their legacy fee and timing provenance leaves them ineligible for costed resale labels.

## Train and evaluate

Install the pinned learner dependencies only when you are ready to fit models. The recording worker and frozen inference use Python's standard library.

```powershell
& $Py -m pip install -r requirements-research.txt
& $Py -m unittest discover -v
& $Py -m quarter_hour replay --stress --trace exports\replay.json
```

Once you have observations, set the five variables below to the real UTC dates you declared for research. Use earlier full markets for training, calibration and model selection. Keep the final period sealed until evaluation.

```powershell
$TrainingFrom    = "2025-01-01"
$CalibrationFrom = "2025-01-08"
$SelectionFrom   = "2025-01-15"
$HoldoutFrom     = "2025-01-22"
$HoldoutUntil    = "2025-01-29"
& $Py -m quarter_hour train --training-from $TrainingFrom --calibration-from $CalibrationFrom --selection-from $SelectionFrom --holdout-from $HoldoutFrom --holdout-until $HoldoutUntil
```

`train` reports missing coverage or prints an immutable artifact ID. For a complete challenger, use `evaluate ARTIFACT_ID` after the declared holdout ends. Evaluation grades the settlement and resale objectives and compares settlement-only, resale-only and combined policies with no-trade and a fixed rule. If a challenger passes the sealed holdout gates, use `run --trial ARTIFACT_ID` to collect fresh paper evidence under that artifact's exact settings. Inspect it with `report --model ARTIFACT_ID --trial-only`. `promote ARTIFACT_ID` requires both the held-out and forward evidence gates, and takes effect on worker restart.

No real dataset was supplied with the recorder package. Actual training and evaluation need timestamped two-sided books, public spot observations, contemporaneous fee metadata and official finalized matches across separate days. Resale training also needs continuous executable depth through each horizon. The synthetic test results verify software behavior. Forecast accuracy and paper profitability remain unmeasured. See [the evaluation protocol](docs/EVALUATION.md), [the data contract](docs/ARCHITECTURE.md) and [the milestone plan](docs/PLAN.md).

