#!/usr/bin/env python3
"""Train the first MLB model or create readiness-gated test-only model runs."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.mlb_live_paper import (
    build_live_paper_readiness,
    model_eligible_game_ids_from_report,
    ready_game_ids_from_report,
    store_live_paper_run,
)
from sports_analytics_pipeline.mlb_modeling import train_mlb_model, create_upcoming_mlb_model_runs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("train")
    run = sub.add_parser("run")
    run.add_argument("--hours", type=int, default=24)
    run.add_argument("--operation-date")
    run.add_argument("--require-readiness", action="store_true")
    args = parser.parse_args()

    settings = Settings.load(args.env)
    if settings.bot_mode not in {"research", "test_only"}:
        raise SystemExit("MLB Phase 4 requires a supported research mode")
    db = connect(settings.database_path)
    initialize_schema(db)
    seed_reference_data(db, include_mlb=True)
    now = datetime.now(timezone.utc)
    try:
        if args.command == "train":
            artifact = train_mlb_model(db, trained_at=now)
            output = asdict(artifact)
            output["trained_at"] = artifact.trained_at.isoformat()
            output["training_cutoff"] = artifact.training_cutoff.isoformat()
        else:
            ready_ids = None
            model_eligible_ids = None
            readiness = None
            if args.require_readiness:
                if not args.operation_date:
                    raise SystemExit("--operation-date is required with --require-readiness")
                readiness = build_live_paper_readiness(
                    db,
                    operation_date=args.operation_date,
                    as_of=now,
                    stale_after_minutes=settings.odds_stale_after_minutes,
                )
                live_run_id = store_live_paper_run(db, report=readiness)
                readiness["live_run_id"] = live_run_id
                ready_ids = ready_game_ids_from_report(readiness)
                model_eligible_ids = model_eligible_game_ids_from_report(readiness)
            output = create_upcoming_mlb_model_runs(
                db,
                prediction_time=now,
                hours=args.hours,
                eligible_game_ids=(
                    model_eligible_ids if args.require_readiness else ready_ids
                ),
            )
            if readiness is not None:
                output["readiness"] = readiness
                output["ready_game_ids"] = list(ready_ids or ())
                output["model_eligible_game_ids"] = list(model_eligible_ids or ())
                output["bookmaker_coverage_warning_game_ids"] = [
                    game["game_id"]
                    for game in readiness.get("games", [])
                    if game.get("bookmaker_coverage_warnings")
                ]
                output["blocked_game_ids"] = [
                    game["game_id"]
                    for game in readiness.get("games", [])
                    if game.get("status") == "blocked"
                ]
                output["notice"] = (
                    "Games with complete material research and at least one fresh, complete, "
                    "trustworthy two-sided market may receive a test-only model screen. Missing "
                    "individual bookmakers are coverage warnings and do not block qualification."
                )
        print(json.dumps(output, indent=2, sort_keys=True))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

