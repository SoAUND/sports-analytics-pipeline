#!/usr/bin/env python3
"""Run MLB test-only paper operations with date locking and bounded retries."""
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
    build_reliable_operations_command,
    execute_reliable_mlb_operations,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()

    settings = Settings.load(args.env)
    db = connect(settings.database_path)
    initialize_schema(db)
    try:
        command = build_reliable_operations_command(
            operation_date=args.date,
            env_path=args.env,
            hours=args.hours,
        )
        output = execute_reliable_mlb_operations(
            db,
            project_root=PROJECT_ROOT,
            operation_date=args.date,
            bot_mode=settings.bot_mode,
            command=command,
            max_attempts=args.attempts,
        )
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if output["status"] in {"succeeded", "skipped_duplicate"} else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
