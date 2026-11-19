import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from quarter_hour.cli import main, source_status
from quarter_hour.domain import Side, json_text
from quarter_hour.forecast import policy_signature
from quarter_hour.journal import PaperWorker, forward_report, paper_state, promote
from quarter_hour.policy import Policy
from quarter_hour.registry import active_model, save_artifact
from quarter_hour.storage import Store

from .fixtures import START, result_for, snapshot


def save(store, sample):
    store.save_market(sample.market)
    return store.save_snapshot(sample)


SYNTHETIC_MODEL_OUTPUT = {
    "status": "READY", "settlement_yes": .85,
    "resale": {"yes_30": {"net_return": {"p10": .12, "p50": .24, "p90": .39}}},
    "resale_exclusions": {},
}


class JournalTests(unittest.TestCase):
    def test_untrained_decisions_and_forecasts_are_immutable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as folder, Store(Path(folder) / "paper.sqlite3") as store:
            first = save(store, snapshot())
            worker = PaperWorker(store, Policy())
            result = worker.record(first, now=first.at)
            self.assertEqual(result["action"], "WAIT")
            self.assertEqual(result["reason"], "UNTRAINED")
            self.assertIn("book_age_seconds", result["detail"]["sides"]["yes"])
            self.assertEqual(worker.record(first, now=first.at + 1), result)
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute("UPDATE forecasts SET status='READY'")
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)

    def test_open_position_survives_restart_and_uses_exact_finalized_payout(self):
        policy = Policy(entry_cap="3.00")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper.sqlite3"
            with Store(path) as store, patch("quarter_hour.journal.forecast", return_value=SYNTHETIC_MODEL_OUTPUT):
                first = save(store, snapshot(10))
                second = save(store, snapshot(12))
                worker = PaperWorker(store, policy)
                self.assertEqual(worker.record(first, now=first.at)["status"], "SUBMITTED")
                self.assertEqual(worker.record(second, now=second.at)["status"], "FILLED")
                self.assertLessEqual(paper_state(store)[0].position.entry.quote.cash, policy.entry_cap)
            with Store(path) as store:
                worker = PaperWorker(store, Policy(entry_cap=".10"))
                third = save(store, replace(snapshot(904), gap=True))
                self.assertEqual(worker.record(third, now=third.at)["status"], "AWAITING_SETTLEMENT")
                restored = paper_state(store)[0].position
                self.assertEqual(restored.order.policy, policy)
                result = result_for(first, Side.NO)
                self.assertTrue(store.save_result(first.market, result))
                subsequent = save(store, snapshot(82, ordinal=1, start=first.market.opens + 900))
                self.assertEqual(worker.record(subsequent, now=subsequent.at)["action"], "SETTLE")
                self.assertIsNone(paper_state(store)[0].position)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)
                metrics = forward_report(store)
                self.assertEqual(metrics["settlement_trades"], 1)
                self.assertEqual(metrics["resale_trades"], 0)
                self.assertEqual(metrics["net_pnl"], -restored.entry.quote.cash)

    def test_trials_and_activation_require_grade_evidence(self):
        with tempfile.TemporaryDirectory() as folder, Store(Path(folder) / "paper.sqlite3") as store:
            identifier = save_artifact(store, {"status": "CHALLENGER", "fixture": True})
            with self.assertRaisesRegex(ValueError, "holdout gates"):
                PaperWorker(store, Policy(), trial=identifier)
            self.assertEqual(promote(store, identifier)["reason"], "SEALED_HOLDOUT_GATES_NOT_PASSED")
            self.assertIsNone(active_model(store))

    def test_promotion_is_explicit_after_forward_evidence_gates(self):
        with tempfile.TemporaryDirectory() as folder, Store(Path(folder) / "paper.sqlite3") as store:
            cfg = Policy()
            identifier = save_artifact(store, {"status": "CHALLENGER", "policy_signature": policy_signature(cfg)})
            report = json_text({"gates": {"eligible_for_forward_trial": True}})
            store.db.execute("INSERT INTO evaluations VALUES (?,?,?,?,?)",
                             (identifier, START - 1, START - 172_800, START - 86_400, report))
            self.assertEqual(promote(store, identifier)["status"], "PROMOTION_BLOCKED")
            worker = PaperWorker(store, cfg, trial=identifier)
            with patch("quarter_hour.journal.forecast", return_value=SYNTHETIC_MODEL_OUTPUT):
                for day in range(5):
                    for slot in range(4):
                        start = START + day * 86_400 + slot * 900
                        ordinal = day * 4 + slot + 1
                        for offset in (10, 12, 20, 22):
                            sample = snapshot(offset, start=start, ordinal=ordinal,
                                              yes=".44" if offset >= 20 else ".20",
                                              no=".50" if offset >= 20 else ".74")
                            sample = save(store, sample)
                            worker.record(sample, now=sample.at)
            self.assertIsNone(active_model(store))
            promoted = promote(store, identifier)
            self.assertEqual(promoted["status"], "PAPER_MODEL_ACTIVATED")
            self.assertEqual(active_model(store), identifier)

    def test_source_probe_reports_real_feed_health_separately_from_paper_policy(self):
        with tempfile.TemporaryDirectory() as folder, Store(Path(folder) / "paper.sqlite3") as store:
            first = snapshot(20)
            save(store, replace(first, market=replace(first.market, can_close_early=True, notional=Decimal("2"))))
            report = source_status(store, Policy())
            self.assertTrue(report["ready_at_last_snapshot"])
            self.assertEqual(report["failures"], [])
            self.assertEqual(report["market_notional_dollars"], 2)
            self.assertTrue(report["market_can_close_early"])
            self.assertIn("UNSUPPORTED_MARKET_NOTIONAL", report["paper_policy_failures"])
            self.assertIn("EARLY_CLOSE_STATUS_UNCONFIRMED", report["paper_policy_failures"])
            missing = snapshot(25)
            save(store, replace(missing, spot=None))
            self.assertIn("SPOT_MISSING", source_status(store, Policy())["failures"])

    def test_cli_status_history_and_restore_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "paper.sqlite3"
            with Store(path) as store:
                first = save(store, snapshot())
                PaperWorker(store, Policy()).record(first, now=first.at)
            with redirect_stdout(io.StringIO()) as text:
                self.assertEqual(main(["--db", str(path), "status"]), 0)
            report = json.loads(text.getvalue())
            self.assertTrue(report["paper_only"])
            self.assertEqual(report["model_state"], "UNTRAINED")
            self.assertEqual(report["latest_decision"]["payload"]["reason"], "UNTRAINED")
            backup = root / "backup.sqlite3"
            curve = root / "curve.csv"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--db", str(path), "history", first.ticker, "--csv", str(curve)]), 0)
                self.assertEqual(main(["--db", str(path), "backup", str(backup)]), 0)
            self.assertIn("yes_probability", curve.read_text())
            with Store(backup) as restored:
                self.assertEqual(paper_state(restored)[1], first.id)
