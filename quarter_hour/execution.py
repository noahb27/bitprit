"""Chronological strategy and fill state machine shared by replay and the worker."""

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Callable

from .domain import Market, Result, Side, Snapshot, number
from .policy import Offer, Policy, Quote, entry_offer, exit_quote, quote_conditions


@dataclass(frozen=True)
class Identity:
    ticker: str
    event: str
    opens: float
    closes: float

    @classmethod
    def from_market(cls, market: Market) -> "Identity":
        return cls(market.ticker, market.event, market.opens, market.closes)

    def matches(self, sample: Snapshot) -> bool:
        return sample.market is not None and self == self.from_market(sample.market)


@dataclass(frozen=True)
class Intent:
    market: Identity
    side: Side
    buy: bool
    quantity: int
    at: float
    ready: float
    deadline: float
    reason: str
    horizon: int | None
    policy: Policy
    entry_limit: Decimal | None
    signal_id: int

    @classmethod
    def create(cls, market, side, buy, size, at, reason, horizon, policy, signal_id, entry_limit=None):
        ready = at + policy.latency
        deadline = min(ready + policy.fill_window, market.closes - policy.exit_time_buffer)
        return cls(market, side, buy, size, at, ready, deadline, reason, horizon, policy, entry_limit, signal_id)

    @classmethod
    def load(cls, data):
        return cls(**(data | {
            "market": Identity(**data["market"]), "side": Side(data["side"]), "policy": Policy(**data["policy"]),
            "entry_limit": number(data["entry_limit"]) if data["entry_limit"] is not None else None,
        }))


@dataclass(frozen=True)
class Fill:
    sample_id: int
    at: float
    quote: Quote

    @classmethod
    def load(cls, data):
        return cls(data["sample_id"], data["at"], Quote.load(data["quote"]))


@dataclass(frozen=True)
class Position:
    order: Intent
    entry: Fill

    @classmethod
    def load(cls, data):
        return cls(Intent.load(data["order"]), Fill.load(data["entry"]))


@dataclass(frozen=True)
class Trade:
    position: Position
    exit: Fill
    outcome: str
    reason: str
    market_close: float | None = None

    @property
    def exposure_seconds(self) -> float:
        close = self.market_close if self.market_close is not None else self.position.order.market.closes
        return max(0, min(self.exit.at, close) - self.position.entry.at)

    @property
    def pnl(self) -> Decimal:
        return self.exit.quote.cash - self.position.entry.quote.cash

    @classmethod
    def load(cls, data):
        return cls(
            Position.load(data["position"]), Fill.load(data["exit"]),
            data["outcome"], data["reason"], data.get("market_close"),
        )


@dataclass(frozen=True)
class State:
    intent: Intent | None = None
    position: Position | None = None
    last_entry_ticker: str | None = None

    @classmethod
    def load(cls, data):
        return cls(
            Intent.load(data["intent"]) if data["intent"] else None,
            Position.load(data["position"]) if data["position"] else None,
            data["last_entry_ticker"],
        )


@dataclass(frozen=True)
class Advice:
    side: Side | None
    horizon: int | None
    reason: str
    detail: dict = field(default_factory=dict)


Advisor = Callable[[Snapshot, dict[Side, Offer], Policy, float], Advice]


@dataclass(frozen=True)
class Decision:
    action: str
    status: str
    reason: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    state: State
    decision: Decision
    fill: Fill | None = None
    trade: Trade | None = None


