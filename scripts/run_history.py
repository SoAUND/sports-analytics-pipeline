#!/usr/bin/env python3
"""Download the reviewed 2020-2025 regular-season model history once."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sports_analytics_pipeline.collectors.base import MappingTeamResolver
from sports_analytics_pipeline.collectors.http import UrllibTransport
from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import alias_mapping, seed_reference_data
from sports_analytics_pipeline.history import collect_historical_training_games


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env", help="Protected settings file")
    args = parser.parse_args()
    settings = Settings.load(args.env)
    db = connect(settings.database_path)
    try:
        initialize_schema(db)
        seed_reference_data(db)
        output = collect_historical_training_games(
            db,
            transport=UrllibTransport(
                timeout_seconds=settings.http_timeout_seconds,
                attempts=settings.http_retry_attempts,
            ),
            resolver=MappingTeamResolver(alias_mapping(db)),
        )
        print(json.dumps(output, indent=2))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
