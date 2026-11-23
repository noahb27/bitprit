"""Measure disk space before starting or continuing a recording."""

import errno
import os
import shutil
import sqlite3
from pathlib import Path

RESERVE_BYTES = 50_000_000
WARNING_BYTES = 100_000_000


def existing_directory(path: Path) -> Path:
    while not path.is_dir() and path != path.parent:
        path = path.parent
    return path


def file_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def volume_bytes(root: Path) -> int:
    """Count files on the dedicated volume without following links or nested mounts."""
    try:
        device = root.stat().st_dev
    except FileNotFoundError:
        return 0

    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if info.st_dev != device:
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        else:
                            total += max(info.st_size, getattr(info, "st_blocks", 0) * 512)
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            continue
    return total


def storage_status(database: Path, volume_limit_bytes: int | None = None) -> dict:
    limits = {
        "volume_limit_bytes": volume_limit_bytes, "reserve_bytes": RESERVE_BYTES,
        "warning_bytes": WARNING_BYTES,
    }
    try:
        database = database.resolve()
        parent = database.parent
        usage = shutil.disk_usage(existing_directory(parent))
        files = volume_bytes(parent) if volume_limit_bytes is not None else None
        remaining = usage.free if files is None else min(usage.free, max(0, volume_limit_bytes - files))
        state = "stop" if remaining <= RESERVE_BYTES else "warning" if remaining <= WARNING_BYTES else "ok"
        return limits | {
            "state": state, "reason": "LOW_STORAGE" if state == "stop" else None,
            "filesystem_total_bytes": usage.total, "filesystem_free_bytes": usage.free,
            "volume_file_bytes": files, "headroom_bytes": remaining,
            "database_bytes": file_bytes(database), "wal_bytes": file_bytes(Path(str(database) + "-wal")),
            "shm_bytes": file_bytes(Path(str(database) + "-shm")),
        }
    except OSError as exc:
        return limits | {"state": "stop", "reason": "STORAGE_CHECK_FAILED", "error": str(exc)}


def is_storage_full(exc: Exception) -> bool:
    return (
        isinstance(exc, sqlite3.Error) and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL
        or isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC))
    )


def storage_full(exc: Exception) -> dict:
    return {"state": "stop", "reason": "STORAGE_FULL", "error": str(exc)}
