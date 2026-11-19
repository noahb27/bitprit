"""Chronological market groups and delayed, executable resale labels."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256

from .domain import Result, Side, Snapshot, json_text
from .execution import Identity, Intent, attempt_fill
from .features import FeatureCursor, resale_features
from .policy import Policy, entry_offer
from .replay import utc_day

HORIZONS = (30, 60, 120)
ANCHORS = (120, 300, 480, 660)
ANCHOR_TOLERANCE = 20
MAX_CONTINUITY_SECONDS = 15


@dataclass(frozen=True)
class Periods:
    training_from: str
    calibration_from: str
    selection_from: str
    holdout_from: str
    holdout_until: str

    def __post_init__(self):
        if any(a >= b for a, b in zip(self.boundaries, self.boundaries[1:])):
            raise ValueError("dates must be strictly ascending UTC days")

    @property
    def boundaries(self) -> tuple[float, ...]:
        return tuple(datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc).timestamp()
                     for day in (self.training_from, self.calibration_from, self.selection_from,
                                 self.holdout_from, self.holdout_until))

    def window(self, phase: str) -> tuple[float, float]:
        index = ("training", "calibration", "selection", "holdout").index(phase)
        return self.boundaries[index:index + 2]


@dataclass(frozen=True)
class Example:
    ticker: str
    day: str
    at: float
    features: dict[str, float]
    targets: tuple[float, ...]
    label_at: float


@dataclass
class Dataset:
    snapshots: list[Snapshot]
    features: dict[int, dict[str, float]]
    settlement: list[Example]
    resale: dict[str, list[Example]]
    coverage: dict
    digest: str


def head_key(side: Side, horizon: int) -> str:
    return f"{side.value}_{horizon}"


def _first_fill(order: Intent, samples, start: int, policy: Policy):
    previous = samples[start].at
    for index in range(start + 1, len(samples)):
        snapshot = samples[index]
        if snapshot.at > order.deadline:
            break
        if snapshot.gap or snapshot.at - previous > MAX_CONTINUITY_SECONDS:
            return None, index, "gap"
        previous = snapshot.at
        fill, _, _ = attempt_fill(order, snapshot, policy, at=snapshot.at)
        if fill:
            return fill, index, "filled"
    return None, start, "no_full_fill"


def delayed_resale(samples, index: int, side: Side, horizon: int, policy: Policy):
    """Label a prescheduled exit at entry fill plus horizon, plus execution latency."""
    sample = samples[index]
    offer = entry_offer(sample, side, policy, at=sample.at, horizon=horizon)
    if offer.buy is None:
        return None, "no_entry_offer"
    market = Identity.from_market(sample.market)
    size = offer.buy.quantity
    buy = Intent.create(market, side, True, size, sample.at, "label", horizon, policy, sample.id, offer.buy.cash)
    entry, entry_index, status = _first_fill(buy, samples, index, policy)
    if entry is None:
        return None, "entry_" + status
    sell = Intent.create(market, side, False, size, entry.at + horizon, "label", horizon, policy, sample.id)
    final, _, status = _first_fill(sell, samples, entry_index, policy)
    if final is None:
        return None, "exit_" + status
    targets = (float(final.quote.gross / size), float((final.quote.cash - entry.quote.cash) / size))
    return (offer, targets, final.at), "label"


def matched_label(result: Result | None, sample: Snapshot, cutoff: float) -> bool:
    m = sample.market
    return (
        result is not None and (result.ticker, result.event) == (m.ticker, m.event)
        and m.closes <= result.received < cutoff
        and (result.settled is None or m.closes <= result.settled <= result.received)
    )


def make_dataset(
    samples, results: dict[str, Result], policy: Policy, start: float, end: float, *, keep_timeline: bool = True,
) -> Dataset:
    timeline = [s for s in samples if start <= s.at < end]
    cursor = FeatureCursor()
    features = {}
    available = set()
    used = set()
    settlements = []
    resale = defaultdict(list)
    labels = defaultdict(Counter)
    digest = sha256()
    digest.update(json_text({"start": start, "end": end, "policy": policy}).encode())
    for result in sorted(results.values(), key=lambda result: result.ticker):
        if start <= result.received < end:
            digest.update(json_text(result).encode())
    anchors = ready = 0
    for index, sample in enumerate(timeline):
        digest.update(json_text(sample).encode())
        m = sample.market
        facts = cursor.observe(sample, policy)
        if not m or m.opens < start or m.closes > end:
            continue
        available.add(m.ticker)
        if facts is None:
            continue
        ready += 1
        if keep_timeline:
            features[sample.id] = facts
        anchor = next((offset for offset in ANCHORS if offset <= sample.at - m.opens <= offset + ANCHOR_TOLERANCE), None)
        if anchor is None or (m.ticker, anchor) in used:
            continue
        used.add((m.ticker, anchor))
        anchors += 1
        day = utc_day(m.opens)
        result = results.get(m.ticker)
        if matched_label(result, sample, end):
            settlements.append(Example(m.ticker, day, sample.at, facts, (float(result.side == Side.YES),), result.received))
        for side in Side:
            for horizon in HORIZONS:
                key = head_key(side, horizon)
                label, outcome = delayed_resale(timeline, index, side, horizon, policy)
                labels[key][outcome] += 1
                if label:
                    offer, targets, label_at = label
                    resale[key].append(Example(m.ticker, day, sample.at,
                                               resale_features(facts, offer, sample), targets, label_at))
    return Dataset(timeline if keep_timeline else [], features, settlements, dict(resale), {
        "observations": len(timeline), "whole_markets": len(available), "feature_ready": ready,
        "anchors": anchors, "settlement_labels": len(settlements),
        "resale_outcomes": {k: dict(v) for k, v in labels.items()},
    }, digest.hexdigest())


def market_weights(rows: list[Example]) -> list[float]:
    counts = Counter(row.ticker for row in rows)
    return [1 / counts[row.ticker] for row in rows]


def evidence(rows: list[Example]) -> dict:
    return {"samples": len(rows), "markets": len({row.ticker for row in rows}), "days": len({row.day for row in rows})}
