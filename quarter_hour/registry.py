"""Immutable challenger and holdout records."""

import json
import time
from hashlib import sha256

from .domain import json_text


def save_artifact(store, artifact: dict) -> str:
    content = json_text(artifact)
    identifier = sha256(content.encode()).hexdigest()
    store.db.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?,?)", (identifier, time.time(), content))
    return identifier


def load_artifact(store, identifier: str) -> dict:
    row = store.db.execute("SELECT payload FROM artifacts WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise ValueError("unknown model artifact ID")
    if sha256(row["payload"].encode()).hexdigest() != identifier:
        raise ValueError("model artifact digest mismatch")
    return json.loads(row["payload"])


def active_model(store) -> str | None:
    row = store.db.execute("SELECT artifact_id FROM active_model WHERE slot=1").fetchone()
    return row[0] if row else None


def evaluation(store, identifier: str):
    row = store.db.execute("SELECT * FROM evaluations WHERE artifact_id=?", (identifier,)).fetchone()
    return (row["created"], json.loads(row["payload"])) if row else None


def check_unused_holdout(store, start: float, end: float):
    conflict = store.db.execute("SELECT 1 FROM evaluations WHERE starts<? AND ends>? LIMIT 1", (end, start)).fetchone()
    if conflict:
        raise ValueError("the proposed holdout overlaps a previously graded holdout; choose a fresh later window")
