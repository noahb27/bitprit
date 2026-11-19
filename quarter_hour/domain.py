"""Recorded market data. Epoch seconds are UTC and prices are in dollars."""

import json
import math
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum


class Side(StrEnum):
    YES = "yes"
    NO = "no"

    @property
    def other(self) -> "Side":
        return Side.NO if self == Side.YES else Side.YES


def number(value) -> Decimal:
    if not isinstance(value, (str, int, float, Decimal)) or isinstance(value, bool):
        raise ValueError("expected a number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if not result.is_finite():
        raise ValueError("non-finite decimal")
    return result


def positive(value) -> Decimal:
    result = number(value)
    if not 0 < result <= 1_000_000_000:
        raise ValueError("number is outside supported positive bounds")
    return result


def epoch(value: str) -> float:
    if not isinstance(value, str):
        raise ValueError("timestamp missing")
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("timestamp requires a timezone")
    return result.timestamp()


def elapsed(now: float, then: float | None) -> float | None:
    return None if then is None else round(now - then, 3)


def json_text(value) -> str:
    def encode(obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if is_dataclass(obj):
            return asdict(obj)
        raise TypeError(f"cannot serialize {type(obj).__name__}")

    return json.dumps(value, default=encode, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class Market:
    ticker: str
    event: str
    series: str
    opens: float
    closes: float
    received: float
    updated: float | None
    status: str
    strike: Decimal | None
    strike_type: str | None
    rules: str
    secondary_rules: str
    notional: Decimal
    can_close_early: bool
    fee_waiver_until: float | None
    status_requested: float | None = None


Level = tuple[Decimal, Decimal]


@dataclass(frozen=True)
class Book:
    ticker: str
    requested: float | None
    received: float
    source_at: float | None
    yes: tuple[Level, ...]
    no: tuple[Level, ...]

    @property
    def observed_at(self) -> float:
        if self.source_at is None or self.requested is None:
            return self.source_at if self.source_at is not None else self.requested or self.received
        return min(self.source_at, self.requested)

    @property
    def crossed(self) -> bool:
        return bool(self.yes and self.no and self.yes[0][0] + self.no[0][0] >= 1)

    def bids(self, side: Side) -> tuple[Level, ...]:
        return self.yes if side == Side.YES else self.no

    def asks(self, side: Side) -> tuple[Level, ...]:
        return tuple((Decimal(1) - price, qty) for price, qty in self.bids(side.other))


@dataclass(frozen=True)
class Spot:
    price: Decimal
    source_at: float
    received: float


@dataclass(frozen=True)
class Fees:
    type: str
    multiplier: Decimal
    received: float
    event: str
    terms_url: str
    settlement_sources: tuple[str, ...]


@dataclass(frozen=True)
class Result:
    ticker: str
    event: str
    side: Side
    received: float
    settled: float | None
    benchmark_value: Decimal | None
    market_close: float | None = None

    def verified_close(self, market, *, known_at: float) -> float | None:
        close = market.closes if self.market_close is None else self.market_close
        if (
            (self.ticker, self.event) == (market.ticker, market.event)
            and market.opens < close <= market.closes
            and close <= self.received <= known_at
            and (self.settled is None or close <= self.settled <= self.received)
        ):
            return close
        return None


@dataclass(frozen=True)
class Snapshot:
    at: float
    market: Market | None = None
    book: Book | None = None
    spot: Spot | None = None
    fees: Fees | None = None
    flags: tuple[str, ...] = ()
    gap: bool = False
    id: int = 0

    @property
    def ticker(self) -> str | None:
        return self.market.ticker if self.market else None

    @classmethod
    def load(cls, row: dict) -> "Snapshot":
        market, book, spot, fees = (row.get(key) for key in ("market", "book", "spot", "fees"))
        if market:
            market["strike"] = number(market["strike"]) if market["strike"] is not None else None
            market["notional"] = number(market["notional"])
        if book:
            for side in ("yes", "no"):
                book[side] = tuple((number(p), number(q)) for p, q in book[side])
        if spot:
            spot["price"] = number(spot["price"])
        if fees:
            fees["multiplier"] = number(fees["multiplier"])
            fees["settlement_sources"] = tuple(fees["settlement_sources"])
        return cls(
            row["at"], Market(**market) if market else None, Book(**book) if book else None,
            Spot(**spot) if spot else None, Fees(**fees) if fees else None,
            tuple(row["flags"]), row["gap"], row["id"],
        )


def seconds(value, name: str, lower: float, upper: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f"{name} must be between {lower} and {upper} seconds")
    return float(value)
