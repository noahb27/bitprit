"""Atomic paper decisions, frozen forecasts and explicit model activation."""

import json
import time

from .dataset import Example
from .domain import json_text, number
from .execution import State, Trade, advance
from .features import FEATURE_VERSION, FeatureCursor
from .fit import binary_metrics
from .forecast import choose, forecast, policy_signature
from .policy import Policy
from .registry import active_model, evaluation, load_artifact
from .replay import trade_report, utc_day
from .research import MIN_FORWARD_DAYS, MIN_FORWARD_TRADES


def paper_state(store) -> tuple[State, int]:
    row = store.db.execute("SELECT last_sample,payload FROM paper_state WHERE slot=1").fetchone()
    return (State.load(json.loads(row["payload"])), row["last_sample"]) if row else (State(), 0)


class PaperWorker:
    def __init__(self, store, policy: Policy, *, trial: str | None = None):
        self.store = store
        self.policy = policy
        self.model_id = trial or active_model(store)
        self.artifact = load_artifact(store, self.model_id) if self.model_id else None
        self.mode = "trial" if trial else "paper"
        if trial:
            graded = evaluation(store, trial)
            if not graded or not graded[1]["gates"]["eligible_for_forward_trial"]:
                raise ValueError("the requested challenger has not passed its sealed holdout gates")
            if policy_signature(policy) != self.artifact["policy_signature"]:
                raise ValueError("a forward trial must use the exact settings recorded in its artifact")
        self.cursor = FeatureCursor()

    def priority(self) -> str | None:
        state, _ = paper_state(self.store)
        return state.position.order.market.ticker if state.position else None

    def record(self, sample, *, now: float | None = None):
        with self.store.transaction():
            state, last = paper_state(self.store)
            if sample.id <= last:
                existing = self.store.db.execute(
                    "SELECT payload FROM decisions WHERE sample_id=?", (sample.id,),
                ).fetchone()
                if existing:
                    return json.loads(existing["payload"])
                raise ValueError("a recording interruption left this older snapshot without a decision")
            at = max(sample.at, time.time() if now is None else now)
            facts = self.cursor.observe(sample, self.policy)
            predicted = forecast(sample, facts, self.artifact, self.policy, at=at)

            def advice(sample, offers, cfg, at):
                return choose(predicted, offers, cfg, "combined")

            ticker = state.position.order.market.ticker if state.position else None
            known = self.store.results(ticker=ticker) if ticker else {}
            step = advance(state, sample, known, self.policy, advice, at=at)
            decision = step.decision
            evidence = {"prediction_status": predicted["status"], "artifact_id": self.model_id,
                        "mode": self.mode, "feature_version": FEATURE_VERSION, "policy": self.policy}
            payload = {"action": decision.action, "status": decision.status, "reason": decision.reason,
                       "detail": decision.detail, "evidence": evidence}
            db = self.store.db
            db.execute("INSERT INTO features VALUES (?,?,?)", (sample.id, FEATURE_VERSION, json_text(facts)))
            db.execute("INSERT INTO forecasts VALUES (?,?,?,?)", (
                sample.id, self.model_id, predicted["status"], json_text(predicted),
            ))
            db.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?)", (
                sample.id, at, self.model_id, self.mode, decision.action, decision.status, json_text(payload),
            ))
            if step.fill:
                signal = state.position.order.signal_id if state.position else state.intent.signal_id
                db.execute("INSERT INTO fills VALUES (?,?,?,?)", (
                    sample.id, decision.action, signal, json_text(step.fill),
                ))
                if step.trade:
                    db.execute("INSERT INTO trades VALUES (?,?,?,?)", (
                        sample.id, signal, step.trade.outcome, json_text(step.trade),
                    ))
            db.execute(
                """INSERT INTO paper_state VALUES (1,?,?) ON CONFLICT(slot) DO UPDATE
                SET last_sample=excluded.last_sample,payload=excluded.payload""",
                (sample.id, json_text(step.state)),
            )
        return json.loads(json_text(payload))


def _journal_trades(store, identifier=None, *, trial_from: float | None = None):
    query = """
        SELECT t.payload FROM trades t JOIN decisions signal ON signal.sample_id=t.signal_sample
        WHERE 1=1
    """
    parameters = []
    if identifier:
        query += " AND signal.artifact_id=?"
        parameters.append(identifier)
    if trial_from is not None:
        query += " AND signal.mode='trial' AND signal.at>=?"
        parameters.append(trial_from)
    return [Trade.load(json.loads(row[0])) for row in store.db.execute(query + " ORDER BY t.sample_id", parameters)]


