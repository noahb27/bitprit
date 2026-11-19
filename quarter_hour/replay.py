"""Replay public snapshots and summarize simulated execution."""

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from math import log, sqrt
from random import Random
from statistics import mean

from .domain import Result, Snapshot
from .execution import Advisor, State, Trade, advance, settlement
from .policy import FEE_MODEL, Policy


def utc_day(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).date().isoformat()


def quantile(values: list, q: float):
    ordered = sorted(values)
    if not ordered:
        return None
    index = (len(ordered) - 1) * q
    lower = int(index)
    weight = index - lower
    high = ordered[min(lower + 1, len(ordered) - 1)]
    if isinstance(high, Decimal):
        weight = Decimal(str(weight))
    return ordered[lower] + weight * (high - ordered[lower])


def day_interval(values: list) -> list | None:
    if len(values) < 2:
        return None
    rng = Random(2718)
    means = [mean(rng.choices(values, k=len(values))) for _ in range(1000)]
    return [quantile(means, .025), quantile(means, .975)]


class SpotTrail:
    def __init__(self):
        self.points = deque()
        self.last_source = None

    def update(self, sample: Snapshot, *, max_age: float = 10):
        if sample.gap:
            self.points.clear()
            self.last_source = None
        while self.points and self.points[0][0] < sample.at - 125:
            self.points.popleft()
        s = sample.spot
        if s and s.source_at <= s.received <= sample.at and 0 <= sample.at - s.source_at <= max_age:
            if self.last_source is None or s.source_at > self.last_source:
                self.points.append((sample.at, float(s.price)))
                self.last_source = s.source_at

    def volatility(self, at: float) -> float | None:
        points = [(seen, price) for seen, price in self.points if seen >= at - 60]
        if len(points) < 3 or points[-1][0] - points[0][0] < 10:
            return None
        pairs = list(zip(points, points[1:]))
        if any(after[0] - before[0] > 15 for before, after in pairs):
            return None
        returns = sum(log(after[1] / before[1]) ** 2 for before, after in pairs)
        return sqrt(60 * returns / (points[-1][0] - points[0][0]))

    def trailing_return(self, at: float, seconds: int, price: float) -> float | None:
        for seen, past in reversed(self.points):
            if seen <= at - seconds:
                return log(price / past) if seen >= at - seconds - 8 else None
        return None


@dataclass
class Replay:
    trades: list[Trade]
    state: State
    metrics: dict
    decisions: list[dict]


def replay(
    snapshots, results: dict[str, Result], policy: Policy, advisor: Advisor,
    *, keep_decisions: bool = False, end_at: float | None = None,
) -> Replay:
    state = State()
    stats = Counter()
    waits = Counter()
    days, markets = set(), set()
    trades, decisions = [], []
    trail = SpotTrail()
    volatility = {}
    first_at = last_at = None
    for sample in snapshots:
        if last_at is not None and sample.at <= last_at:
            raise ValueError("replay observations must be strictly chronological")
        first_at = sample.at if first_at is None else first_at
        last_at = sample.at
        days.add(utc_day(sample.at))
        if sample.ticker:
            markets.add(sample.ticker)
        trail.update(sample, max_age=policy.max_spot_age)
        step = advance(state, sample, results, policy, advisor)
        state = step.state
        action, status = step.decision.action, step.decision.status
        stats["observations"] += 1
        stats["collection_gaps"] += int(sample.gap)
        if status == "SUBMITTED":
            stats["entry_intents" if action.startswith("BUY") else "exit_intents"] += 1
        if step.decision.detail.get("timed_exit_started"):
            stats["exit_intents"] += 1
        if status == "FILLED":
            if action.startswith("BUY"):
                stats["entry_fills"] += 1
                volatility[sample.id] = trail.volatility(sample.at)
            else:
                stats["sale_fills"] += 1
        if action in ("WAIT", "HOLD"):
            waits[step.decision.reason] += 1
        if step.trade:
            trades.append(step.trade)
        if keep_decisions:
            decisions.append({"id": sample.id, "at": sample.at, "ticker": sample.ticker,
                              "decision": step.decision, "fill": step.fill})
    if end_at is not None and last_at is not None and end_at < last_at:
        raise ValueError("replay end precedes the last observation")
    if state.position and end_at is not None:
        terminal = settlement(state.position, results.get(state.position.order.market.ticker), end_at)
        if terminal:
            trades.append(terminal)
            state = replace(state, position=None, intent=None)
    report = trade_report(trades, days, volatility)
    open_seconds = 0
    if state.position and last_at is not None:
        open_seconds = max(0, min(last_at, state.position.order.market.closes) - state.position.entry.at)
    closed_seconds = sum(
        max(0, min(t.exit.at, t.position.order.market.closes) - t.position.entry.at) for t in trades
    )
    duration = None if first_at is None else last_at - first_at
    return Replay(trades, state, report | {
        "sample_start": first_at, "sample_end": last_at, "observed_days": len(days),
        "observed_markets": len(markets), "counts": dict(stats), "wait_reasons": dict(waits),
        "open_position": state.position is not None, "pending_order": state.intent is not None,
        "entry_fill_coverage": stats["entry_fills"] / stats["entry_intents"] if stats["entry_intents"] else None,
        "pre_close_exit_fraction": report["resale_trades"] / len(trades) if trades else None,
        "exposure_seconds_before_close": closed_seconds + open_seconds,
        "exposure_fraction": (closed_seconds + open_seconds) / duration if duration else None,
        "assumptions": {"policy": policy, "fee_model": FEE_MODEL,
                        "fill": "First eligible recorded depth after latency. Full size only.",
                        "exit_policies": "Observed profit and stop triggers or fixed horizon, with official settlement fallback."},
    }, decisions)


