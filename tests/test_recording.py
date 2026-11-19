import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

from quarter_hour.collector import COINBASE, KALSHI, SERIES, Collector, PublicGet, Response, Unavailable
from quarter_hour.domain import Side
from quarter_hour.storage import Store

from .fixtures import START, iso, result_for, snapshot


class FakePublic:
    def __init__(self, clock):
        self.clock = clock
        self.market = snapshot().market
        self.final = False
        self.fail_spot = False
        self.fail_book = False

    def get(self, base, path, **query):
        key, event = self.market.ticker, self.market.event
        now = self.clock()
        body = {
            "ticker": key, "event_ticker": event, "market_type": "binary",
            "open_time": iso(START), "close_time": iso(START + 900), "status": "active",
            "floor_strike": 100000, "strike_type": "greater_or_equal", "can_close_early": False,
            "rules_primary": "Synthetic terms", "notional_value_dollars": "1.0000",
        }
        if base == COINBASE:
            if self.fail_spot:
                raise Unavailable("provider backoff")
            data = {"price": "100001.25", "time": iso(now - 1)}
        elif path == "/markets":
            assert query["series_ticker"] == SERIES
            data = {"markets": [body]}
        elif path == f"/series/{SERIES}":
            data = {"series": {
                "ticker": SERIES, "fee_type": "quadratic", "fee_multiplier": 1,
                "contract_terms_url": "https://example.invalid", "settlement_sources": [{"name": "Test index"}],
            }}
        elif path == f"/events/{event}":
            data = {"event": {
                "event_ticker": event, "series_ticker": SERIES,
                "fee_type_override": "quadratic", "fee_multiplier_override": 1.25,
            }}
        elif path.endswith("/orderbook"):
            assert path == f"/markets/{key}/orderbook"
            data = {"orderbook_fp": {
                "yes_dollars": [["0.20", "6.09"], ["0.24", "10.00"]],
                "no_dollars": [["0.70", "20.00"], ["0.65", "11.00"]],
            }}
            if self.fail_book:
                data["orderbook_fp"]["yes_dollars"].append(["0.24", "12"])
        else:
            assert path == f"/markets/{key}"
            data = {"market": body | {"status": "determined", "result": "yes"}}
            if self.final:
                data["market"] |= {
                    "status": "finalized", "settlement_ts": iso(START + 930),
                    "result": "no", "settlement_value_dollars": "0", "expiration_value": "99950",
                }
        return Response(data, now - .01, now)


class RecordingTests(unittest.TestCase):
    def test_capture_restart_result_export_and_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "local.sqlite3"
            now = START + 20
            clock = lambda: now
            public = FakePublic(clock)
            key = public.market.ticker
            with Store(path) as store:
                collector = Collector(store, public, interval=3, clock=clock)
                first = collector.collect()
                self.assertTrue(first.gap)
                self.assertEqual(str(first.book.yes[0][0]), "0.24")
                self.assertEqual(str(first.fees.multiplier), "1.25")
                self.assertEqual(first.spot.source_at, now - 1)
                public.fail_spot = True
                public.fail_book = True
                now += 3
                invalid = collector.collect()
                self.assertFalse(invalid.gap)
                self.assertIsNone(invalid.spot)
                self.assertIsNone(invalid.book)
                self.assertTrue(any("BOOK_INVALID" in f for f in invalid.flags))
                self.assertEqual(invalid.market.received, first.market.received)
            with Store(path) as store:
                now = START + 935
                worker = Collector(store, public, interval=3, clock=clock)
                restarted = worker.collect()
                self.assertTrue(restarted.gap)
                self.assertIsNone(restarted.market)
                worker.check_result()
                self.assertEqual(store.results(), {})
                now += 65
                public.final = True
                worker.check_result()
                result = store.results()[key]
                self.assertEqual(result.side, Side.NO)
                self.assertEqual(result.benchmark_value, 99_950)
                store.backup(root / "backup.sqlite3")
                store.export(root / "data.jsonl")
                with self.assertRaises(ValueError):
                    store.export(root / "data.jsonl")
            with Store(root / "backup.sqlite3") as restored:
                self.assertEqual(restored.results()[key], result)
                self.assertEqual(len(list(restored.snapshots())), 3)
                self.assertGreater(restored.db.execute("SELECT count(*) FROM quality").fetchone()[0], 0)
            lines = [json.loads(line) for line in (root / "data.jsonl").read_text().splitlines()]
            self.assertIn("schema_version", lines[0])
            self.assertTrue(any(line.get("table") == "results" for line in lines))

    def test_recorded_identity_time_and_duplicate_worker(self):
        with tempfile.TemporaryDirectory() as folder, Store(Path(folder) / "local.sqlite3") as store:
            a = snapshot()
            b = snapshot(ordinal=2)
            self.assertTrue(store.save_market(a.market))
            self.assertFalse(store.save_result(a.market, result_for(b)))
            store.save_snapshot(a)
            with self.assertRaises(ValueError):
                store.save_snapshot(a)
            with store.worker_lock():
                with self.assertRaisesRegex(ValueError, "already running"):
                    with store.worker_lock():
                        self.fail("duplicate worker acquired lock")
            with store.worker_lock():
                pass
            store.db.execute("PRAGMA user_version=99")
            with self.assertRaisesRegex(ValueError, "newer"):
                Store(store.path)

    def test_public_get_backs_off_on_429(self):
        with tempfile.TemporaryDirectory() as folder, Store(Path(folder) / "local.sqlite3") as store:
            client = PublicGet(store, clock=lambda: START)
            client.opener = Mock()
            client.opener.open.side_effect = HTTPError(KALSHI, 429, "limited", {"Retry-After": "18"}, None)
            with self.assertRaisesRegex(Unavailable, "429"):
                client.get(KALSHI, "/markets")
            with self.assertRaisesRegex(Unavailable, "backoff"):
                client.get(KALSHI, "/markets")
            self.assertEqual(client.opener.open.call_count, 1)
            row = store.db.execute("SELECT status FROM raw_responses").fetchone()
            self.assertEqual(row[0], 429)

    def test_backup_is_consistent_during_an_uncommitted_write(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "local.sqlite3"
            backup = Path(folder) / "backup.sqlite3"
            with Store(path) as reader, Store(path) as writer, writer.transaction():
                writer.quality(START, "UNCOMMITTED")
                reader.backup(backup)
            with sqlite3.connect(backup) as copy:
                self.assertEqual(copy.execute("SELECT count(*) FROM quality").fetchone()[0], 0)