def attempt_fill(
    order: Intent, sample: Snapshot, current_policy: Policy, *, at: float,
) -> tuple[Fill | None, str, dict]:
    """Recheck market, book and costs on the first eligible fill observation."""
    clock = {"ready_at": order.ready, "deadline": order.deadline, "at": at,
             "order_ticker": order.market.ticker, "ticker": sample.ticker, "signal_sample": order.signal_id}
    if sample.gap:
        return None, "CANCELLED_COLLECTION_GAP", clock
    if at > order.deadline or at >= order.market.closes - order.policy.exit_time_buffer:
        return None, "ORDER_EXPIRED", clock
    if not order.market.matches(sample):
        return None, "WAIT_MATCHING_MARKET", clock
    if at < order.ready or not sample.book or sample.book.observed_at < order.ready:
        return None, "WAIT_FRESH_BOOK_AFTER_LATENCY", clock
    if order.buy:
        # A lowered budget applies even to an entry submitted before restart.
        policy = replace(order.policy, entry_cap=min(order.policy.entry_cap, current_policy.entry_cap),
                         max_contracts=min(order.policy.max_contracts, current_policy.max_contracts))
        offer = entry_offer(sample, order.side, policy, at=at, horizon=order.horizon,
                            quantity=order.quantity, after_entry=True)
        trace = clock | offer.details
        if not offer.buy:
            return None, "WAIT_" + "|".join(offer.failures), trace
        if order.entry_limit is None or offer.buy.cash > order.entry_limit:
            return None, "WAIT_ENTRY_COST_ABOVE_SIGNAL_LIMIT", trace | {"limit_dollars": order.entry_limit}
        return Fill(sample.id, at, offer.buy), "FILLED", trace
    quote, failures, detail = exit_quote(sample, order.side, order.quantity, order.policy, at=at)
    return (Fill(sample.id, at, quote), "FILLED", clock | detail) if quote else (
        None, "WAIT_" + "|".join(failures), clock | detail,
    )


def settlement(position: Position, result: Result | None, at: float) -> Trade | None:
    close = result.verified_close(position.order.market, known_at=at) if result else None
    if close is None or position.entry.at >= close:
        return None
    qty = position.entry.quote.quantity
    gross = Decimal(qty if position.order.side == result.side else 0)
    fill = Fill(0, result.received, Quote(qty, gross, Decimal(0), Decimal(0), (), False))
    return Trade(position, fill, "settlement", "CARRIED_TO_OFFICIAL_SETTLEMENT", close)


def _pending(state, sample, policy, at):
    order = state.intent
    action = f"BUY {order.side.upper()}" if order.buy else "SELL"
    fill, status, detail = attempt_fill(order, sample, policy, at=at)
    if fill:
        if order.buy:
            opened = State(position=Position(order, fill), last_entry_ticker=order.market.ticker)
            return Step(opened, Decision(action, "FILLED", order.reason, detail), fill)
        trade = Trade(state.position, fill, "resale", order.reason)
        closed = replace(state, intent=None, position=None)
        return Step(closed, Decision(action, "FILLED", order.reason, detail | {"net_pnl": trade.pnl}), fill, trade)
    next_state = state
    if status.startswith(("CANCELLED", "ORDER_EXPIRED")):
        next_state = replace(state, intent=None)
    return Step(next_state, Decision("HOLD" if state.position else "WAIT", "ORDER_PENDING" if next_state.intent else status,
                                     status, detail))