def trade_report(trades, days=None, volatility=None) -> dict:
    net = Decimal(0)
    peak = drawdown = Decimal(0)
    streak = worst_streak = 0
    wins, losses = [], []
    turnover = Decimal(0)
    subsets = {kind: defaultdict(list) for kind in ("day", "side", "entry_price", "volatility")}
    daily = {day: Decimal(0) for day in (days or ())}
    outcomes = {"resale": [], "settlement": []}
    for trade in trades:
        entry, order = trade.position.entry, trade.position.order
        pnl = trade.pnl
        net += pnl
        peak = max(peak, net)
        drawdown = max(drawdown, peak - net)
        streak = streak + 1 if pnl < 0 else 0
        worst_streak = max(streak, worst_streak)
        if pnl > 0:
            wins.append(pnl)
        elif pnl < 0:
            losses.append(pnl)
        turnover += entry.quote.gross
        if trade.outcome == "resale":
            turnover += trade.exit.quote.gross
        outcomes[trade.outcome].append(pnl)
        day = utc_day(entry.at)
        daily[day] = daily.get(day, Decimal(0)) + pnl
        price = entry.quote.average
        band = "under_0.25" if price < Decimal(".25") else "0.25_to_0.49" if price < Decimal(".50") else "0.50_plus"
        vol = (volatility or {}).get(entry.sample_id)
        vol_band = "unknown" if vol is None else "under_10bp" if vol < .001 else "10bp_plus"
        for kind, value in (("day", day), ("side", order.side), ("entry_price", band), ("volatility", vol_band)):
            subsets[kind][value].append(pnl)
    return {
        "closed_trades": len(trades), "winning_trades": len(wins), "losing_trades": len(losses),
        "net_pnl": net, "average_gain": mean(wins) if wins else None, "average_loss": mean(losses) if losses else None,
        "realized_max_drawdown": drawdown, "longest_losing_streak": worst_streak, "gross_turnover": turnover,
        "top_three_share_of_gross_wins": sum(sorted(wins, reverse=True)[:3]) / sum(wins) if wins else None,
        "resale_trades": len(outcomes["resale"]), "resale_net_pnl": sum(outcomes["resale"], Decimal(0)),
        "settlement_trades": len(outcomes["settlement"]), "settlement_net_pnl": sum(outcomes["settlement"], Decimal(0)),
        "mean_daily_pnl_interval_95": day_interval(list(daily.values())),
        "groups": {
            kind: {label: {"trades": len(pnls), "net_pnl": sum(pnls)} for label, pnls in values.items()}
            for kind, values in subsets.items()
        },
    }


def stress_cases(read, policy: Policy):
    """Stream deterministic perturbations from the identical replay selection."""
    def with_gaps():
        for index, sample in enumerate(read()):
            if index % 25 not in (6, 7, 8):
                yield replace(sample, gap=True) if index % 25 == 9 else sample

    def thin_books():
        for index, sample in enumerate(read()):
            if index % 5 == 0 and sample.book:
                book = replace(sample.book, yes=(), no=tuple((price, qty / 10) for price, qty in sample.book.no))
                sample = replace(sample, book=book)
            yield sample

    def stale_metadata():
        for index, sample in enumerate(read()):
            if index % 5 == 0 and sample.market:
                sample = replace(sample, market=replace(sample.market, received=sample.at - policy.max_metadata_age - 1))
            yield sample

    yield "higher_costs", read, replace(policy, fee_scale=min(10, policy.fee_scale * Decimal("1.5")),
                                        slippage=min(Decimal(".25"), policy.slippage + Decimal(".02")))
    yield "extra_latency", read, replace(policy, latency=min(60, policy.latency + 3))
    yield "missing_samples_and_restarts", with_gaps, policy
    yield "one_sided_thin_depth", thin_books, policy
    yield "stale_market_metadata", stale_metadata, policy
