"""Local settings and strict policy validation."""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .domain import seconds
from .policy import Policy


@dataclass(frozen=True)
class Settings:
    database: Path = Path("data/quarter-hour.sqlite3")
    interval: float = 5.0
    timeout: float = 3.0
    policy: Policy = field(default_factory=Policy)

    @classmethod
    def read(cls, path: Path | None):
        if path is None:
            default = Path("local.toml")
            if not default.exists():
                return cls()
            path = default
        with path.open("rb") as stream:
            options = tomllib.load(stream)
        unknown = set(options) - {"database", "interval_seconds", "request_timeout_seconds", "paper"}
        if unknown:
            raise ValueError(f"unknown configuration fields: {', '.join(sorted(unknown))}")
        return cls(
            Path(options.get("database", "data/quarter-hour.sqlite3")),
            seconds(options.get("interval_seconds", 5), "interval_seconds", 1, 15),
            seconds(options.get("request_timeout_seconds", 3), "request_timeout_seconds", .25, 10),
            Policy(**options.get("paper", {})),
        )
