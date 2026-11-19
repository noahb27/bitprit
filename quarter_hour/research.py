"""Dataset preparation, holdout evaluation and benchmark comparisons."""

from collections import defaultdict
from statistics import mean
from time import time

from .dataset import HORIZONS, Periods, evidence, make_dataset
from .domain import json_text, number
from .execution import cheap_contract, no_trade
from .features import FeatureCursor
from .fit import PHASES, binary_metrics, regression_metrics, train_models, unchanged_bid
from .forecast import STRATEGIES, choose, forecast, resale_distribution, settlement_probability
from .policy import Policy
from .registry import check_unused_holdout, evaluation, load_artifact, save_artifact
from .replay import replay, stress_cases

HOLDOUT_SETTLEMENT_MARKETS = 16
HOLDOUT_RESALE_MARKETS = 12
HOLDOUT_DAYS = 3
MIN_OFFLINE_TRADES = 12
MIN_FORWARD_TRADES = 20
MIN_FORWARD_DAYS = 5


def train(store, periods: Periods, policy: Policy) -> dict:
    with store.transaction(write=False):
        check_unused_holdout(store, *periods.window("holdout"))
        results = store.results(until=periods.window("holdout")[0])
        data = {}
        for phase in PHASES:
            start, end = periods.window(phase)
            data[phase] = make_dataset(store.snapshots(since=start, until=end), results, policy,
                                       start, end)
    artifact, report = train_models(data, periods, policy)
    if artifact is None:
        return report
    with store.transaction():
        check_unused_holdout(store, *periods.window("holdout"))
        identifier = save_artifact(store, artifact)
    return report | {"artifact_id": identifier}


def advisors(features, artifact, *, stress=False) -> dict:
    def model_advisor(strategy):
        def advise(sample, offers, policy, at):
            predicted = forecast(sample, features.get(sample.id), artifact, policy, at=at, stress=stress)
            return choose(predicted, offers, policy, strategy)

        return advise

    result = {"no_trade": no_trade, "cheap_contract": cheap_contract}
    if artifact is not None:
        result.update({strategy: model_advisor(strategy) for strategy in STRATEGIES})
    return result


def replay_many(read, results, policy, *, artifact=None, with_stress=False, end=None) -> dict:
    def run(source, settings, stress):
        current_features = {}
        metrics = {}
        for name, advisor in advisors(current_features, artifact, stress=stress).items():
            if name in STRATEGIES:
                cursor = FeatureCursor()

                def watch():
                    for sample in source():
                        current_features.clear()
                        current_features[sample.id] = cursor.observe(sample, settings)
                        yield sample

                samples = watch()
            else:
                samples = source()
            metrics[name] = replay(samples, results, settings, advisor, end_at=end).metrics
        return metrics

    reports = run(read, policy, False)
    if not with_stress:
        return reports
    scenarios = {name: run(source, alternative, True) for name, source, alternative in stress_cases(read, policy)}
    return {"base": reports, "stress": scenarios}


def _holdout_coverage(data) -> dict:
    counts = {"settlement": evidence(data.settlement), "resale": {}}
    missing = []
    if counts["settlement"]["markets"] < HOLDOUT_SETTLEMENT_MARKETS or counts["settlement"]["days"] < HOLDOUT_DAYS:
        missing.append(f"settlement needs {HOLDOUT_SETTLEMENT_MARKETS} labeled markets across {HOLDOUT_DAYS} UTC days")
    if {row.targets[0] for row in data.settlement} != {0., 1.}:
        missing.append("settlement needs both official outcomes")
    for side in ("yes", "no"):
        for horizon in HORIZONS:
            name = f"{side}_{horizon}"
            group = data.resale.get(name, [])
            head = evidence(group)
            outcomes = data.coverage["resale_outcomes"].get(name, {})
            attempts = sum(value for key, value in outcomes.items() if key != "no_entry_offer")
            head["labeled_fraction"] = len(group) / attempts if attempts else 0
            counts["resale"][name] = head
            if head["markets"] < HOLDOUT_RESALE_MARKETS or head["days"] < HOLDOUT_DAYS or head["labeled_fraction"] < .6:
                missing.append(f"{name} needs {HOLDOUT_RESALE_MARKETS} labeled markets across {HOLDOUT_DAYS} UTC days and 60% fill coverage")
    return {"missing": missing, "counts": counts, "coverage": data.coverage}


