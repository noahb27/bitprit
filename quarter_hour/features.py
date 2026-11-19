"""Point-in-time features built only from already recorded public observations."""

from math import log

from .domain import Side, Snapshot
from .policy import Offer
from .replay import SpotTrail

FEATURE_VERSION = "market-snapshot-v1"
SETTLEMENT_FEATURES = (
    "spot_strike_log", "seconds_remaining", "yes_mid", "spread",
    "yes_bid_share", "spot_return_15s", "spot_return_60s", "spot_volatility_60s",
)
RESALE_FEATURES = SETTLEMENT_FEATURES + (
    "quantity", "ask_per_contract", "cost_per_contract", "bid_per_contract", "sale_cash_per_contract",
    "fee_multiplier",
)


class FeatureCursor:
    def __init__(self):
        self.trail = SpotTrail()

    def observe(self, sample: Snapshot, policy) -> dict[str, float] | None:
        self.trail.update(sample, max_age=policy.max_spot_age)
        m, s, b = sample.market, sample.spot, sample.book
        at = sample.at
        if not m or not s or not b or not b.yes or not b.no or sample.gap:
            return None
        if (
            m.strike is None or m.strike_type not in ("greater", "greater_or_equal") or not m.rules.strip()
            or m.status != "active" or not m.opens <= at < m.closes
            or not 0 <= at - m.received <= policy.max_metadata_age or b.ticker != m.ticker or b.crossed
            or not b.observed_at <= b.received <= at or at - b.observed_at > policy.max_book_age
            or not s.source_at <= s.received <= at or at - s.source_at > policy.max_spot_age
        ):
            return None
        price = float(s.price)
        short = self.trail.trailing_return(at, 15, price)
        minute = self.trail.trailing_return(at, 60, price)
        volatility = self.trail.volatility(at)
        if short is None or minute is None or volatility is None:
            return None
        bid = b.yes[0][0]
        ask = b.asks(Side.YES)[0][0]
        depth_yes = sum(q for _, q in b.yes[:3])
        depth_no = sum(q for _, q in b.no[:3])
        return dict(zip(SETTLEMENT_FEATURES, (
            log(price / float(m.strike)), m.closes - at, float((bid + ask) / 2), float(ask - bid),
            float(depth_yes / (depth_yes + depth_no)), short, minute, volatility,
        )))


def resale_features(features: dict[str, float], offer: Offer, sample: Snapshot) -> dict[str, float]:
    buy = offer.buy
    mark = offer.details["immediate_exit"]
    return features | {
        "quantity": buy.quantity, "ask_per_contract": float(buy.average),
        "cost_per_contract": float(buy.cash / buy.quantity),
        "bid_per_contract": float(mark["price_per_contract"]),
        "sale_cash_per_contract": float(mark["net_dollars"] / buy.quantity),
        "fee_multiplier": float(sample.fees.multiplier),
    }