def forward_report(store, identifier=None, *, trial_from: float | None = None) -> dict:
    trades = _journal_trades(store, identifier, trial_from=trial_from)
    filters = []
    params = []
    if identifier:
        filters.append("artifact_id=?")
        params.append(identifier)
    if trial_from is not None:
        filters.append("mode='trial' AND at>=?")
        params.append(trial_from)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    rows = store.db.execute(f"SELECT at,action,status FROM decisions{where}", params)
    days = set()
    counts = {}
    buy_intents = 0
    for row in rows:
        days.add(utc_day(row["at"]))
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        if row["action"].startswith("BUY"):
            buy_intents += row["status"] == "SUBMITTED"
    buy_fills = store.db.execute(
        f"""SELECT count(*) FROM (SELECT sample_id FROM decisions{where}) signal
        JOIN fills f ON f.signal_sample=signal.sample_id WHERE f.action IN ('BUY YES','BUY NO')""",
        params,
    ).fetchone()[0]
    vols = {}
    for trade in trades:
        key = trade.position.entry.sample_id
        row = store.db.execute("SELECT payload FROM features WHERE sample_id=?", (key,)).fetchone()
        facts = json.loads(row[0]) if row else None
        if facts:
            vols[key] = facts["spot_volatility_60s"]
    closed_days = {utc_day(t.position.entry.at) for t in trades}
    state, _ = paper_state(store)
    return trade_report(trades, days, vols) | {
        "mode": "TRIAL_ONLY" if trial_from is not None else "PAPER_JOURNAL",
        "model_id": identifier, "days_observed": len(days), "days_with_closed_trades": len(closed_days),
        "entry_intents": buy_intents, "entry_fills": buy_fills, "decision_counts": counts,
        "entry_fill_coverage": buy_fills / buy_intents if buy_intents else None,
        "completed_exposure_seconds": sum(t.exposure_seconds for t in trades),
        "open_position": state.position is not None,
        "pending_order": state.intent is not None,
    }


def original_forecast_report(store, identifier=None, *, trial_from=None) -> dict:
    eligible = store.db.execute(
        """SELECT f.artifact_id,f.payload,o.ticker,o.at,m.closes,r.payload AS final
        FROM forecasts f JOIN observations o ON o.id=f.sample_id JOIN markets m ON m.ticker=o.ticker
        JOIN results r ON r.ticker=o.ticker JOIN decisions d ON d.sample_id=o.id
        WHERE f.artifact_id IS NOT NULL AND (? IS NULL OR f.artifact_id=?)
          AND (? IS NULL OR (d.at>=? AND d.mode='trial'))
          AND o.at<m.closes
        ORDER BY o.at,f.sample_id""",
        (identifier, identifier, trial_from, trial_from),
    )
    grouped: dict[str, dict] = {}
    for row in eligible:
        probability = json.loads(row["payload"])["settlement_yes"]
        if probability is None:
            continue
        final = json.loads(row["final"])
        close = final.get("market_close") or row["closes"]
        if not row["at"] < close <= final["received"]:
            continue
        first = grouped.setdefault(row["artifact_id"], {})
        if row["ticker"] not in first:
            first[row["ticker"]] = (Example(row["ticker"], utc_day(row["at"]), row["at"], {},
                (float(final["side"] == "yes"),), final["received"]), probability)
    return {
        identifier: binary_metrics([item[0] for item in group.values()], [item[1] for item in group.values()])
        for identifier, group in grouped.items()
    }


def promote(store, identifier: str) -> dict:
    with store.transaction():
        load_artifact(store, identifier)
        graded = evaluation(store, identifier)
        if not graded or not graded[1]["gates"]["eligible_for_forward_trial"]:
            return {"status": "PROMOTION_BLOCKED", "reason": "SEALED_HOLDOUT_GATES_NOT_PASSED"}
        report = forward_report(store, identifier, trial_from=graded[0])
        uncertainty = report["mean_daily_pnl_interval_95"]
        checks = {
            "minimum_closed_trial_trades": report["closed_trades"] >= MIN_FORWARD_TRADES,
            "minimum_trial_trade_days": report["days_with_closed_trades"] >= MIN_FORWARD_DAYS,
            "positive_trial_pnl": number(report["net_pnl"]) > 0,
            "positive_mean_utc_day_lower_bound": uncertainty is not None and number(uncertainty[0]) > 0,
            "no_open_position_or_order": not (report["open_position"] or report["pending_order"]),
        }
        if not all(checks.values()):
            return {"status": "PROMOTION_BLOCKED", "checks": checks, "forward_trial": report}
        store.db.execute(
            """INSERT INTO active_model VALUES (1,?,?) ON CONFLICT(slot) DO UPDATE
            SET artifact_id=excluded.artifact_id,activated=excluded.activated""", (identifier, time.time()),
        )
    return {"status": "PAPER_MODEL_ACTIVATED", "artifact_id": identifier, "checks": checks}
