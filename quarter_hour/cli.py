"""Local command-line controls and inspection."""

import argparse
import csv
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .archive import backup_database, new_output, verify_backup
from .collector import Collector, PublicGet, ticker
from .config import Settings
from .dataset import Periods
from .disk import is_storage_full, storage_full, storage_status
from .domain import Side, elapsed, epoch, json_text, seconds
from .journal import PaperWorker, forward_report, original_forecast_report, paper_state, promote
from .legacy import import_recorder
from .policy import Policy, paper_quote_conditions, source_conditions
from .registry import active_model, evaluation, load_artifact
from .replay import replay
from .research import advisors, grade_holdout, replay_many, train
from .storage import Store
from .worker import request_stop, run_worker


def bounded_count(raw: str) -> int:
    count = int(raw)
    if not 1 <= count <= 1000:
        raise argparse.ArgumentTypeError("choose a count from 1 through 1000")
    return count


def volume_limit(raw: str) -> int:
    megabytes = int(raw)
    if megabytes <= 0:
        raise argparse.ArgumentTypeError("volume limit must be positive")
    return megabytes * 1_000_000


def timestamp(text: str) -> float:
    try:
        if len(text) == 10:
            return epoch(text + "T00:00:00Z")
        return epoch(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD in UTC or an ISO timestamp with timezone") from exc


def iso(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="milliseconds")


def parse(argv):
    root = argparse.ArgumentParser(description="Quarter Hour local market research and paper execution")
    root.add_argument("--db", type=Path, help="SQLite database path")
    root.add_argument("--config", type=Path, help="settings file, defaults to local.toml if present")
    root.add_argument("--volume-limit-mb", dest="volume_limit_bytes", type=volume_limit, metavar="MB",
                      help="capacity in decimal MB of the dedicated database directory")
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="record public data and evaluate paper positions")
    modes = run.add_mutually_exclusive_group()
    modes.add_argument("--collect-only", action="store_true", help="record without paper decisions")
    modes.add_argument("--trial", metavar="ARTIFACT", help="paper workflow for an eligible frozen challenger")
    run.add_argument("--cycles", type=bounded_count, help="stop after a bounded number of recording cycles")
    run.add_argument("--status-every", type=bounded_count,
                     help="write a compact health summary every N recording cycles")
    run.add_argument("--require-sources", action="store_true",
                     help="check recorded source readiness on the last cycle and exit 2 if unavailable")
    commands.add_parser("stop", help="ask the local worker to stop after the current cycle")
    commands.add_parser("status", help="show collection health, latest decision and model state")
    commands.add_parser("storage", help="inspect disk usage without opening the database")
    backup = commands.add_parser("backup", help="make a consistent standalone SQLite backup and checksum")
    backup.add_argument("path", type=Path)
    backup.add_argument("--off-volume", action="store_true",
                        help="require the destination to be on a separate filesystem")
    verify = commands.add_parser("verify-backup", help="verify a standalone backup and optional manifest")
    verify.add_argument("path", type=Path)
    verify.add_argument("--manifest", type=Path)
    export = commands.add_parser("export", help="export a consistent copy of the recordings as JSONL")
    export.add_argument("path", type=Path)
    legacy = commands.add_parser("import-recorder", help="import a read-only backup of the supplied Node recorder")
    legacy.add_argument("backup", type=Path)
    legacy.add_argument("--interval", type=float, default=5, help="legacy polling interval in seconds")
    refresh = commands.add_parser("refresh-results", help="recheck exact tickers for official results")
    refresh.add_argument("--checks", type=bounded_count, default=10)
    history = commands.add_parser("history", help="inspect a recorded market's price and forecast curves")
    history.add_argument("ticker", type=ticker)
    history.add_argument("--csv", type=Path, dest="csv_output")
    for name in ("decisions", "trades"):
        inspect = commands.add_parser(name, help=f"inspect recent paper {name}")
        inspect.add_argument("--limit", type=bounded_count, default=10)
    report = commands.add_parser("report", help="summarize paper P&L and original forecasts")
    report.add_argument("--model")
    report.add_argument("--trial-only", action="store_true")
    simulation = commands.add_parser("replay", help="replay recorded data with fixed or model policies")
    simulation.add_argument("--since", type=timestamp, default=0)
    simulation.add_argument("--until", type=timestamp)
    simulation.add_argument("--model")
    simulation.add_argument("--stress", action="store_true")
    simulation.add_argument("--trace", type=Path, help="save cheap-contract decisions and trades to a new JSON file")
    learner = commands.add_parser("train", help="fit and freeze a challenger with declared date boundaries")
    for arg in ("training-from", "calibration-from", "selection-from", "holdout-from", "holdout-until"):
        learner.add_argument("--" + arg, required=True, metavar="YYYY-MM-DD")
    for name, help_text in (
        ("evaluate", "grade a frozen challenger on its sealed holdout"),
        ("model", "inspect a frozen artifact and its saved evaluation"),
        ("promote", "activate a paper model after all forward gates pass"),
    ):
        item = commands.add_parser(name, help=help_text)
        item.add_argument("artifact")
    args = root.parse_args(argv)
    if args.command == "run" and args.require_sources and args.cycles is None:
        root.error("--require-sources requires --cycles")
    if args.command == "report" and args.trial_only and not args.model:
        root.error("--trial-only requires --model")
    if args.command == "replay" and args.until is not None and args.until <= args.since:
        root.error("--until must follow --since")
    return args


