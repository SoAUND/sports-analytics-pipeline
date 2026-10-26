#!/usr/bin/env python3
"""Run the complete MLB Phase 7 daily paper-testing operations sequence."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.mlb_operations import build_daily_mlb_plan, execute_daily_mlb_operations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", help="MLB operation date in YYYY-MM-DD form")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--skip-collection", action="store_true")
    parser.add_argument("--skip-settlement", action="store_true")
    args = parser.parse_args()
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit("date must use YYYY-MM-DD") from exc

    settings = Settings.load(args.env)
    if settings.bot_mode not in {"research", "test_only"}:
        raise SystemExit("MLB Phase 7 requires a supported research mode")

    db = connect(settings.database_path)
    try:
        initialize_schema(db)
        seed_reference_data(db, include_mlb=True)
        plan = build_daily_mlb_plan(
            date=args.date,
            env_path=args.env,
            hours=args.hours,
            skip_collection=args.skip_collection,
            skip_settlement=args.skip_settlement,
        )
        summary = execute_daily_mlb_operations(
            db,
            project_root=PROJECT_ROOT,
            operation_date=args.date,
            bot_mode=settings.bot_mode,
            steps=plan,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if summary["status"] == "succeeded" else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

