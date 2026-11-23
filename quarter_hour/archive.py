"""Read-only SQLite backup and archive verification. Also runnable as a standalone script."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


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


def open_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"database file does not exist: {path}")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10, isolation_level=None)


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp is not None else None


def database_summary(db: sqlite3.Connection) -> dict:
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity check failed")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("SQLite foreign-key check failed")
    tables = db.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
    ).fetchall()
    counts = {}
    for (name,) in tables:
        identifier = '"' + name.replace('"', '""') + '"'
        counts[name] = db.execute(f"SELECT COUNT(*) FROM {identifier}").fetchone()[0]
    if not {"observations", "markets", "results"} <= counts.keys():
        raise ValueError("database is missing Quarter Hour recording tables")
    first_id, last_id, start, end = db.execute("SELECT MIN(id),MAX(id),MIN(at),MAX(at) FROM observations").fetchone()
    first_result, last_result = db.execute("SELECT MIN(received),MAX(received) FROM results").fetchone()
    return {
        "schema_version": db.execute("PRAGMA user_version").fetchone()[0],
        "integrity": "ok", "counts": counts,
        "observation_range": {"first_id": first_id, "last_id": last_id, "start_utc": utc(start), "end_utc": utc(end)},
        "result_receipt_range": {"start_utc": utc(first_result), "end_utc": utc(last_result)},
    }


def file_summary(path: Path) -> dict:
    with path.open("rb") as stream:
        return {"bytes": path.stat().st_size, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}


def backup_database(source: Path, destination: Path, *, off_volume: bool = False) -> dict:
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination == source or destination.exists():
        raise ValueError("backup destination must be a new file")
    if not source.is_file():
        raise FileNotFoundError(f"database file does not exist: {source}")
    parent = next(path for path in destination.parents if path.is_dir())
    if off_volume and parent.stat().st_dev == source.stat().st_dev:
        raise ValueError("backup destination must be on a different filesystem from the source")
    with closing(open_read_only(source)) as db:
        db.execute("BEGIN")
        # Pin a single committed snapshot, including pages still in the source WAL.
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        if shutil.disk_usage(parent).free < (pages + 1) * page_size + 1_000_000:
            raise ValueError("destination has insufficient free space for the backup")
        with new_output(destination) as temporary:
            with closing(sqlite3.connect(temporary)) as target:
                db.backup(target)
                db.rollback()
                if target.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise ValueError("could not make backup standalone")
                summary = database_summary(target)
            summary |= file_summary(temporary)
    return {"saved_to": str(destination), "created_utc": datetime.now(timezone.utc).isoformat(), **summary}


def verify_backup(path: Path, *, manifest: Path | None = None) -> dict:
    path = Path(path).resolve(strict=True)
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("expected a standalone backup; preserve the original database and its sidecars as a set")
    with closing(open_read_only(path)) as db:
        db.execute("BEGIN")
        summary = database_summary(db) | file_summary(path)
    if manifest is not None:
        expected = json.loads(Path(manifest).read_text(encoding="utf-8-sig"))
        if not isinstance(expected, dict):
            raise ValueError("archive manifest must be a JSON object")
        for field, value in summary.items():
            if value != expected.get(field):
                raise ValueError(f"archive manifest mismatch for {field}")
    return {"verified": str(path), **summary}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Archive an existing Quarter Hour database")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("source", type=Path)
    backup.add_argument("destination", type=Path)
    backup.add_argument("--off-volume", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("path", type=Path)
    verify.add_argument("--manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            report = backup_database(args.source, args.destination, off_volume=args.off_volume)
        else:
            report = verify_backup(args.path, manifest=args.manifest)
        print(json.dumps(report, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Quarter Hour archive: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