def output(report):
    print(json.dumps(json.loads(json_text(report)), indent=2, ensure_ascii=True))


def source_status(store, policy: Policy) -> dict:
    latest = store.last_snapshot()
    if latest is None:
        return {"ready_at_last_snapshot": False, "failures": ["NO_RECORDED_OBSERVATIONS"]}
    failures, info = source_conditions(latest, policy, at=latest.at, entry=True)
    market, fees = latest.market, latest.fees
    result = {
        "ready_at_last_snapshot": not failures, "failures": failures,
        "paper_policy_failures": paper_quote_conditions(latest, policy, at=latest.at, entry=True),
        **info,
    }
    return result | {
        "recorded_at_utc": iso(latest.at), "seconds_since_sample": elapsed(time.time(), latest.at),
        "spot_price": latest.spot.price if latest.spot else None,
        "yes_bid_levels": len(latest.book.yes) if latest.book else 0,
        "no_bid_levels": len(latest.book.no) if latest.book else 0,
        "market_series_ticker": market.series if market else None,
        "market_event_ticker": market.event if market else None,
        "market_open_utc": iso(market.opens) if market else None,
        "market_close_utc": iso(market.closes) if market else None,
        "market_strike_type": market.strike_type if market else None,
        "target": market.strike if market else None,
        "fee_event_ticker": fees.event if fees else None,
        "terms_url": fees.terms_url if fees else None,
        "settlement_sources": fees.settlement_sources if fees else None,
    }


def _decoded(rows):
    for item in rows:
        row = dict(item)
        for key in ("payload", "detail"):
            if key in row and row[key] is not None:
                row[key] = json.loads(row[key])
        yield row


def status(store, policy, *, volume_limit_bytes=None):
    db = store.db
    counts = {
        name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        for name in ("observations", "markets", "results", "forecasts", "decisions", "trades", "artifacts")
    }
    last_worker = db.execute("SELECT * FROM worker WHERE slot=1").fetchone()
    recent_quality = db.execute("SELECT * FROM quality ORDER BY id DESC LIMIT 5")
    decisions = _decoded(db.execute("SELECT * FROM decisions ORDER BY sample_id DESC LIMIT 1"))
    model = active_model(store)
    return {
        "database": str(store.path), "paper_only": True, "entry_cap": policy.entry_cap,
        "storage": storage_status(store.path, volume_limit_bytes),
        "counts": counts, "active_model": model, "model_state": "PAPER_MODEL_ACTIVATED" if model else "UNTRAINED",
        "worker": dict(last_worker) if last_worker else None,
        "heartbeat_age_seconds": elapsed(time.time(), last_worker["heartbeat"]) if last_worker else None,
        "sources": source_status(store, policy), "latest_decision": next(decisions, None),
        "paper_state": paper_state(store)[0], "recent_quality": list(_decoded(recent_quality)),
    }


