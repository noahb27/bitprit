"""Independent paper-execution guards and Decimal cost arithmetic."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

from .domain import Book, Fees, Side, Snapshot, elapsed, number, seconds

HARD_CAP = Decimal("5")
FEE_MODEL = "quadratic-taker-per-level-v1"
QUADRATIC_TYPES = ("quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Policy:
    entry_cap: Decimal = HARD_CAP
    max_contracts: int = 3
    slippage: Decimal = Decimal("0.01")
    fee_scale: Decimal = Decimal(1)
    latency: float = 1.0
    fill_window: float = 8.0
    exit_time_buffer: float = 15.0
    max_book_age: float = 3.0
    max_spot_age: float = 10.0
    max_metadata_age: float = 40.0
    max_spread: Decimal = Decimal("0.12")
    profit_target: Decimal = Decimal("0.06")
    stop_loss: Decimal = Decimal("0.12")
    min_net_edge: Decimal = Decimal("0.02")
    probability_buffer: Decimal = Decimal("0.05")

    def __post_init__(self):
        for name, lo, hi in (
            ("entry_cap", 0, HARD_CAP), ("slippage", 0, .25), ("fee_scale", 1, 10),
            ("max_spread", 0, 1), ("profit_target", 0, 1), ("stop_loss", 0, 1),
            ("min_net_edge", 0, 1), ("probability_buffer", 0, .5),
        ):
            value = number(getattr(self, name))
            if not number(lo) <= value <= number(hi):
                raise ValueError(f"{name} must be between {lo} and {hi}")
            object.__setattr__(self, name, value)
        if type(self.max_contracts) is not int or not 1 <= self.max_contracts <= 500:
            raise ValueError("max_contracts must be a whole number from 1 to 500")
        if self.entry_cap == 0 or self.profit_target == 0 or self.stop_loss == 0:
            raise ValueError("entry_cap, profit_target and stop_loss must be positive")
        for name, lo, hi in (
            ("latency", .001, 60), ("fill_window", .001, 60), ("exit_time_buffer", 0, 180),
            ("max_book_age", .001, 30), ("max_spot_age", .001, 120), ("max_metadata_age", .001, 300),
        ):
            object.__setattr__(self, name, seconds(getattr(self, name), name, lo, hi))

    @property
    def leg_time(self) -> float:
        return self.latency + self.fill_window

    def required(self, horizon: int | None, *, after_entry: bool = False) -> float:
        buy_time = 0 if after_entry else self.leg_time
        hold_time = 0 if horizon is None else horizon + self.leg_time
        return buy_time + hold_time + self.exit_time_buffer


@dataclass(frozen=True)
class Quote:
    quantity: int
    gross: Decimal
    fee: Decimal
    slippage: Decimal
    levels: tuple[tuple[Decimal, Decimal], ...]
    buy: bool

    @property
    def average(self) -> Decimal:
        return self.gross / self.quantity

    @property
    def cash(self) -> Decimal:
        return self.gross + self.fee + self.slippage if self.buy else self.gross - self.fee - self.slippage

    def details(self) -> dict:
        return {
            "quantity": self.quantity, "gross": self.gross, "fee": self.fee,
            "slippage": self.slippage, "levels": self.levels,
            "price_per_contract": self.average, "net_dollars": self.cash,
        }

    @classmethod
    def load(cls, data: dict) -> "Quote":
        return cls(data["quantity"], number(data["gross"]), number(data["fee"]), number(data["slippage"]),
                   tuple((number(p), number(q)) for p, q in data["levels"]), data["buy"])


@dataclass(frozen=True)
class Offer:
    buy: Quote | None
    failures: tuple[str, ...]
    details: dict


def sweep(book: Book, fees: Fees, side: Side, quantity: int, policy: Policy, *, buy: bool) -> Quote | None:
    """Walk price levels and reject an incomplete fill."""
    if type(quantity) is not int or quantity <= 0:
        return None
    if fees.type not in QUADRATIC_TYPES or not 0 <= fees.multiplier <= 100:
        return None
    levels = book.asks(side) if buy else book.bids(side)
    left = Decimal(quantity)
    gross = fee = Decimal(0)
    fills = []
    factor = Decimal("0.07") * fees.multiplier * policy.fee_scale
    for price, depth in levels:
        if not 0 < price < 1 or depth <= 0:
            return None
        take = min(left, depth)
        gross += take * price
        fee += (factor * take * price * (1 - price)).quantize(CENT, rounding=ROUND_CEILING)
        fills.append((price, take))
        left -= take
        if not left:
            slippage = policy.slippage * quantity
            return Quote(quantity, gross, fee, slippage if buy else min(slippage, gross), tuple(fills), buy)
    return None


def quote_conditions(sample: Snapshot, policy: Policy, *, at: float, entry: bool) -> tuple[list[str], dict]:
    m, b, s, f = sample.market, sample.book, sample.spot, sample.fees
    info = {
        "at": at, "sample_id": sample.id, "ticker": sample.ticker, "data_flags": sample.flags,
        "book_age_seconds": elapsed(at, b.observed_at if b else None),
        "book_time_basis": ("exchange_or_request_start" if b.source_at is not None else "request_start")
        if b and b.requested is not None else "unavailable",
        "book_receipt_age_seconds": elapsed(at, b.received if b else None),
        "spot_source_age_seconds": elapsed(at, s.source_at if s else None),
        "metadata_age_seconds": elapsed(at, m.received if m else None),
        "fee_metadata_age_seconds": elapsed(at, f.received if f else None),
        "remaining_seconds": elapsed(m.closes, at) if m else None,
        "exit_time_buffer_seconds": policy.exit_time_buffer, "max_book_age_seconds": policy.max_book_age,
        "max_spot_age_seconds": policy.max_spot_age, "max_metadata_age_seconds": policy.max_metadata_age,
        "fee_model": FEE_MODEL, "fee_type": f.type if f else None, "fee_multiplier": f.multiplier if f else None,
    }
    failures = []
    if not m:
        failures.append("MARKET_MISSING")
    elif m.series != "KXBTC15M" or m.notional != 1 or m.can_close_early:
        failures.append("UNSUPPORTED_MARKET")
    elif m.status != "active" or not m.opens <= at < m.closes - policy.exit_time_buffer:
        failures.append("OUTSIDE_TRADING_WINDOW")
    elif not 0 <= at - m.received <= policy.max_metadata_age or (m.updated is not None and m.updated > m.received):
        failures.append("MARKET_METADATA_STALE_OR_FUTURE")

    if not b or not m or b.ticker != m.ticker:
        failures.append("MATCHING_BOOK_MISSING")
    elif b.requested is None:
        failures.append("BOOK_REQUEST_TIME_UNAVAILABLE")
    elif not (b.requested <= b.received <= sample.at <= at and 0 <= at - b.observed_at <= policy.max_book_age):
        failures.append("BOOK_STALE_OR_FUTURE")
    elif b.crossed:
        failures.append("CROSSED_OR_LOCKED_BOOK")
    elif entry and (not b.yes or not b.no):
        failures.append("TWO_SIDED_DEPTH_REQUIRED")

    if not f or not m or f.event != m.event:
        failures.append("MATCHING_FEE_METADATA_MISSING")
    elif not 0 <= at - f.received <= policy.max_metadata_age:
        failures.append("FEE_METADATA_STALE_OR_FUTURE")
    elif f.type not in QUADRATIC_TYPES or not 0 <= f.multiplier <= 100:
        failures.append("UNSUPPORTED_FEE_SCHEDULE")
    if m and m.fee_waiver_until is not None and at < m.fee_waiver_until:
        failures.append("UNSUPPORTED_FEE_WAIVER")

    if entry:
        if not s:
            failures.append("SPOT_MISSING")
        elif not 0 <= at - s.source_at <= policy.max_spot_age or not s.source_at <= s.received <= sample.at:
            failures.append("SPOT_STALE_OR_FUTURE")
        if m and (m.strike is None or not m.rules.strip() or m.strike_type not in ("greater", "greater_or_equal")):
            failures.append("TARGET_OR_RULES_UNSUPPORTED_OR_MISSING")
        if sample.gap:
            failures.append("COLLECTION_GAP")
    return failures, info


def entry_offer(
    sample: Snapshot, side: Side, policy: Policy, *, at: float, horizon: int | None = None,
    quantity: int | None = None, after_entry: bool = False,
) -> Offer:
    failures, info = quote_conditions(sample, policy, at=at, entry=True)
    required = policy.required(horizon, after_entry=after_entry)
    info.update({"required_seconds": required, "horizon_seconds": horizon, "entry_budget": policy.entry_cap})
    if sample.market and sample.market.closes - at <= required:
        failures.append("INSUFFICIENT_TIME")
    if failures:
        return Offer(None, tuple(failures), info)
    assert sample.book is not None and sample.fees is not None
    b, f = sample.book, sample.fees
    info["ask_depth"] = sum(qty for _, qty in b.asks(side))
    info["bid_depth"] = sum(qty for _, qty in b.bids(side))
    if quantity is not None and (type(quantity) is not int or not 1 <= quantity <= policy.max_contracts):
        return Offer(None, ("INVALID_CONTRACT_QUANTITY",), info)
    limit = min(policy.entry_cap, HARD_CAP)
    for size in range(quantity or policy.max_contracts, (quantity or 1) - 1, -1):
        buy = sweep(b, f, side, size, policy, buy=True)
        sell = sweep(b, f, side, size, policy, buy=False)
        details = info.copy()
        issues = []
        if buy is None or sell is None:
            issues.append("INSUFFICIENT_FULL_SIZE_DEPTH")
        if buy:
            details["entry"] = buy.details()
            if buy.cash > limit:
                details["over_budget_by"] = buy.cash - limit
                issues.append("ENTRY_LIMIT_EXCEEDED")
        if buy and sell:
            spread = buy.average - sell.average
            details.update({"spread": spread, "spread_limit": policy.max_spread,
                         "immediate_exit": sell.details(), "immediate_net": sell.cash - buy.cash})
            if spread > policy.max_spread:
                issues.append("SPREAD_OVER_LIMIT")
        if not issues:
            return Offer(buy, (), details)
    return Offer(None, tuple(issues), details)


def exit_quote(sample: Snapshot, side: Side, quantity: int, policy: Policy, *, at: float) -> tuple[Quote | None, tuple[str, ...], dict]:
    failures, info = quote_conditions(sample, policy, at=at, entry=False)
    if failures:
        return None, tuple(failures), info
    assert sample.book is not None and sample.fees is not None
    quote = sweep(sample.book, sample.fees, side, quantity, policy, buy=False)
    if quote is None:
        info["bid_depth"] = sum(qty for _, qty in sample.book.bids(side))
        return None, ("INSUFFICIENT_FULL_SIZE_BIDS",), info
    info["exit_quote"] = quote.details()
    return quote, (), info