def _manage(state, sample, results, policy, at):
    position = state.position
    order = position.order
    market, original, entry = order.market, order.policy, position.entry
    trade = settlement(position, results.get(market.ticker), at)
    if trade:
        return Step(replace(state, position=None, intent=None), Decision(
            "SETTLE", "SETTLED", trade.reason, {"net_pnl": trade.pnl, "ticker": market.ticker},
        ), trade.exit, trade)
    if at >= market.closes - original.exit_time_buffer:
        return Step(replace(state, intent=None), Decision(
            "HOLD", "AWAITING_SETTLEMENT", "EXIT_WINDOW_ENDED_AWAIT_OFFICIAL_RESULT",
            {"ticker": market.ticker, "closes_at": market.closes, "remaining_seconds": market.closes - at},
        ))
    if state.intent:
        return _pending(state, sample, policy, at)
    if order.horizon is None:
        return Step(state, Decision("HOLD", "OPEN", "SETTLEMENT_ONLY_POLICY", {"ticker": market.ticker}))
    if not market.matches(sample):
        return Step(state, Decision("HOLD", "OPEN", "WAIT_MATCHING_MARKET", {"ticker": market.ticker}))
    quote, failures, detail = exit_quote(sample, order.side, order.quantity, original, at=at)
    scheduled = entry.at + order.horizon
    desired = min(scheduled, market.closes - original.required(None))
    detail.update({
        "scheduled_exit_at": desired, "entry_cost": entry.quote.cash,
        "profit_target_dollars": original.profit_target * order.quantity,
        "stop_loss_dollars": original.stop_loss * order.quantity,
    })
    trigger = None
    reason = "PROFIT_STOP_AND_TIME_NOT_TRIGGERED"
    if at >= desired:
        reason = "FIXED_HORIZON" if scheduled == desired else "PRE_CLOSE_EXIT"
        # A timed exit is scheduled at entry. If the window was missed, a new
        # order incurs latency from the current observation.
        trigger = at if sample.gap or at > desired + original.leg_time else desired
    elif quote:
        net = quote.cash - entry.quote.cash
        detail["current_net_pnl"] = net
        if net <= -detail["stop_loss_dollars"]:
            trigger, reason = at, "STOP_LOSS"
        elif net >= detail["profit_target_dollars"]:
            trigger, reason = at, "PROFIT_TARGET"
    if trigger is None:
        return Step(state, Decision("HOLD", "OPEN", "|".join(failures) or reason, detail))
    intent = Intent.create(market, order.side, False, order.quantity, trigger, reason,
                           order.horizon, original, sample.id)
    pending = replace(state, intent=intent)
    if trigger < at:
        step = _pending(pending, sample, policy, at)
        return replace(step, decision=replace(step.decision, detail=step.decision.detail | {"timed_exit_started": True}))
    return Step(pending, Decision("SELL", "SUBMITTED", reason, detail | {"ready_at": intent.ready}))


def advance(
    state: State, sample: Snapshot, results: dict[str, Result], policy: Policy,
    advisor: Advisor, *, at: float | None = None,
) -> Step:
    at = sample.at if at is None else max(sample.at, at)
    if state.position:
        return _manage(state, sample, results, policy, at)
    if state.intent:
        return _pending(state, sample, policy, at)
    offers = {side: entry_offer(sample, side, policy, at=at) for side in Side}
    detail = {
        "sides": {side: offer.details | {"policy_failures": offer.failures} for side, offer in offers.items()},
    }
    if sample.ticker is not None and sample.ticker == state.last_entry_ticker:
        return Step(state, Decision("WAIT", "NO_ENTRY", "ALREADY_ENTERED_THIS_MARKET", detail))
    choice = advisor(sample, offers, policy, at)
    detail.update(choice.detail)
    if choice.side is None:
        return Step(state, Decision("WAIT", "NO_ENTRY", choice.reason, detail))
    if not isinstance(choice.side, Side) or (choice.horizon is not None and choice.horizon not in (30, 60, 120)):
        return Step(state, Decision("WAIT", "NO_ENTRY", "INVALID_ADVICE", detail))
    offer = entry_offer(sample, choice.side, policy, at=at, horizon=choice.horizon)
    if offer.buy is None:
        return Step(state, Decision("WAIT", "POLICY_BLOCKED", "|".join(offer.failures), detail | {"selected": offer.details}))
    intent = Intent.create(
        Identity.from_market(sample.market), choice.side, True, offer.buy.quantity, at, choice.reason,
        choice.horizon, policy, sample.id, offer.buy.cash,
    )
    return Step(replace(state, intent=intent), Decision(
        f"BUY {choice.side.upper()}", "SUBMITTED", choice.reason,
        detail | {"selected": offer.details, "ready_at": intent.ready},
    ))


def no_trade(sample, offers, policy, at):
    return Advice(None, None, "NO_TRADE_BASELINE")


def cheap_contract(sample, offers, policy, at):
    choices = [(offer.buy.average, side) for side, offer in offers.items() if offer.buy]
    if not choices:
        return Advice(None, 60, "NO_EXECUTABLE_ENTRY")
    price, side = min(choices)
    info = {"ask_per_contract": price, "cheap_baseline_ceiling": Decimal("0.35")}
    if price > info["cheap_baseline_ceiling"]:
        return Advice(None, 60, "CHEAP_BASELINE_PRICE_LIMIT", info)
    return Advice(side, 60, "CHEAP_BASELINE", info)
