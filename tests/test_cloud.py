"""Cloud startup must use persistent storage and the normal paper worker."""

import signal
import unittest
from unittest.mock import patch

from quarter_hour import cloud


class CloudTests(unittest.TestCase):
    def test_missing_volume_prevents_recording(self):
        with patch("quarter_hour.cloud.Path.is_mount", return_value=False), \
             patch("quarter_hour.cli.main") as cli:
            self.assertEqual(cloud.main(), 2)
            cli.assert_not_called()

    def test_volume_uses_persistent_database_and_restores_signal_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        with patch("quarter_hour.cloud.Path.is_mount", return_value=True), \
             patch("quarter_hour.cli.main", return_value=0) as cli:
            self.assertEqual(cloud.main(), 0)
            cli.assert_called_once_with(["--db", "/data/quarter-hour.sqlite3", "run"])
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_container_termination_reaches_worker_interrupt_path(self):
        with self.assertRaises(KeyboardInterrupt):
            cloud._interrupt(signal.SIGTERM, None)
