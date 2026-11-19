"""Frozen JSON inference and transparent policy scoring."""

from hashlib import sha256
from math import exp, log
from struct import pack, unpack

from .dataset import HORIZONS, head_key
from .domain import Side, json_text
from .execution import Advice
from .features import FEATURE_VERSION, RESALE_FEATURES, SETTLEMENT_FEATURES, resale_features
from .policy import FEE_MODEL, Policy, entry_offer

MODEL_SCHEMA = 1
STRATEGIES = ("settlement_only", "resale_only", "combined")


def policy_signature(policy: Policy) -> str:
    return sha256(json_text(policy).encode()).hexdigest()


def clip_probability(probability: float) -> float:
    return min(.999, max(.001, probability))


def logit(probability: float) -> float:
    p = clip_probability(probability)
    return log(p / (1 - p))


def logistic(value: float) -> float:
    return 1 / (1 + exp(-max(-35, min(35, value))))


def predict_payload(model: dict, row: list[float]) -> float:
    if model["family"] == "tree":
        node = 0
        indices, thresholds = model["feature"], model["threshold"]
        while indices[node] >= 0:
            value = unpack("f", pack("f", row[indices[node]]))[0]
            node = model["left"][node] if value <= thresholds[node] else model["right"][node]
        return model["values"][node]
    if model["family"] == "constant":
        return model["value"]
    value = model["intercept"] + sum(
        coefficient * ((feature - center) / scale)
        for coefficient, feature, center, scale in zip(model["coefficients"], row, model["center"], model["scale"])
    )
    return logistic(value) if model["family"] == "logistic" else value


def settlement_probability(model: dict, values: dict) -> float:
    raw = predict_payload(model["fit"], [values[key] for key in SETTLEMENT_FEATURES])
    calibration = model["calibration"]
    return clip_probability(logistic(calibration["intercept"] + calibration["slope"] * logit(raw)))


def resale_distribution(model: dict, values: dict) -> dict:
    vector = [values[key] for key in RESALE_FEATURES]
    result = {}
    for name, head in model.items():
        center = predict_payload(head["fit"], vector)
        estimates = [center + q for q in head["offsets"]]
        if name == "exit_value":
            estimates = [min(1, max(0, value)) for value in estimates]
        result[name] = dict(zip(("p10", "p50", "p90"), estimates))
    return result


def forecast(sample, features, artifact, policy: Policy, *, at: float, stress: bool = False) -> dict:
    if artifact is None:
        return {"status": "UNTRAINED", "settlement_yes": None, "resale": {}}
    if artifact["schema"] != MODEL_SCHEMA or artifact["feature_version"] != FEATURE_VERSION or artifact["fee_model"] != FEE_MODEL:
        return {"status": "UNSUPPORTED_ARTIFACT_VERSION", "settlement_yes": None, "resale": {}}
    if not stress and artifact["policy_signature"] != policy_signature(policy):
        return {"status": "EXECUTION_SETTINGS_DIFFER_FROM_TRAINING", "settlement_yes": None, "resale": {}}
    if features is None:
        return {"status": "FEATURES_UNAVAILABLE_NEED_FRESH_60_SECOND_HISTORY", "settlement_yes": None, "resale": {}}
    if not 220 <= features["seconds_remaining"] <= 780:
        return {"status": "OUTSIDE_TRAINING_TIME_RANGE", "settlement_yes": None, "resale": {}}
    model = artifact["settlement"]
    predictions = {
        "status": "READY", "settlement_yes": settlement_probability(model, features) if model else None,
        "resale": {}, "resale_exclusions": {},
    }
    for side in Side:
        for horizon in HORIZONS:
            key = head_key(side, horizon)
            if key not in artifact["resale"]:
                predictions["resale_exclusions"][key] = "UNTRAINED_RESALE_HEAD"
                continue
            offer = entry_offer(sample, side, policy, at=at, horizon=horizon)
            if offer.buy is None:
                predictions["resale_exclusions"][key] = offer.failures
                continue
            inputs = resale_features(features, offer, sample)
            predictions["resale"][key] = {
                "quantity": offer.buy.quantity, "inputs": inputs,
                **resale_distribution(artifact["resale"][key], inputs),
            }
    return predictions


def choose(prediction: dict, offers: dict, policy: Policy, strategy: str) -> Advice:
    if strategy not in STRATEGIES:
        raise ValueError("unsupported strategy")
    if prediction["status"] != "READY":
        return Advice(None, None, prediction["status"])
    probability = prediction["settlement_yes"]
    if strategy != "resale_only" and probability is None:
        return Advice(None, None, "UNTRAINED_SETTLEMENT")
    eligible = []
    arithmetic = {}
    buffer = float(policy.probability_buffer)
    edge = float(policy.min_net_edge)
    for side, offer in offers.items():
        if not offer.buy:
            continue
        cost = float(offer.buy.cash / offer.buy.quantity)
        fair = None if probability is None else probability if side == Side.YES else 1 - probability
        lower = max(0, fair - buffer) if fair is not None else None
        settlement_net = None if lower is None else lower - cost
        stat = {
            "entry_cost_per_contract": cost, "settlement_probability": fair, "probability_deduction": buffer,
            "buffered_settlement_payout": lower, "buffered_settlement_net": settlement_net,
            "gap_above_required_settlement_edge": None if settlement_net is None else settlement_net - edge,
            "required_edge_per_contract": edge,
        }
        arithmetic[side] = stat
        if strategy == "settlement_only":
            if settlement_net > edge:
                eligible.append((settlement_net, side, None))
            continue
        stat["horizons"] = {}
        for horizon in HORIZONS:
            resale = prediction["resale"].get(head_key(side, horizon))
            if not resale:
                stat["horizons"][horizon] = {"blocked": prediction["resale_exclusions"].get(head_key(side, horizon))}
                continue
            quantiles = resale["net_return"]
            q10 = quantiles["p10"]
            # The combined score retains a resale requirement while accounting
            # for the downside of getting stuck through official settlement.
            score = q10 if strategy == "resale_only" else .8 * q10 + .2 * settlement_net
            stat["horizons"][horizon] = {
                "median_net_after_both_legs": quantiles["p50"], "resale_downside_q10": q10,
                "resale_uncertainty_deduction": quantiles["p50"] - q10, "conservative_score": score,
                "required_edge": edge, "gap_above_required_edge": score - edge,
                "required_seconds": policy.required(horizon),
            }
            if q10 > edge and score > edge:
                eligible.append((score, side, horizon))
    detail = {"model_arithmetic": arithmetic, "strategy": strategy}
    if eligible:
        _, side, horizon = max(eligible, key=lambda item: item[0])
        return Advice(side, horizon, "BUFFERED_ESTIMATE_CLEARS_COST_AND_EDGE_CHECKS", detail)
    if not arithmetic:
        return Advice(None, None, "NO_EXECUTABLE_ENTRY", detail)
    if strategy != "settlement_only" and not prediction["resale"]:
        return Advice(None, None, "NO_ELIGIBLE_RESALE_HORIZON", detail)
    return Advice(None, None, "NET_EDGE_BELOW_REQUIRED_THRESHOLD", detail)
