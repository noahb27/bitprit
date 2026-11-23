import unittest
from dataclasses import replace
from decimal import Decimal

from quarter_hour.domain import Side
from quarter_hour.execution import Advice, State, advance, cheap_contract, no_trade
from quarter_hour.policy import HARD_CAP, Policy, entry_offer, exit_quote, sweep
from quarter_hour.replay import replay, stress_cases

from .fixtures import result_for, snapshot


class ExecutionTests(unittest.TestCase):
    def test_decimal_price_walk_rounding_and_whole_contract_budget(self):
        sample = snapshot()
        book = replace(sample.book, no=((Decimal(".75"), Decimal("1.5")), (Decimal(".70"), Decimal(5))))
        policy = Policy(slippage=".005", max_contracts=20)
        fill = sweep(book, sample.fees, Side.YES, 3, policy, buy=True)
        self.assertEqual(fill.gross, Decimal(".825"))
        self.assertEqual(fill.fee, Decimal(".05"))
        self.assertEqual(fill.cash, Decimal(".890"))
        self.assertIsNone(sweep(book, sample.fees, Side.YES, 7, policy, buy=True))
        self.assertIsNone(sweep(book, sample.fees, Side.YES, 2.5, policy, buy=True))
        offer = entry_offer(sample, Side.YES, policy, at=sample.at)
        self.assertLessEqual(offer.buy.cash, HARD_CAP)
        self.assertEqual(offer.buy.quantity, 17)
        blocked = entry_offer(sample, Side.YES, replace(policy, entry_cap=".10"), at=sample.at)
        self.assertEqual(blocked.failures, ("ENTRY_LIMIT_EXCEEDED",))
        self.assertGreater(blocked.details["over_budget_by"], 0)
        with self.assertRaises(ValueError):
            Policy(entry_cap="5.0001")

    def test_independent_policy_blocks_invalid_signals(self):
        sample = snapshot()
        cfg = Policy()
        for observation, issue in (
            (replace(sample, spot=replace(sample.spot, source_at=sample.at - 30)), "SPOT_STALE_OR_FUTURE"),
            (replace(sample, book=replace(sample.book, ticker="OTHER")), "MATCHING_BOOK_MISSING"),
            (replace(sample, book=replace(sample.book, requested=sample.at - 5)), "BOOK_STALE_OR_FUTURE"),
            (replace(sample, book=replace(sample.book, yes=())), "TWO_SIDED_DEPTH_REQUIRED"),
            (replace(sample, fees=replace(sample.fees, type="flat")), "UNSUPPORTED_FEE_SCHEDULE"),
            (replace(sample, market=replace(sample.market, strike_type="custom")), "TARGET_OR_RULES_UNSUPPORTED_OR_MISSING"),
            (snapshot(895), "OUTSIDE_TRADING_WINDOW"),
            (snapshot(840), "INSUFFICIENT_TIME"),
            (snapshot(size=".99"), "INSUFFICIENT_FULL_SIZE_DEPTH"),
            (snapshot(yes=".40", no=".74"), "CROSSED_OR_LOCKED_BOOK"),
        ):
            with self.subTest(issue=issue):
                output = advance(State(), observation, {}, cfg, lambda *args: Advice(Side.YES, 60, "model"))
                self.assertIn(issue, output.decision.reason)
                self.assertIsNone(output.state.intent)

    def test_early_close_requires_status_request_after_the_book_for_both_legs(self):
        sample = snapshot()
        cfg = Policy()
        market = replace(sample.market, can_close_early=True)
        for requested in (None, sample.book.received - .01):
            quote = replace(sample, market=replace(market, status_requested=requested))
            self.assertIn("EARLY_CLOSE_STATUS_UNCONFIRMED", entry_offer(quote, Side.YES, cfg, at=quote.at).failures)
            self.assertIn("EARLY_CLOSE_STATUS_UNCONFIRMED", exit_quote(quote, Side.YES, 1, cfg, at=quote.at)[1])
        confirmed = replace(sample, market=replace(market, status_requested=sample.book.received + .01))
        self.assertIsNotNone(entry_offer(confirmed, Side.YES, cfg, at=confirmed.at).buy)
        self.assertIsNotNone(exit_quote(confirmed, Side.YES, 1, cfg, at=confirmed.at)[0])

        last = snapshot(885)
        late = replace(last, market=replace(last.market, can_close_early=True, status_requested=last.book.received))
        self.assertIn("OUTSIDE_TRADING_WINDOW", exit_quote(late, Side.YES, 1, cfg, at=late.at)[1])

    def test_replay_uses_earlier_official_settlement_only_after_receipt(self):
        original = snapshot(10, id=1)
        buy = snapshot(12, id=2)
        result = replace(result_for(original), market_close=original.market.opens + 30,
                         settled=original.market.opens + 32, received=original.market.opens + 45)
        samples = [original, buy]
        pending = replay(samples, {original.ticker: result}, Policy(), cheap_contract, end_at=result.received - 1)
        self.assertTrue(pending.metrics["open_position"])
        graded = replay(samples, {original.ticker: result}, Policy(), cheap_contract, end_at=result.received)
        self.assertEqual(graded.metrics["settlement_trades"], 1)
        self.assertEqual(graded.trades[0].exposure_seconds, 18)

    def test_latency_no_fill_on_trigger_and_no_retrospective_peak(self):
        samples = [
            snapshot(10, id=1), snapshot(10.5, id=2), snapshot(12, id=3),
            snapshot(20, yes=".44", no=".50", id=4),   # Take profit signal.
            snapshot(20.5, yes=".83", no=".12", id=5), # Before latency.
            snapshot(22, yes=".16", no=".77", id=6),  # Executable after latency.
            snapshot(24, yes=".70", no=".25", id=7),
        ]
        run = replay(samples, {}, Policy(), cheap_contract, keep_decisions=True)
        self.assertEqual(len(run.trades), 1)
        trade = run.trades[0]
        self.assertEqual(trade.position.entry.sample_id, 3)
        self.assertEqual(trade.exit.sample_id, 6)
        self.assertEqual(trade.exit.quote.average, Decimal(".16"))
        self.assertEqual(trade.reason, "PROFIT_TARGET")
        self.assertLess(trade.pnl, 0)
        self.assertEqual(run.decisions[4]["decision"].reason, "WAIT_FRESH_BOOK_AFTER_LATENCY")
        self.assertEqual(replay(samples, {}, Policy(), no_trade).metrics["closed_trades"], 0)

    def test_gap_price_limit_and_lower_cap_cancel_or_block_entry(self):
        policy = Policy()
        first = snapshot(10, id=1)
        pending = advance(State(), first, {}, policy, cheap_contract).state
        bad = snapshot(12, no=".70", id=2)
        more_expensive = advance(pending, bad, {}, policy, cheap_contract)
        self.assertEqual(more_expensive.decision.reason, "WAIT_ENTRY_COST_ABOVE_SIGNAL_LIMIT")
        lower_cap = advance(pending, snapshot(12), {}, replace(policy, entry_cap=".10"), cheap_contract)
        self.assertIn("ENTRY_LIMIT_EXCEEDED", lower_cap.decision.reason)
        gap = advance(pending, replace(snapshot(13), gap=True), {}, policy, cheap_contract)
        self.assertEqual(gap.decision.status, "CANCELLED_COLLECTION_GAP")
        self.assertIsNone(gap.state.intent)

    def test_stop_uses_owned_bid_and_rejects_thin_or_late_quote(self):
        cfg = Policy()
        snapshots = [
            snapshot(10, id=1), snapshot(12, id=2),
            snapshot(20, yes=".08", no=".85", id=3),  # stop
            snapshot(22, yes=".04", no=".90", size=".5", id=4),  # too thin
            snapshot(24, yes=".025", no=".79", id=5),  # owned bid only
        ]
        result = replay(snapshots, {}, cfg, cheap_contract, keep_decisions=True)
        self.assertEqual(result.trades[0].reason, "STOP_LOSS")
        self.assertEqual(result.trades[0].exit.quote.average, Decimal(".025"))
        self.assertIn("INSUFFICIENT_FULL_SIZE_BIDS", result.decisions[3]["decision"].reason)

    def test_expiry_awaits_matching_official_result(self):
        start = snapshot(10, id=1)
        other = snapshot(920, ordinal=3, id=4)
        samples = [start, snapshot(12, id=2), snapshot(900, id=3), other]
        official = result_for(start, Side.NO, delay=60)
        wrong = replace(official, event=other.market.event)
        cfg = Policy()
        self.assertTrue(replay(samples, {start.ticker: wrong}, cfg, cheap_contract,
                               end_at=official.received).metrics["open_position"])
        report = replay(samples, {start.ticker: official}, cfg, cheap_contract, end_at=official.received)
        self.assertEqual(report.metrics["settlement_trades"], 1)
        self.assertEqual(report.metrics["resale_trades"], 0)
        self.assertEqual(report.trades[0].exit.quote.gross, 0)
        self.assertEqual(report.trades[0].exit.at, official.received)
        self.assertFalse(report.metrics["open_position"])
        self.assertEqual(len(list(stress_cases(lambda: iter(samples), cfg))), 5)


if __name__ == "__main__":
    unittest.main()
