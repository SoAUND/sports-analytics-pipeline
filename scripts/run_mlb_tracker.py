#!/usr/bin/env python3
"""Run the MLB paper model/evaluation pipeline with optional readiness gating."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.mlb_evaluation import evaluate_mlb_upcoming
from sports_analytics_pipeline.mlb_live_paper import (
    latest_live_paper_run_for_date,
    ready_game_ids_from_report,
)
from sports_analytics_pipeline.mlb_modeling import create_upcoming_mlb_model_runs
from sports_analytics_pipeline.paper import paper_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", nargs="?", help="Optional explicit YYYY-MM-DD date to collect first")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--game-id")
    parser.add_argument("--skip-pull", action="store_true")
    parser.add_argument("--require-readiness", action="store_true")
    parser.add_argument("--operation-date")
    parser.add_argument("--model-runs-already-created", action="store_true")
    args = parser.parse_args()
    if args.hours < 1 or args.hours > 168:
        raise SystemExit("--hours must be between 1 and 168")
    if not args.skip_pull and not args.date:
        raise SystemExit("Provide YYYY-MM-DD or use --skip-pull")
    if args.require_readiness and not args.operation_date:
        raise SystemExit("--operation-date is required with --require-readiness")

    settings = Settings.load(args.env)
    if settings.bot_mode not in {"research", "test_only"}:
        raise SystemExit("MLB Phase 5 requires a supported research mode")

    pull = {"status": "skipped", "reason": "--skip-pull was used"}
    if not args.skip_pull:
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_mlb_collectors.py"),
            args.date,
            "--env",
            args.env,
        ]
        completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
        pull = {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "stdout": completed.stdout[-12000:],
            "stderr": completed.stderr[-4000:],
        }
        if completed.returncode != 0:
            print(json.dumps({
                "collection": pull,
                "notice": "Collection failed; no model/evaluation run was attempted.",
            }, indent=2))
            return completed.returncode

    db = connect(settings.database_path)
    try:
        initialize_schema(db)
        seed_reference_data(db, include_mlb=True)
        now = datetime.now(timezone.utc)

        eligible_ids = None
        readiness = None
        if args.require_readiness:
            readiness = latest_live_paper_run_for_date(db, args.operation_date)
            if readiness is None:
                raise SystemExit("No stored MLB readiness audit exists for --operation-date")
            eligible_ids = ready_game_ids_from_report(readiness)
            if args.game_id:
                eligible_ids = tuple(game for game in eligible_ids if game == args.game_id)

        if args.model_runs_already_created:
            model = {
                "status": "skipped",
                "reason": "Model runs were created by the readiness-gated model step.",
                "ready_game_ids": list(eligible_ids or ()),
            }
        else:
            model = create_upcoming_mlb_model_runs(
                db,
                prediction_time=now,
                hours=args.hours,
                game_id=args.game_id,
                eligible_game_ids=eligible_ids,
            )

        evaluation = evaluate_mlb_upcoming(
            db,
            as_of=now,
            hours=args.hours,
            game_id=args.game_id,
            game_ids=eligible_ids,
            stale_after_minutes=settings.odds_stale_after_minutes,
            persist=True,
        )
        output = {
            "collection": pull,
            "model": model,
            "evaluation": evaluation,
            "paper_report": paper_report(db, sport="mlb"),
            "notice": (
                "MLB paper tracking only. When readiness gating is enabled, blocked games "
                "do not reach model/evaluation and their exact blockers remain in the readiness audit."
            ),
        }
        if readiness is not None:
            output["readiness"] = readiness
            output["ready_game_ids"] = list(eligible_ids or ())
        print(json.dumps(output, indent=2, sort_keys=True))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