def _history(store, key):
    rows = store.db.execute(
        """SELECT o.id, o.payload, f.payload AS forecast, d.action, d.status
        FROM observations o LEFT JOIN forecasts f ON f.sample_id=o.id
        LEFT JOIN decisions d ON d.sample_id=o.id WHERE o.ticker=? ORDER BY o.at,o.id""", (key,),
    )
    for row in rows:
        sample = store._decode_snapshot(row)
        book = sample.book
        asks = {side: book.asks(side) if book else () for side in Side}
        bids = {side: book.bids(side) if book else () for side in Side}
        predicted = json.loads(row["forecast"])["settlement_yes"] if row["forecast"] else None
        yield {
            "sample_id": sample.id, "at_utc": iso(sample.at),
            "spot": sample.spot.price if sample.spot else None,
            "target": sample.market.strike,
            **{
                f"{side}_{kind}": levels[side][0][0] if levels[side] else None
                for side in Side for kind, levels in (("bid", bids), ("ask", asks))
            },
            "yes_probability": predicted, "action": row["action"], "decision_status": row["status"],
            "gap": sample.gap, "flags": " | ".join(sample.flags),
        }


def run_command(store, args, settings) -> int:
    worker = None if args.collect_only else PaperWorker(store, settings.policy, trial=args.trial)
    recorded = 0

    def sample_update(sample):
        nonlocal recorded
        if worker:
            decision = worker.record(sample)
            action = f"{decision['action']} {decision['status']} {decision['reason']}"
        else:
            action = ", ".join(sample.flags) or "RECORDED"
        print(f"{iso(sample.at)}  {sample.ticker or 'no current market'}  {action}", flush=True)
        recorded += 1
        if args.status_every and (recorded == 1 or recorded % args.status_every == 0):
            health = status(store, settings.policy, volume_limit_bytes=args.volume_limit_bytes)
            print(json_text({"health": health}), flush=True)

    print(f"Quarter Hour paper research  {store.path}", flush=True)
    halted = run_worker(
        store, interval=settings.interval, timeout=settings.timeout, cycles=args.cycles,
        on_sample=sample_update, priority=worker.priority if worker else None,
        volume_limit_bytes=args.volume_limit_bytes,
        on_storage_warning=lambda reading: print(json_text({"storage_warning": reading}), flush=True),
    )
    if halted:
        return storage_stop(args, halted)
    if args.require_sources:
        report = source_status(store, settings.policy)
        output(report)
        return 0 if report["ready_at_last_snapshot"] else 2
    return 0


