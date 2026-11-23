"""Foreground worker lifecycle, independent of the local reader."""

import sqlite3
import time
from uuid import uuid4

from .collector import Collector, PublicGet
from .disk import is_storage_full, storage_full, storage_status
from .storage import Store


def run_worker(
    store: Store, *, interval: float, timeout: float, cycles: int | None,
    on_sample=None, priority=None, volume_limit_bytes: int | None = None, on_storage_warning=None,
) -> dict | None:
    if cycles is not None and cycles < 1:
        raise ValueError("cycles must be positive")
    with store.worker_lock():
        reading = storage_status(store.path, volume_limit_bytes)
        if reading["state"] == "stop":
            return reading | {"shutdown_recorded": False}
        session = uuid4().hex
        started = time.time()
        with store.transaction():
            previous = store.db.execute("SELECT * FROM worker WHERE slot=1").fetchone()
            if previous and previous["stopped"] is None:
                store.quality(started, "UNCLEAN_RESTART", last_heartbeat=previous["heartbeat"])
            store.db.execute(
                """INSERT INTO worker(slot,session,started,heartbeat) VALUES (1,?,?,?)
                ON CONFLICT(slot) DO UPDATE SET session=excluded.session,started=excluded.started,
                heartbeat=excluded.heartbeat,stopped=NULL,stop_requested=0""",
                (session, started, started),
            )
            store.quality(started, "WORKER_STARTED", session=session)
        halted = None
        warned = False
        try:
            collector = Collector(store, PublicGet(store, timeout=timeout), interval=interval)
            count = 0
            while cycles is None or count < cycles:
                if store.db.execute("SELECT stop_requested FROM worker WHERE slot=1").fetchone()[0]:
                    break
                reading = storage_status(store.path, volume_limit_bytes)
                if reading["state"] == "stop":
                    halted = reading
                    break
                if reading["state"] == "warning" and not warned and on_storage_warning:
                    on_storage_warning(reading)
                warned = reading["state"] == "warning"
                start = time.monotonic()
                sample = collector.collect()
                if on_sample:
                    on_sample(sample)
                collector.check_result(priority=priority() if priority else None)
                store.db.execute("UPDATE worker SET heartbeat=? WHERE slot=1", (time.time(),))
                count += 1
                if cycles is None or count < cycles:
                    time.sleep(max(0, interval - (time.monotonic() - start)))
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            if not is_storage_full(exc):
                store.quality(time.time(), "WORKER_ERROR", error=str(exc), session=session)
                raise
            halted = storage_full(exc)
        finally:
            stopped = time.time()
            try:
                with store.transaction():
                    store.db.execute("UPDATE worker SET stopped=? WHERE slot=1", (stopped,))
                    if halted:
                        store.quality(stopped, "WORKER_STORAGE_STOP", session=session, storage=halted)
                    store.quality(stopped, "WORKER_STOPPED", session=session)
            except (sqlite3.Error, OSError) as exc:
                if halted is None and not is_storage_full(exc):
                    raise
                halted = (halted or storage_full(exc)) | {
                    "shutdown_recorded": False, "shutdown_error": str(exc),
                }
            else:
                if halted:
                    halted |= {"shutdown_recorded": True}
    return halted


def request_stop(store: Store) -> bool:
    result = store.db.execute("UPDATE worker SET stop_requested=1 WHERE slot=1 AND stopped IS NULL")
    return bool(result.rowcount)
