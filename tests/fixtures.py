"""Synthetic market data for tests only."""

from datetime import datetime, timezone
from decimal import Decimal

from quarter_hour.collector import SERIES
from quarter_hour.domain import Book, Fees, Market, Result, Side, Snapshot, Spot

START = 1_735_689_600


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def snapshot(offset=10, *, start=START, ordinal=0, yes="0.20", no="0.74", size="20", id=1):
    now = start + offset
    event = f"{SERIES}-EXAMPLE{ordinal}"
    key = event + "-M"
    market = Market(
        key, event, SERIES, start, start + 900, now, start, "active", Decimal("100000"),
        "greater_or_equal", "Synthetic contract terms for tests only", "", Decimal(1), False, None,
    )
    book = Book(
        key, now - .1, now - .05, None,
        ((Decimal(yes), Decimal(size)),), ((Decimal(no), Decimal(size)),),
    )
    return Snapshot(
        now, market, book, Spot(Decimal("100001"), now - .5, now - .2),
        Fees("quadratic", Decimal(1), now, event, "https://example.invalid/test", ("Fake index",)),
        id=id,
    )


def result_for(sample, side=Side.YES, *, delay=60):
    market = sample.market
    return Result(
        market.ticker, market.event, side, market.closes + delay, market.closes + 30, Decimal("100050"),
    )
