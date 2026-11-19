"""Fit statistical and tabular challengers using earlier data only."""

from collections import defaultdict
from importlib.metadata import version
from math import log

from .dataset import evidence, market_weights
from .features import FEATURE_VERSION, RESALE_FEATURES, SETTLEMENT_FEATURES
from .forecast import (
    MODEL_SCHEMA, clip_probability, logit, policy_signature, predict_payload, resale_distribution,
    settlement_probability,
)
from .policy import FEE_MODEL
from .replay import day_interval

SEED = 2718
PHASES = ("training", "calibration", "selection")
MINIMUMS = {
    "settlement": (24, 10, 10),
    "resale": (20, 8, 8),
}
MIN_LABELED_RESALE_FRACTION = .60


def _require(rows_by_phase, objective, *, head=None, datasets=None) -> tuple[dict, list[str]]:
    requirements = []
    counts = {}
    for phase, rows, minimum in zip(PHASES, rows_by_phase, MINIMUMS[objective]):
        counts[phase] = evidence(rows)
        if counts[phase]["markets"] < minimum or counts[phase]["days"] < 2:
            requirements.append(f"{phase} needs {minimum} labeled markets across at least 2 UTC days")
        if objective == "settlement" and {r.targets[0] for r in rows} != {0., 1.}:
            requirements.append(f"{phase} needs both official outcomes")
        if head:
            totals = datasets[phase].coverage["resale_outcomes"].get(head, {})
            attempts = sum(value for outcome, value in totals.items() if outcome != "no_entry_offer")
            fraction = len(rows) / attempts if attempts else None
            counts[phase]["conditional_label_fraction"] = fraction
            if fraction is None or fraction < MIN_LABELED_RESALE_FRACTION:
                requirements.append(f"{phase} needs at least 60% complete entry and exit labels among executable signals")
    return counts, requirements


def _arrays(rows, names, target):
    return [[r.features[n] for n in names] for r in rows], [r.targets[target] for r in rows]


