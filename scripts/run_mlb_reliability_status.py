#!/usr/bin/env python3
"""Show MLB test-only paper-operations reliability health."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.mlb_reliability import (
    build_mlb_reliability_health,
    latest_mlb_reliability_summary,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()

    settings = Settings.load(args.env)
    db = connect(settings.database_path)
    initialize_schema(db)
    try:
        output = {
            "health": build_mlb_reliability_health(db),
            "latest": latest_mlb_reliability_summary(db),
        }
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
