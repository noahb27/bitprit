import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from quarter_hour.dataset import Periods, delayed_resale, make_dataset
from quarter_hour.domain import Side
from quarter_hour.features import FeatureCursor
from quarter_hour.fit import PHASES
from quarter_hour.policy import Policy
from quarter_hour.registry import load_artifact
from quarter_hour.research import grade_holdout, train
from quarter_hour.storage import Store

from .fixtures import START, result_for, snapshot

PERIODS = Periods("2025-01-01", "2025-01-03", "2025-01-05", "2025-01-07", "2025-01-10")


class DatasetTests(unittest.TestCase):
    def test_delayed_horizon_uses_first_eligible_full_book_and_censors_gaps(self):
        samples = [snapshot(offset, id=i + 1) for i, offset in enumerate((10, 11, 12, 20, 30, 40, 42, 44, 45))]
        samples[7] = snapshot(44, yes=".40", no=".55", id=8)
        samples[8] = snapshot(45, yes=".90", no=".05", id=9)
        label, reason = delayed_resale(samples, 0, Side.YES, 30, Policy())
        self.assertEqual(reason, "label")
        self.assertEqual(label[1][0], .40)
        self.assertEqual(label[2], START + 44)
        opposite, _ = delayed_resale(samples, 0, Side.NO, 30, Policy())
        self.assertEqual(opposite[1][0], .55)
        gap = samples[:]
        gap[4] = replace(gap[4], gap=True)
        self.assertEqual(delayed_resale(gap, 0, Side.YES, 30, Policy())[1], "exit_gap")

    def test_features_ignore_future_receipts_and_cutoffs_purge_unavailable_labels(self):
        policy = Policy()
        samples = [snapshot(i, id=i + 1) for i in range(0, 200, 5)]
        first = FeatureCursor()
        factual = [first.observe(row, policy) for row in samples[:25]]
        self.assertIsNotNone(factual[-1])
        second = FeatureCursor()
        future = replace(samples[-1], spot=replace(samples[-1].spot, source_at=START + 300))
        compared = [second.observe(row, policy) for row in samples[:-1] + [future]]
        self.assertEqual(compared[:25], factual)
        self.assertIsNone(compared[-1])
        result = result_for(samples[0], delay=120)
        labeled = make_dataset(samples, {result.ticker: result}, policy, START, START + 1100)
        early = make_dataset(samples, {result.ticker: result}, policy, START, START + 1020)
        split = make_dataset(samples, {result.ticker: result}, policy, START + 60, START + 2000)
        self.assertEqual(len(labeled.settlement), 1)
        self.assertEqual(len(early.settlement), 0)
        self.assertEqual(len(split.settlement), 0)
        self.assertLess(labeled.settlement[0].at, labeled.settlement[0].label_at)

    def test_empty_data_stays_untrained(self):
        with tempfile.TemporaryDirectory() as folder, Store(Path(folder) / "none.sqlite3") as store:
            outcome = train(store, PERIODS, Policy())
            self.assertEqual(outcome["status"], "UNTRAINED")
            self.assertNotIn("artifact_id", outcome)
            self.assertEqual(store.db.execute("SELECT count(*) FROM artifacts").fetchone()[0], 0)
            self.assertTrue(outcome["settlement"]["missing"])


def add_markets(store, day_offsets):
    market_id = store.db.execute("SELECT count(*) FROM markets").fetchone()[0]
    for day, count in day_offsets:
        for slot in range(count):
            start = START + 86_400 * day + 3600 + slot * 900
            market_id += 1
            side = Side.YES if market_id % 2 == 0 else Side.NO
            with store.transaction():
                for offset in range(0, 900, 5):
                    row = snapshot(offset, start=start, ordinal=market_id,
                                   yes=".35" if side == Side.YES else ".32",
                                   no=".59" if side == Side.YES else ".62")
                    p = Decimal(100_045 if side == Side.YES else 99_955) + Decimal((offset % 70) / 100)
                    row = replace(row, spot=replace(row.spot, price=p), gap=offset == 0)
                    store.save_market(row.market)
                    store.save_snapshot(row)
                result = result_for(row, side)
                store.save_result(row.market, result)


class LearningTests(unittest.TestCase):
    def test_frozen_challenger_and_later_holdout_each_use_separate_markets(self):
        try:
            import sklearn  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("install requirements-research.txt for the fitting test")
        with tempfile.TemporaryDirectory() as folder, Store(Path(folder) / "lab.sqlite3") as store:
            add_markets(store, [(0, 12), (1, 12), (2, 5), (3, 5), (4, 5), (5, 5)])
            output = train(store, PERIODS, Policy())
            self.assertEqual(output["status"], "CHALLENGER")
            key = output["artifact_id"]
            artifact = load_artifact(store, key)
            self.assertEqual(set(artifact["dataset_digests"]), set(PHASES))
            self.assertEqual(len(artifact["resale"]), 6)
            self.assertEqual(output["settlement"]["counts"]["training"]["markets"], 24)
            original = json.dumps(artifact, sort_keys=True)
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute("UPDATE artifacts SET payload='{}' WHERE id=?", (key,))
            self.assertEqual(grade_holdout(store, key)["status"], "INSUFFICIENT_HOLDOUT")
            add_markets(store, [(6, 6), (7, 6), (8, 6)])
            # A later data append must not change earlier features or a frozen artifact.
            self.assertEqual(train(store, PERIODS, Policy())["artifact_id"], key)
            final = grade_holdout(store, key)
            self.assertEqual(final["status"], "HOLDOUT_GRADED")
            self.assertEqual(final["settlement"]["frozen"]["markets"], 18)
            self.assertEqual(set(final["policy_replay"]), {
                "settlement_only", "resale_only", "combined", "no_trade", "cheap_contract",
            })
            self.assertFalse(final["gates"]["eligible_for_forward_trial"])
            self.assertEqual(grade_holdout(store, key)["status"], "HOLDOUT_GRADED")
            self.assertEqual(json.dumps(load_artifact(store, key), sort_keys=True), original)
            with self.assertRaisesRegex(ValueError, "overlaps"):
                train(store, PERIODS, Policy())

    def test_dates_must_be_strict_and_utc_grouping_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "ascending"):
            Periods("2025-01-01", "2025-01-01", "2025-01-05", "2025-01-07", "2025-01-10")
        self.assertEqual(PERIODS.window("training")[0], datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