def _fit(rows, family, names, target, *, binary=False):
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.preprocessing import StandardScaler
    from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor

    x, y = _arrays(rows, names, target)
    weights = market_weights(rows)
    if family == "constant":
        return {"family": family, "value": sum(a * b for a, b in zip(weights, y)) / sum(weights)}
    if family == "tree":
        tree_class = DecisionTreeClassifier if binary else DecisionTreeRegressor
        learner = tree_class(max_depth=4, min_samples_leaf=max(6, min(30, len(rows) // 12)), random_state=SEED)
        learner.fit(x, y, sample_weight=weights)
        tree = learner.tree_
        values = [float(row[0, 1] / row[0].sum()) if binary else float(row[0, 0]) for row in tree.value]
        return {"family": family, "feature": tree.feature.tolist(), "threshold": tree.threshold.tolist(),
                "left": tree.children_left.tolist(), "right": tree.children_right.tolist(), "values": values}
    scaling = StandardScaler().fit(x, sample_weight=weights)
    learner = LogisticRegression(C=1., max_iter=1000, random_state=SEED) if binary else Ridge(alpha=3.)
    learner.fit(scaling.transform(x), y, sample_weight=weights)
    return {"family": family, "center": scaling.mean_.tolist(), "scale": scaling.scale_.tolist(),
            "coefficients": (learner.coef_[0] if binary else learner.coef_).tolist(),
            "intercept": float(learner.intercept_[0] if binary else learner.intercept_)}


def binary_metrics(rows, predictions) -> dict:
    if not rows:
        return evidence(rows) | {"brier": None, "log_loss": None}
    y = [r.targets[0] for r in rows]
    p = [clip_probability(value) for value in predictions]
    w = market_weights(rows)
    total = sum(w)
    brier = [(prob - actual) ** 2 for prob, actual in zip(p, y)]
    by_day = defaultdict(list)
    bins = [[] for _ in range(5)]
    for row, prob, actual, weight, loss in zip(rows, p, y, w, brier):
        by_day[row.day].append((loss, weight))
        bins[min(4, int(prob * 5))].append((prob, actual, weight))
    profile = []
    for index, group in enumerate(bins):
        weight = sum(v[2] for v in group)
        if weight:
            profile.append({
                "probability_bin_start": index / 5, "rows": len(group),
                "mean_probability": sum(a * c for a, _, c in group) / weight,
                "observed_frequency": sum(b * c for _, b, c in group) / weight,
            })
    loss_by_day = [sum(a * b for a, b in group) / sum(b for _, b in group) for group in by_day.values()]
    return evidence(rows) | {
        "brier": sum(loss * weight for loss, weight in zip(brier, w)) / total,
        "log_loss": -sum(weight * log(prob if actual else 1 - prob) for prob, actual, weight in zip(p, y, w)) / total,
        "accuracy_at_half": sum(weight for prob, actual, weight in zip(p, y, w) if (prob >= .5) == bool(actual)) / total,
        "calibration": profile, "mean_day_brier_interval_95": day_interval(loss_by_day),
    }


def _settlement(train, calibration, selection):
    from sklearn.linear_model import LogisticRegression

    fitted = {}
    scores = {}
    for family in ("logistic", "tree"):
        fit = _fit(train, family, SETTLEMENT_FEATURES, 0, binary=True)
        x, y = _arrays(calibration, SETTLEMENT_FEATURES, 0)
        transformed = [[logit(predict_payload(fit, row))] for row in x]
        calibration_fit = LogisticRegression(C=1., max_iter=1000, random_state=SEED)
        calibration_fit.fit(transformed, y, sample_weight=market_weights(calibration))
        model = {"fit": fit, "calibration": {"slope": float(calibration_fit.coef_[0, 0]),
                                            "intercept": float(calibration_fit.intercept_[0])}}
        scores[family] = binary_metrics(selection, [settlement_probability(model, r.features) for r in selection])
        fitted[family] = model
    constant = _fit(train, "constant", SETTLEMENT_FEATURES, 0)["value"]
    scores["train_base_rate"] = binary_metrics(selection, [constant for _ in selection])
    scores["market_midpoint"] = binary_metrics(selection, [r.features["yes_mid"] for r in selection])
    winner = min(fitted, key=lambda family: scores[family]["brier"])
    return fitted[winner], scores, winner, constant


def regression_metrics(rows, predictions, index: int) -> dict:
    if not rows:
        return evidence(rows) | {"mae": None, "interval_coverage": None}
    weights = market_weights(rows)
    denominator = sum(weights)
    absolute = covered = quantile_loss = 0.
    by_day = defaultdict(list)
    for row, pred, weight in zip(rows, predictions, weights):
        y = row.targets[index]
        error = abs(pred["p50"] - y)
        absolute += weight * error
        covered += weight * (pred["p10"] <= y <= pred["p90"])
        for name, q in (("p10", .10), ("p90", .90)):
            residual = y - pred[name]
            quantile_loss += weight * max(q * residual, (q - 1) * residual)
        by_day[row.day].append((error, weight))
    daily = [sum(a * b for a, b in group) / sum(b for _, b in group) for group in by_day.values()]
    return evidence(rows) | {
        "mae": absolute / denominator, "interval_coverage": covered / denominator,
        "mean_tail_quantile_loss": quantile_loss / (2 * denominator),
        "mean_day_mae_interval_95": day_interval(daily),
    }


def unchanged_bid(row, index: int) -> dict:
    values = row.features
    value = (values["bid_per_contract"] if index == 0
             else values["sale_cash_per_contract"] - values["cost_per_contract"])
    return dict.fromkeys(("p10", "p50", "p90"), value)


def _residual_quantiles(residuals, rows):
    ordered = sorted(zip(residuals, market_weights(rows)))
    total = sum(weight for _, weight in ordered)
    result = []
    for probability in (.10, .50, .90):
        cumulative = 0.
        for residual, weight in ordered:
            cumulative += weight
            if cumulative >= probability * total:
                result.append(residual)
                break
    return result


def _resale(train, calibration, selection):
    models, scores, winners = {}, {}, {}
    for index, name in enumerate(("exit_value", "net_return")):
        fitted = {}
        scores[name] = {}
        for family in ("ridge", "tree"):
            fit = _fit(train, family, RESALE_FEATURES, index)
            x, y = _arrays(calibration, RESALE_FEATURES, index)
            residual = [actual - predict_payload(fit, row) for row, actual in zip(x, y)]
            model = {"fit": fit, "offsets": _residual_quantiles(residual, calibration)}
            scored = [resale_distribution({name: model}, row.features)[name] for row in selection]
            scores[name][family] = regression_metrics(selection, scored, index)
            fitted[family] = model
        scores[name]["unchanged_executable_bid"] = regression_metrics(
            selection, [unchanged_bid(r, index) for r in selection], index,
        )
        winner = min(fitted, key=lambda family: scores[name][family]["mae"])
        models[name] = fitted[winner]
        winners[name] = winner
    return models, scores, winners


def train_models(datasets, periods, policy) -> tuple[dict | None, dict]:
    settlement_rows = [datasets[phase].settlement for phase in PHASES]
    counts, gaps = _require(settlement_rows, "settlement")
    report = {"settlement": {"counts": counts, "missing": gaps}, "resale": {},
              "coverage": {name: data.coverage for name, data in datasets.items()}}
    rows_by_head = {}
    for side in ("yes", "no"):
        for horizon in (30, 60, 120):
            name = f"{side}_{horizon}"
            rows = [datasets[phase].resale.get(name, []) for phase in PHASES]
            counts, missing = _require(rows, "resale", head=name, datasets=datasets)
            report["resale"][name] = {"counts": counts, "missing": missing}
            if not missing:
                rows_by_head[name] = rows
    if gaps and not rows_by_head:
        return None, report | {"status": "UNTRAINED"}
    try:
        packages = {p: version(p) for p in ("scikit-learn", "numpy", "scipy")}
    except ImportError as exc:
        raise ValueError("install requirements-research.txt to fit the models") from exc
    settlement_model = base_rate = None
    if not gaps:
        settlement_model, scores, winner, base_rate = _settlement(*settlement_rows)
        report["settlement"].update({"selection_metrics": scores, "chosen_family": winner})
    resale = {}
    for key, rows in rows_by_head.items():
        resale[key], scores, winners = _resale(*rows)
        report["resale"][key].update({"selection_metrics": scores, "chosen_families": winners})
    status = "CHALLENGER" if settlement_model is not None and len(resale) == 6 else "PARTIAL_CHALLENGER"
    report["status"] = status
    artifact = {
        "schema": MODEL_SCHEMA, "status": status, "feature_version": FEATURE_VERSION, "fee_model": FEE_MODEL,
        "settlement_features": SETTLEMENT_FEATURES, "resale_features": RESALE_FEATURES,
        "settlement": settlement_model, "resale": resale, "training_base_rate": base_rate,
        "periods": periods, "policy": policy, "policy_signature": policy_signature(policy),
        "dataset_digests": {name: data.digest for name, data in datasets.items()},
        "settings": {"random_seed": SEED, "package_versions": packages, "resale_quantiles": [.10, .50, .90]},
        "training_report": report,
    }
    return artifact, report