def basis_diagnostic(samples, results) -> dict:
    points = defaultdict(dict)
    invalid = set()
    closes = {}
    for sample in samples:
        market, spot = sample.market, sample.spot
        if not market or not market.closes - 60 <= sample.at < market.closes:
            continue
        closes[market.ticker] = market.closes
        if sample.gap:
            invalid.add(market.ticker)
        if spot and market.closes - 60 <= spot.source_at <= spot.received <= sample.at:
            points[market.ticker][spot.source_at] = float(spot.price)
    differences = []
    for ticker, result in results.items():
        if ticker not in closes or ticker in invalid or result.benchmark_value is None:
            continue
        observed = sorted(points[ticker])
        if len(observed) < 4:
            continue
        bounds = [closes[ticker] - 60, *observed, closes[ticker]]
        if any(b - a > 15 for a, b in zip(bounds, bounds[1:])):
            continue
        proxy = mean(points[ticker].values())
        official = float(result.benchmark_value)
        differences.append((proxy / official - 1) * 10_000)
    return {
        "description": "Sampled final-minute Coinbase mean versus Kalshi's reported expiration value. The proxy uses only recorded observations.",
        "matched_markets": len(differences), "mean_signed_basis_bps": mean(differences) if differences else None,
        "mean_absolute_basis_bps": mean(abs(x) for x in differences) if differences else None,
    }


def _offline_gates(settlement, resale, policies):
    combined = policies["combined"]
    interval = combined["mean_daily_pnl_interval_95"]
    conditions = {
        "settlement_beats_midpoint_and_base_rate": settlement["frozen"]["brier"] < min(
            settlement["market_midpoint"]["brier"], settlement["training_base_rate"]["brier"]),
        "net_tail_coverage_and_error": all(
            metrics["net_return"]["frozen"]["interval_coverage"] >= .65
            and metrics["net_return"]["frozen"]["mae"] <= metrics["net_return"]["unchanged_executable_bid"]["mae"]
            for metrics in resale.values()
        ),
        "trade_sample": combined["closed_trades"] >= MIN_OFFLINE_TRADES and not combined["open_position"],
        "net_beats_fixed_rule_and_no_trade": number(combined["net_pnl"]) > max(0, number(policies["cheap_contract"]["net_pnl"])),
        "positive_mean_utc_day_lower_bound": interval is not None and number(interval[0]) > 0,
    }
    return {"eligible_for_forward_trial": all(conditions.values()), "checks": conditions}


def _holdout_metrics(identifier, artifact, periods, policy, data, coverage, read, results, end):
    settlement = {
        "frozen": binary_metrics(data.settlement, [settlement_probability(artifact["settlement"], r.features)
                                                   for r in data.settlement]),
        "training_base_rate": binary_metrics(data.settlement, [artifact["training_base_rate"] for _ in data.settlement]),
        "market_midpoint": binary_metrics(data.settlement, [r.features["yes_mid"] for r in data.settlement]),
    }
    resale = {}
    for key, rows in data.resale.items():
        predicted = [resale_distribution(artifact["resale"][key], row.features) for row in rows]
        resale[key] = {}
        for index, target in enumerate(("exit_value", "net_return")):
            resale[key][target] = {
                "frozen": regression_metrics(rows, [p[target] for p in predicted], index),
                "unchanged_executable_bid": regression_metrics(rows, [unchanged_bid(row, index) for row in rows], index),
            }
    policies = replay_many(read, results, policy, artifact=artifact, end=end)
    return coverage | {
        "status": "HOLDOUT_GRADED", "artifact_id": identifier, "holdout": [periods.holdout_from, periods.holdout_until],
        "dataset_digest": data.digest, "settlement": settlement, "fixed_horizon_resale": resale,
        "policy_replay": policies, "spot_benchmark_basis": basis_diagnostic(read(), results),
        "gates": _offline_gates(settlement, resale, policies),
    }


def grade_holdout(store, identifier: str) -> dict:
    saved = evaluation(store, identifier)
    if saved:
        return saved[1]
    artifact = load_artifact(store, identifier)
    if artifact["status"] != "CHALLENGER":
        raise ValueError("holdout grading requires both objectives and all six resale heads")
    periods = Periods(**artifact["periods"])
    policy = Policy(**artifact["policy"])
    start, end = periods.window("holdout")
    if time() < end:
        raise ValueError("the declared holdout has not ended")
    with store.transaction(write=False):
        check_unused_holdout(store, start, end)
        results = store.results(until=end)
        data = make_dataset(store.snapshots(since=start, until=end), results, policy, start, end)
        coverage = _holdout_coverage(data)
        if coverage["missing"]:
            return coverage | {"status": "INSUFFICIENT_HOLDOUT", "artifact_id": identifier}

        def read():
            for sample in store.snapshots(since=start, until=end):
                if sample.market is None or start <= sample.market.opens < sample.market.closes <= end:
                    yield sample

        report = _holdout_metrics(identifier, artifact, periods, policy, data, coverage, read, results, end)
    with store.transaction():
        saved = evaluation(store, identifier)
        if saved:
            return saved[1]
        check_unused_holdout(store, start, end)
        store.db.execute("INSERT INTO evaluations VALUES (?,?,?,?,?)", (identifier, time(), start, end, json_text(report)))
    return evaluation(store, identifier)[1]