def dispatch(store, args, settings):
    command = args.command
    if command == "run":
        return run_command(store, args, settings)
    if command == "stop":
        output({"stop_requested": request_stop(store)})
    elif command == "status":
        with store.transaction(write=False):
            output(status(store, settings.policy, volume_limit_bytes=args.volume_limit_bytes))
    elif command == "export":
        store.export(args.path)
        output({"saved_to": str(args.path.resolve())})
    elif command == "import-recorder":
        output(import_recorder(store, args.backup, interval=seconds(args.interval, "legacy interval", 1, 60)))
    elif command == "refresh-results":
        with store.worker_lock():
            collector = Collector(store, PublicGet(store, timeout=settings.timeout), interval=settings.interval)
            before = len(store.results())
            checked = 0
            for _ in range(args.checks):
                if not collector.check_result():
                    break
                checked += 1
                time.sleep(.3)
            output({"markets_checked": checked, "new_official_results": len(store.results()) - before})
    elif command == "history":
        rows = _history(store, args.ticker)
        if args.csv_output:
            first = next(rows, None)
            if first is None:
                raise ValueError("no recorded observations for this ticker")
            with new_output(args.csv_output) as path, path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(first))
                writer.writeheader()
                writer.writerow(first)
                writer.writerows(rows)
            output({"saved_to": str(args.csv_output.resolve())})
        else:
            output(list(rows))
    elif command in ("decisions", "trades"):
        entries = _decoded(store.db.execute(f"SELECT * FROM {command} ORDER BY sample_id DESC LIMIT ?", (args.limit,)))
        output(list(entries))
    elif command == "report":
        graded = evaluation(store, args.model) if args.trial_only else None
        if args.trial_only and graded is None:
            raise ValueError("no holdout grading record for this trial")
        if args.model:
            load_artifact(store, args.model)
        trial_from = graded[0] if graded else None
        with store.transaction(write=False):
            output({"forward_paper": forward_report(store, args.model, trial_from=trial_from),
                    "original_settlement_forecasts": original_forecast_report(store, args.model, trial_from=trial_from)})
    elif command == "replay":
        with store.transaction(write=False):
            def read():
                return store.snapshots(since=args.since, until=args.until)

            results = store.results(until=args.until)
            artifact = load_artifact(store, args.model) if args.model else None
            policy = Policy(**artifact["policy"]) if artifact else settings.policy
            end = args.until
            last = store.last_snapshot()
            if end is None and last:
                end = max((last.at, *(result.received for result in results.values())))
            experiments = replay_many(read, results, policy, artifact=artifact, with_stress=args.stress, end=end)
            baselines = experiments["base"] if args.stress else experiments
            has_data = baselines["no_trade"]["sample_start"] is not None
            if args.trace:
                cheap = advisors({}, None)["cheap_contract"]
                run = replay(read(), results, policy, cheap, keep_decisions=True, end_at=end)
                with new_output(args.trace) as temporary:
                    temporary.write_text(json_text({"metrics": run.metrics, "decisions": run.decisions,
                                                     "trades": run.trades, "ending_state": run.state}), encoding="utf-8")
        output({"status": "REPLAY_COMPLETE" if has_data else "NO_OBSERVED_DATA", "simulations": experiments})
    elif command == "train":
        periods = Periods(args.training_from, args.calibration_from, args.selection_from, args.holdout_from, args.holdout_until)
        output(train(store, periods, settings.policy))
    elif command == "evaluate":
        output(grade_holdout(store, args.artifact))
    elif command == "model":
        graded = evaluation(store, args.artifact)
        output({"artifact": load_artifact(store, args.artifact), "holdout_report": graded[1] if graded else None,
                "active": active_model(store) == args.artifact})
    elif command == "promote":
        report = promote(store, args.artifact)
        output(report)
        return 0 if report["status"] == "PAPER_MODEL_ACTIVATED" else 2
    return 0


def storage_stop(args, halted: dict) -> int:
    print(json_text({"storage_stop": halted}), flush=True)
    # A normal exit leaves Railway's ON_FAILURE policy idle. A bounded probe
    # still reports failure when storage prevented a source check.
    return 2 if args.require_sources else 0


def main(argv=None):
    args = parse(argv)
    try:
        if args.command == "verify-backup":
            output(verify_backup(args.path, manifest=args.manifest))
            return 0
        settings = Settings.read(args.config)
        database = args.db or settings.database
        if args.command == "backup":
            off_volume = args.off_volume or args.volume_limit_bytes is not None
            output(backup_database(database, args.path, off_volume=off_volume))
            return 0
        if args.command in ("run", "storage"):
            reading = storage_status(database, args.volume_limit_bytes)
            if args.command == "storage":
                output(reading)
                return 2 if reading["reason"] == "STORAGE_CHECK_FAILED" else 0
            if reading["state"] == "stop":
                return storage_stop(args, reading | {"shutdown_recorded": False})
        with Store(database) as store:
            return dispatch(store, args, settings)
    except (TypeError, ValueError, OSError, sqlite3.Error) as exc:
        if args.command == "run" and is_storage_full(exc):
            return storage_stop(args, storage_full(exc) | {"shutdown_recorded": False})
        print(f"Quarter Hour: {exc}", file=sys.stderr)
        return 2

