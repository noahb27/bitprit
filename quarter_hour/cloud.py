"""Railway entry point: refuse ephemeral storage and retain paper-only CLI behavior."""

import signal
import sys
from pathlib import Path


def _interrupt(signum, frame):
    # The existing worker handles KeyboardInterrupt and records a clean stop.
    raise KeyboardInterrupt


def main():
    data = Path("/data")
    if not data.is_mount():
        print("Cloud worker refused: attach a persistent volume at /data first.", file=sys.stderr)
        return 2
    from .cli import main as cli_main

    previous = signal.signal(signal.SIGTERM, _interrupt)
    try:
        return cli_main(["--db", str(data / "quarter-hour.sqlite3"), "run"])
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
