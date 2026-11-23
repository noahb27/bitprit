import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError

from quarter_hour.collector import COINBASE, KALSHI, SERIES, Collector, PublicGet, Response, Unavailable
from quarter_hour.domain import Side
from quarter_hour.policy import Policy, entry_offer
from quarter_hour.storage import Store

from .fixtures import START, iso, result_for, snapshot


class FakePublic:
    def __init__(self, clock):
        self.clock = clock
        self.market = snapshot().market
        self.final = False
        self.fail_spot = False
        self.fail_book = False
        self.detail_status = "determined"
        self.detail_event = None

    def get(self, base, path, **query):
        key, event = self.market.ticker, self.market.event
        now = self.clock()
        body = {
            "ticker": key, "event_ticker": event, "market_type": "binary",
            "open_time": iso(START), "close_time": iso(START + 900), "status": "active",
            "floor_strike": 100000, "strike_type": "greater_or_equal", "can_close_early": self.market.can_close_early,
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
            data = {"market": body | {"status": self.detail_status, "result": "yes"}}
            if self.detail_event:
                data["market"]["event_ticker"] = self.detail_event
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

    def test_early_close_market_is_rechecked_after_each_book(self):
        now = START + 30
        clock = lambda: now
        public = FakePublic(clock)
        public.market = replace(public.market, can_close_early=True)
        public.detail_status = "active"
        get = public.get

        def forward(*args, **kwargs):
            nonlocal now
            now += .05
            return get(*args, **kwargs)

        public.get = Mock(side_effect=forward)
        with tempfile.TemporaryDirectory() as folder, Store(Path(folder) / "local.sqlite3") as store:
            collector = Collector(store, public, interval=5, clock=clock)
            collector.collect()
            now += 5
            active = collector.collect()
            self.assertGreaterEqual(active.market.status_requested, active.book.received)
            self.assertEqual(public.get.call_args_list[-1].args, (KALSHI, f"/markets/{active.ticker}"))
            self.assertIsNotNone(entry_offer(active, Side.YES, Policy(), at=active.at).buy)

            now += 1
            public.detail_status = "closed"
            closed = collector.collect()
            self.assertEqual(closed.market.status, "closed")
            self.assertIn("OUTSIDE_TRADING_WINDOW", entry_offer(closed, Side.YES, Policy(), at=closed.at).failures)

            now += 1
            public.detail_status = "active"
            public.detail_event = active.ticker
            wrong_event = collector.collect()
            self.assertEqual(wrong_event.market.event, active.market.event)
            self.assertTrue(any(flag.startswith("MARKET_STATUS_INVALID") for flag in wrong_event.flags))
            self.assertIn("EARLY_CLOSE_STATUS_UNCONFIRMED",
                          entry_offer(wrong_event, Side.YES, Policy(), at=wrong_event.at).failures)

    def test_only_official_matching_early_close_can_shorten_settlement(self):
        example = snapshot()
        official = replace(example.market, closes=START + 100, can_close_early=True)
        result = replace(result_for(example), received=START + 120, settled=START + 110, market_close=START + 100)
        with tempfile.TemporaryDirectory() as folder, Store(Path(folder) / "local.sqlite3") as store:
            store.save_market(example.market)
            self.assertFalse(store.save_result(replace(official, can_close_early=False), result))
            self.assertFalse(store.save_result(replace(official, opens=START - 5), result))
            self.assertTrue(store.save_result(official, result))
            self.assertEqual(store.results()[result.ticker].market_close, START + 100)
            self.assertEqual(store.db.execute("SELECT closes FROM markets").fetchone()[0], example.market.closes)

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

    def test_public_get_records_payload_and_rejects_http_cached_data(self):
        class Reply(io.BytesIO):
            def __init__(self, age):
                super().__init__(b'{"level": 0.23}')
                self.status = 200
                self.headers = {"Age": age}

        with tempfile.TemporaryDirectory() as folder, Store(Path(folder) / "local.sqlite3") as store:
            client = PublicGet(store, clock=lambda: START)
            client.opener = SimpleNamespace(open=Mock(side_effect=[Reply("0"), Reply("10")]))
            response = client.get(KALSHI, "/markets")
            self.assertEqual(str(response.body["level"]), "0.23")
            with self.assertRaisesRegex(Unavailable, "cache age"):
                client.get(KALSHI, "/markets")
            rows = store.db.execute("SELECT payload FROM raw_responses ORDER BY id").fetchall()
            self.assertEqual([json.loads(row[0])["http_age"] for row in rows], ["0", "10"])
            request = client.opener.open.call_args_list[0].args[0]
            self.assertEqual(request.get_method(), "GET")

    def test_backup_is_consistent_during_an_uncommitted_write(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "local.sqlite3"
            backup = Path(folder) / "backup.sqlite3"
            with Store(path) as reader, Store(path) as writer, writer.transaction():
                writer.quality(START, "UNCOMMITTED")
                reader.backup(backup)
            with sqlite3.connect(backup) as copy:
                self.assertEqual(copy.execute("SELECT count(*) FROM quality").fetchone()[0], 0)
