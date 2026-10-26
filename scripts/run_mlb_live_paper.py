#!/usr/bin/env python3
"""Run one MLB Phase 8 current-data paper cycle and audit its readiness."""
from __future__ import annotations
import argparse, json, sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.mlb_live_paper import build_live_paper_readiness, store_live_paper_run
from sports_analytics_pipeline.mlb_operations import build_daily_mlb_plan, execute_daily_mlb_operations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", help="Explicit YYYY-MM-DD MLB operation date")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--skip-collection", action="store_true")
    parser.add_argument("--skip-settlement", action="store_true")
    args = parser.parse_args()

    settings = Settings.load(args.env)
    if settings.bot_mode not in {"research", "test_only"}:
        raise SystemExit("MLB Phase 8 requires a supported research mode")
    db = connect(settings.database_path)
    try:
        initialize_schema(db)
        seed_reference_data(db, include_mlb=True)
        plan = build_daily_mlb_plan(
            date=args.date, env_path=args.env, hours=args.hours,
            skip_collection=args.skip_collection, skip_settlement=args.skip_settlement,
        )
        operations = execute_daily_mlb_operations(
            db, project_root=PROJECT_ROOT, operation_date=args.date,
            bot_mode=settings.bot_mode, steps=plan,
        )
        if operations["status"] != "succeeded":
            print(json.dumps({
                "operations": operations,
                "notice": "Required operation failed; Phase 8 readiness was not treated as successful. No real-money action was created."
            }, indent=2, sort_keys=True))
            return 1
        report = build_live_paper_readiness(
            db, operation_date=args.date, as_of=datetime.now(timezone.utc),
            stale_after_minutes=settings.odds_stale_after_minutes,
        )
        live_run_id = store_live_paper_run(db, report=report, operations_run_id=operations["run_id"])
        report["live_run_id"] = live_run_id
        report["operations_run_id"] = operations["run_id"]
        print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        db.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

