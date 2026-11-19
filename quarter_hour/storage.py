"""Versioned SQLite store and consistent, portable exports."""

import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path

from .domain import Market, Result, Side, Snapshot, json_text, number

MIGRATIONS = (
    (
        """CREATE TABLE markets (
            ticker TEXT PRIMARY KEY, event TEXT NOT NULL, opens REAL NOT NULL,
            closes REAL NOT NULL, recorded REAL NOT NULL, last_check REAL
        )""",
        """CREATE TABLE observations (
            id INTEGER PRIMARY KEY, at REAL NOT NULL, ticker TEXT REFERENCES markets(ticker),
            payload TEXT NOT NULL
        )""",
        "CREATE INDEX observation_ticker_time ON observations(ticker, at, id)",
        "CREATE INDEX observation_time ON observations(at, id)",
        """CREATE TABLE raw_responses (
            id INTEGER PRIMARY KEY, route TEXT NOT NULL, requested REAL NOT NULL,
            received REAL NOT NULL, status INTEGER, payload TEXT
        )""",
        """CREATE TABLE results (
            ticker TEXT PRIMARY KEY REFERENCES markets(ticker), received REAL NOT NULL,
            payload TEXT NOT NULL
        )""",
        """CREATE TABLE quality (
            id INTEGER PRIMARY KEY, at REAL NOT NULL, code TEXT NOT NULL, detail TEXT NOT NULL
        )""",
        """CREATE TABLE worker (
            slot INTEGER PRIMARY KEY CHECK(slot = 1), session TEXT NOT NULL, started REAL NOT NULL,
            heartbeat REAL NOT NULL, stopped REAL, stop_requested INTEGER NOT NULL DEFAULT 0
        )""",
    ),
)


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA foreign_keys = ON")
            self.db.execute("PRAGMA journal_mode = WAL")
            self.db.execute("PRAGMA synchronous = FULL")
            self.migrate()
        except BaseException:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.db.close()

    @contextmanager
    def transaction(self, *, write: bool = True):
        self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def migrate(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == len(MIGRATIONS):
            return
        with self.transaction():
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise ValueError("database was created by a newer Quarter Hour version")
            for index, migration in enumerate(MIGRATIONS[version:], start=version + 1):
                for sql in migration:
                    self.db.execute(sql)
                self.db.execute(f"PRAGMA user_version = {index}")

    @contextmanager
    def worker_lock(self):
        # Holding a transaction on a separate file leaves the research DB readable.
        lock = self.path.with_suffix(self.path.suffix + ".worker")
        with closing(sqlite3.connect(lock, timeout=0)) as lease:
            try:
                lease.execute("BEGIN EXCLUSIVE")
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                    raise ValueError("a worker is already running for this database") from exc
                raise
            yield

    def quality(self, at: float, code: str, **detail):
        self.db.execute(
            "INSERT INTO quality(at,code,detail) VALUES (?,?,?)", (at, code, json_text(detail)),
        )

    def raw(self, route: str, requested: float, received: float, status: int | None, payload):
        self.db.execute(
            "INSERT INTO raw_responses(route,requested,received,status,payload) VALUES (?,?,?,?,?)",
            (route, requested, received, status, json_text(payload)),
        )

    def save_market(self, market: Market) -> bool:
        identity = (market.event, market.opens, market.closes)
        self.db.execute(
            "INSERT OR IGNORE INTO markets(ticker,event,opens,closes,recorded) VALUES (?,?,?,?,?)",
            (market.ticker, *identity, market.received),
        )
        current = self.db.execute(
            "SELECT event,opens,closes FROM markets WHERE ticker=?", (market.ticker,),
        ).fetchone()
        return tuple(current) == identity

    def last_snapshot(self) -> Snapshot | None:
        row = self.db.execute(
            "SELECT id,payload FROM observations ORDER BY id DESC LIMIT 1",
        ).fetchone()
        return self._decode_snapshot(row) if row else None

    def save_snapshot(self, sample: Snapshot) -> Snapshot:
        last = self.db.execute("SELECT at FROM observations ORDER BY id DESC LIMIT 1").fetchone()
        if last and sample.at <= last["at"]:
            raise ValueError("clock moved backward or repeated a sample time")
        cur = self.db.execute(
            "INSERT INTO observations(at,ticker,payload) VALUES (?,?,?)",
            (sample.at, sample.ticker, json_text(sample)),
        )
        return replace(sample, id=cur.lastrowid)

    @staticmethod
    def _decode_snapshot(row) -> Snapshot:
        data = json.loads(row["payload"])
        data["id"] = row["id"]
        return Snapshot.load(data)

    def snapshots(self, *, since: float = 0, until: float | None = None, ticker: str | None = None):
        query = "SELECT id,payload FROM observations WHERE at>=?"
        params = [since]
        if until is not None:
            query += " AND at<?"
            params.append(until)
        if ticker is not None:
            query += " AND ticker=?"
            params.append(ticker)
        for row in self.db.execute(query + " ORDER BY at,id", params):
            yield self._decode_snapshot(row)

    def results(self) -> dict[str, Result]:
        results = {}
        for row in self.db.execute("SELECT payload FROM results"):
            info = json.loads(row["payload"])
            info["side"] = Side(info["side"])
            value = info["benchmark_value"]
            info["benchmark_value"] = number(value) if value is not None else None
            results[info["ticker"]] = Result(**info)
        return results

    def save_result(self, market: Market, result: Result) -> bool:
        if (
            (result.ticker, result.event) != (market.ticker, market.event)
            or result.received < market.closes
            or (result.settled is not None and not market.closes <= result.settled <= result.received)
            or not self.save_market(market)
        ):
            return False
        row = self.db.execute("SELECT payload FROM results WHERE ticker=?", (result.ticker,)).fetchone()
        if row:
            existing = json.loads(row["payload"])
            return existing["side"] == result.side and existing["event"] == result.event
        self.db.execute(
            "INSERT INTO results VALUES (?,?,?)",
            (result.ticker, result.received, json_text(result)),
        )
        return True

    def due_result(self, now: float, retry_after: float = 60, *, priority: str | None = None):
        return self.db.execute(
            """SELECT ticker,event FROM markets WHERE closes<=?
            AND (last_check IS NULL OR last_check<=?)
            AND NOT EXISTS (SELECT 1 FROM results WHERE results.ticker=markets.ticker)
            ORDER BY CASE WHEN ticker=? THEN 0 ELSE 1 END,last_check,closes LIMIT 1""",
            (now, now - retry_after, priority),
        ).fetchone()

    def checked_result(self, ticker: str, at: float):
        self.db.execute("UPDATE markets SET last_check=? WHERE ticker=?", (at, ticker))

    def backup(self, path: Path):
        path = Path(path).resolve()
        if path == self.path or path.exists():
            raise ValueError("backup destination must be a new file")
        with new_output(path) as temporary:
            with closing(sqlite3.connect(temporary)) as target:
                self.db.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("backup integrity check failed")

    def export(self, path: Path):
        if Path(path).resolve().exists():
            raise ValueError("export destination must be a new file")
        with new_output(Path(path).resolve()) as temporary, temporary.open("w", encoding="utf-8") as out:
            with self.transaction(write=False):
                tables = self.db.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
                ).fetchall()
                out.write(json_text({"schema_version": len(MIGRATIONS), "exported": time.time()}) + "\n")
                for (table,) in tables:
                    identifier = '"' + table.replace('"', '""') + '"'
                    for row in self.db.execute(f"SELECT * FROM {identifier}"):
                        record = dict(row)
                        for field in ("payload", "detail"):
                            if field in record and record[field] is not None:
                                record[field] = json.loads(record[field])
                        out.write(json_text({"table": table, "record": record}) + "\n")


@contextmanager
def new_output(path: Path):
    """Publish a complete file without replacing an existing destination."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".quarter-hour-")
    os.close(fd)
    temporary = Path(name)
    try:
        yield temporary
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
