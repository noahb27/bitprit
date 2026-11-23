import errno
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from quarter_hour.cli import main
from quarter_hour.disk import RESERVE_BYTES, WARNING_BYTES, storage_status
from quarter_hour.storage import Store
from quarter_hour.worker import run_worker

from .fixtures import START, snapshot


class StorageTests(unittest.TestCase):
    def test_dedicated_volume_limit_counts_backups_and_wal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "quarter-hour.sqlite3"
            source.write_bytes(b"db")
            wal = Path(str(source) + "-wal")
            wal.write_bytes(b"wal")
            nested = root / "copies"
            nested.mkdir()
            with (nested / "old-backup.sqlite3").open("wb") as old:
                old.truncate(460_000_000)
            reading = storage_status(source, 500_000_000)
            self.assertEqual(reading["database_bytes"], 2)
            self.assertEqual(reading["wal_bytes"], 3)
            self.assertGreaterEqual(reading["volume_file_bytes"], 460_000_005)
            self.assertEqual(reading["state"], "stop")
            self.assertLessEqual(reading["headroom_bytes"], 40_000_000)

    def test_no_database_writes_on_preflight_stop(self):
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()) as out:
            database = Path(folder) / "new" / "quarter-hour.sqlite3"
            args = ["--db", str(database), "--volume-limit-mb", "50"]
            self.assertEqual(main(args + ["run"]), 0)
            self.assertFalse(database.parent.exists())
            reading = json.loads(out.getvalue())["storage_stop"]
            self.assertEqual(reading["reason"], "LOW_STORAGE")
            self.assertEqual(reading["headroom_bytes"], RESERVE_BYTES)
            self.assertFalse(reading["shutdown_recorded"])

    def test_low_space_warning_and_clean_stop_before_next_cycle(self):
        with tempfile.TemporaryDirectory() as folder, Store(Path(folder) / "quarter-hour.sqlite3") as store:
            readings = (
                {"state": "ok"}, {"state": "warning", "headroom_bytes": WARNING_BYTES},
                {"state": "stop", "reason": "LOW_STORAGE", "headroom_bytes": RESERVE_BYTES},
            )
            reader = Mock(side_effect=readings)
            publish_warning = Mock()
            row = snapshot()
            store.save_market(row.market)
            collector = Mock()
            collector.collect.side_effect = lambda: store.save_snapshot(row)
            with patch("quarter_hour.worker.storage_status", reader), patch(
                "quarter_hour.worker.Collector", return_value=collector,
            ):
                halted = run_worker(store, interval=0.001, timeout=1, cycles=None, on_storage_warning=publish_warning)
            publish_warning.assert_called_once_with(readings[1])
            self.assertEqual(collector.collect.call_count, 1)
            self.assertTrue(halted["shutdown_recorded"])
            self.assertEqual(store.db.execute("SELECT count(*) FROM observations").fetchone()[0], 1)
            self.assertIsNotNone(store.db.execute("SELECT stopped FROM worker").fetchone()[0])
            codes = [row[0] for row in store.db.execute("SELECT code FROM quality ORDER BY id")]
            self.assertEqual(codes, ["WORKER_STARTED", "WORKER_STORAGE_STOP", "WORKER_STOPPED"])

    def test_sqlite_full_rolls_back_and_preserves_committed_data(self):
        with tempfile.TemporaryDirectory() as folder, Store(Path(folder) / "quarter-hour.sqlite3") as store:
            row = snapshot()
            store.save_market(row.market)
            store.save_snapshot(row)
            pages = store.db.execute("PRAGMA page_count").fetchone()[0]
            store.db.execute(f"PRAGMA max_page_count={pages}")

            def write_large_response():
                store.raw("fixture", START, START + 1, 200, "x" * 500_000)

            collector = Mock()
            collector.collect.side_effect = write_large_response
            with patch("quarter_hour.worker.Collector", return_value=collector):
                halted = run_worker(store, interval=0.001, timeout=1, cycles=1)
            self.assertEqual(halted["reason"], "STORAGE_FULL")
            self.assertIn("database or disk is full", halted["error"])
            self.assertEqual(store.db.execute("SELECT count(*) FROM raw_responses").fetchone()[0], 0)
            self.assertEqual(store.last_snapshot().ticker, row.ticker)
            self.assertEqual(store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_full_disk_can_prevent_shutdown_log_without_masking_error(self):
        with tempfile.TemporaryDirectory() as folder, Store(Path(folder) / "quarter-hour.sqlite3") as store:
            collector = Mock()
            collector.collect.side_effect = OSError(errno.ENOSPC, "volume filled")
            quality = store.quality

            def write_event(at, code, **detail):
                if code == "WORKER_STOPPED":
                    raise OSError(errno.ENOSPC, "no room for shutdown record")
                return quality(at, code, **detail)

            with patch.object(store, "quality", side_effect=write_event), patch(
                "quarter_hour.worker.Collector", return_value=collector,
            ):
                halted = run_worker(store, interval=0.001, timeout=1, cycles=1)
            self.assertIn("volume filled", halted["error"])
            self.assertFalse(halted["shutdown_recorded"])
            self.assertIn("no room for shutdown record", halted["shutdown_error"])
            self.assertEqual(store.db.execute("SELECT count(*) FROM quality").fetchone()[0], 1)
