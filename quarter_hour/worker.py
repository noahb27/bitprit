"""Foreground worker lifecycle, independent of the local reader."""

import time
from uuid import uuid4

from .collector import Collector, PublicGet
from .storage import Store


def run_worker(
    store: Store, *, interval: float, timeout: float, cycles: int | None,
    on_sample=None, priority=None,
):
    if cycles is not None and cycles < 1:
        raise ValueError("cycles must be positive")
    with store.worker_lock():
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
        try:
            collector = Collector(store, PublicGet(store, timeout=timeout), interval=interval)
            count = 0
            while cycles is None or count < cycles:
                if store.db.execute("SELECT stop_requested FROM worker WHERE slot=1").fetchone()[0]:
                    break
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
            store.quality(time.time(), "WORKER_ERROR", error=str(exc), session=session)
            raise
        finally:
            stopped = time.time()
            with store.transaction():
                store.db.execute("UPDATE worker SET stopped=? WHERE slot=1", (stopped,))
                store.quality(stopped, "WORKER_STOPPED", session=session)


def request_stop(store: Store) -> bool:
    result = store.db.execute("UPDATE worker SET stop_requested=1 WHERE slot=1 AND stopped IS NULL")
    return bool(result.rowcount)
