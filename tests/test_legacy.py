import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from quarter_hour.legacy import import_recorder
from quarter_hour.storage import Store

from .fixtures import START, iso, snapshot


class LegacyTests(unittest.TestCase):
    def test_import_retains_observations_without_inventing_fees_or_results(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "legacy.sqlite"
            sample = snapshot()
            market = sample.market
            at = START + 20
            data = {
                "market": {
                    "ticker": market.ticker, "eventTicker": market.event, "openTime": iso(START),
                    "closeTime": iso(market.closes), "target": 100_000,
                    "observedAt": (at - 1) * 1000, "strikeType": "greater_or_equal",
                    "rules": "Synthetic rules", "secondaryRules": "",
                },
                "spot": {"price": 100_010, "time": iso(at - .5), "fetchedAt": at * 1000},
                "orderbook": {
                    "ticker": market.ticker, "observedAt": at * 1000,
                    "yesBids": [{"price": .20, "quantity": 6}],
                    "noBids": [{"price": .75, "quantity": 10}],
                },
                "fees": {"verified": True, "type": "quadratic", "multiplier": 1},
            }
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE samples(id,collected_at,ticker,quote_json,probability)")
                db.execute("CREATE TABLE markets(ticker,result,checked_at)")
                for table in ("forecasts", "positions", "candles", "events"):
                    db.execute(f"CREATE TABLE {table}(id)")
                db.execute("INSERT INTO samples VALUES (1,?,?,?,.6)", (at * 1000, market.ticker, json.dumps(data)))
                db.execute("INSERT INTO markets VALUES (?,'yes',?)", (market.ticker, (at + 900)*1000))
            db.close()
            original = source.read_bytes()
            with Store(root / "modern.sqlite3") as target:
                report = import_recorder(target, source)
                self.assertEqual(report["imported"], 1)
                observed = target.last_snapshot()
                self.assertEqual(observed.ticker, sample.ticker)
                self.assertEqual(observed.book.received, at)
                self.assertIsNone(observed.book.requested)
                self.assertIsNone(observed.fees)
                self.assertEqual(target.results(), {})
                raw = target.db.execute("SELECT payload FROM raw_responses WHERE route='legacy:sample'").fetchone()
                self.assertEqual(json.loads(raw[0])["probability"], .6)
                with self.assertRaisesRegex(ValueError, "empty"):
                    import_recorder(target, source)
            self.assertEqual(source.read_bytes(), original)
