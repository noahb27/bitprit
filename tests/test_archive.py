import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from quarter_hour.archive import backup_database, file_summary, verify_backup
from quarter_hour.cli import main
from quarter_hour.journal import PaperWorker
from quarter_hour.policy import Policy
from quarter_hour.storage import Store

from .fixtures import result_for, snapshot


class ArchiveTests(unittest.TestCase):
    def test_archives_committed_wal_as_a_standalone_file_with_a_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "live.sqlite3"
            backup = root / "copies" / "backup.sqlite3"
            manifest_path = root / "manifest.json"
            with Store(source) as store:
                store.db.execute("PRAGMA wal_autocheckpoint=0")
                store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                row = snapshot()
                store.save_market(row.market)
                row = store.save_snapshot(row)
                store.raw("fixture book", row.at - .1, row.at, 200, {"bids": [[".2", "10"]]})
                PaperWorker(store, Policy()).record(row, now=row.at)
                result = result_for(row)
                store.save_result(row.market, result)
                wal = Path(str(source) + "-wal")
                hashes_before = (file_summary(source), file_summary(wal))
                self.assertGreater(hashes_before[1]["bytes"], 0)
                with patch("quarter_hour.cli.Store") as store_constructor, redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(["--db", str(source), "backup", str(backup)]), 0)
                    store_constructor.assert_not_called()
                self.assertEqual((file_summary(source), file_summary(wal)), hashes_before)
                manifest = json.loads(output.getvalue())
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8-sig")

            self.assertEqual(manifest["schema_version"], 3)
            for table in ("observations", "results", "forecasts", "decisions", "raw_responses"):
                self.assertEqual(manifest["counts"][table], 1, table)
            self.assertEqual(manifest["observation_range"]["first_id"], row.id)
            self.assertFalse(Path(str(backup) + "-wal").exists())
            self.assertEqual(verify_backup(backup, manifest=manifest_path)["integrity"], "ok")
            manifest["sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest mismatch for sha256"):
                verify_backup(backup, manifest=manifest_path)
            with self.assertRaisesRegex(ValueError, "destination must be a new file"):
                backup_database(source, backup)

    def test_off_volume_backup_preflight_and_verification_preserve_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, destination = root / "original.sqlite3", root / "new-copy" / "backup.sqlite3"
            with Store(source):
                original = source.read_bytes()
                with self.assertRaisesRegex(ValueError, "different filesystem"):
                    backup_database(source, destination, off_volume=True)
                self.assertFalse(destination.parent.exists())
                self.assertEqual(source.read_bytes(), original)
            missing = root / "missing.sqlite3"
            with self.assertRaises(FileNotFoundError):
                backup_database(missing, destination)
            self.assertFalse(missing.exists())
            self.assertFalse(destination.exists())

    def test_verification_rejects_a_wal_sidecar(self):
        with tempfile.TemporaryDirectory() as folder, Store(Path(folder) / "live.sqlite3") as store:
            store.quality(0, "FIXTURE")
            with self.assertRaisesRegex(ValueError, "standalone"):
                verify_backup(store.path)
