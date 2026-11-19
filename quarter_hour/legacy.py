"""Read a consistent Windows recorder backup without mutating it."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from .collector import BAD_DATA, Response, parse_book, parse_market
from .domain import Snapshot, Spot, epoch, positive
from .storage import Store


def _snapshot(row, at: float, snapshot_data: dict, *, gap: bool) -> Snapshot:
    flags = ["LEGACY_FEE_TIMING_AND_OVERRIDES_UNVERIFIED"]
    market = book = spot = None
    raw_market = snapshot_data.get("market")
    if raw_market:
        try:
            observed = raw_market["observedAt"] / 1000
            if not 0 < observed <= at:
                raise ValueError("invalid legacy market receipt")
            market = parse_market({
                "ticker": raw_market["ticker"], "event_ticker": raw_market["eventTicker"],
                "market_type": "binary", "open_time": raw_market["openTime"],
                "close_time": raw_market["closeTime"], "status": "active",
                "floor_strike": raw_market["target"], "strike_type": raw_market["strikeType"],
                "notional_value_dollars": "1", "can_close_early": False,
                "rules_primary": raw_market["rules"], "rules_secondary": raw_market["secondaryRules"],
            }, observed, expected=row["ticker"])
            flags.append("LEGACY_MARKET_FIELDS_INFERRED_FROM_BINARY_RECORDER")
        except BAD_DATA:
            flags.append("LEGACY_MARKET_INVALID")
    raw_book = snapshot_data.get("orderbook")
    if raw_book and market:
        try:
            received = raw_book["observedAt"] / 1000
            if not 0 < received <= at or raw_book["ticker"] != market.ticker:
                raise ValueError("invalid legacy book identity or receipt")
            data = {f"{side}_dollars": [[level["price"], level["quantity"]] for level in raw_book[f"{side}Bids"]]
                    for side in ("yes", "no")}
            book = parse_book(Response({"orderbook_fp": data}, received, received), market.ticker)
            book = replace(book, requested=None)
            flags.append("LEGACY_BOOK_REQUEST_AND_EXCHANGE_TIME_UNAVAILABLE")
        except BAD_DATA:
            flags.append("LEGACY_BOOK_INVALID")
    raw_spot = snapshot_data.get("spot")
    if raw_spot:
        try:
            captured = raw_spot["fetchedAt"] / 1000
            source = epoch(raw_spot["time"])
            if not 0 < source <= captured <= at:
                raise ValueError("invalid legacy spot source time")
            spot = Spot(positive(raw_spot["price"]), source, captured)
            flags.append("LEGACY_SPOT_RECEIPT_IS_UPPER_BOUND")
        except BAD_DATA:
            flags.append("LEGACY_SPOT_INVALID")
    return Snapshot(at, market, book, spot, flags=tuple(flags), gap=gap)


def import_recorder(store: Store, backup: Path, *, interval: float = 5) -> dict:
    path = backup.resolve(strict=True)
    if path == store.path:
        raise ValueError("use a separate database for the import")
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("use a standalone recorder backup rather than an active database with a WAL")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute("BEGIN")
        original_counts = {
            table: source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("forecasts", "positions", "candles", "events")
        }
        with store.worker_lock(), store.transaction():
            if store.last_snapshot() is not None:
                raise ValueError("import requires an empty research database")
            rows = source.execute("SELECT * FROM samples ORDER BY id")
            prior = None
            imported = skipped = 0
            for row in rows:
                at = row["collected_at"] / 1000
                if at <= 0:
                    raise ValueError("invalid recorder timestamp")
                # Retain the original forecasts and quote JSON alongside the normalized observations.
                original = dict(row)
                store.raw("legacy:sample", at, at, None, original)
                if prior is not None and at <= prior:
                    store.quality(at, "LEGACY_NON_MONOTONIC_TIME", sample=row["id"])
                    skipped += 1
                    continue
                data = json.loads(row["quote_json"])
                gap = prior is None or at - prior > interval * 2.5
                sample = _snapshot(row, at, data, gap=gap)
                if sample.market and not store.save_market(sample.market):
                    store.quality(at, "LEGACY_MARKET_IDENTITY_CHANGED", sample=row["id"])
                    sample = replace(sample, market=None, book=None, flags=sample.flags + ("IDENTITY_CHANGED",))
                store.save_snapshot(sample)
                imported += 1
                prior = at
            for row in source.execute("SELECT * FROM markets WHERE result IS NOT NULL"):
                # The legacy market table has a last-check time and result, but no source response.
                store.raw("legacy:unverified_result", row["checked_at"] / 1000,
                          row["checked_at"] / 1000, None, dict(row))
    return {
        "imported": imported, "skipped_non_monotonic": skipped,
        "archived_in_original_backup": original_counts,
        "official_results": "Recheck the exact tickers using the public Kalshi endpoint",
        "costed_replay": "Legacy rows lack contemporaneous event fees and book request times",
    }
