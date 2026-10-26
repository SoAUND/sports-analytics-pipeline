#!/usr/bin/env python3
"""Pull current odds, run Phase 4, and return the best straight/parlay screen."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.evaluation import evaluate_upcoming
from sports_analytics_pipeline.modeling import run_phase4
from scripts.run_collectors import collect_odds


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--as-of must include a time-zone offset")
    return parsed.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env", help="Protected settings file")
    parser.add_argument("--as-of", type=_timestamp, help="Auditable ISO-8601 cutoff")
    parser.add_argument("--hours", type=int, default=24, help="Upcoming window")
    parser.add_argument("--game-id", help="Evaluate only one stored game")
    parser.add_argument(
        "--skip-pull", action="store_true", help="Reuse stored odds without an API request"
    )
    parser.add_argument("--no-persist", action="store_true", help="Preview only")
    args = parser.parse_args()
    if args.hours < 1 or args.hours > 168:
        raise SystemExit("--hours must be between 1 and 168")

    pull = {"status": "skipped", "reason": "--skip-pull was used"}
    if not args.skip_pull:
        pull = collect_odds(SimpleNamespace(env=args.env), preseason=True)

    settings = Settings.load(args.env)
    as_of = args.as_of or datetime.now(timezone.utc)
    db = connect(settings.database_path)
    try:
        initialize_schema(db)
        seed_reference_data(db)
        model = run_phase4(
            db,
            as_of=as_of,
            hours=args.hours,
            game_id=args.game_id,
            stale_after_minutes=settings.odds_stale_after_minutes,
            persist=not args.no_persist,
        )
        evaluation = evaluate_upcoming(
            db,
            as_of=as_of,
            hours=args.hours,
            game_id=args.game_id,
            stale_after_minutes=settings.odds_stale_after_minutes,
            persist=not args.no_persist,
        )
        print(
            json.dumps(
                {
                    "odds_pull": pull,
                    "model": model,
                    "evaluation": evaluation,
                    "notice": "No real-money action is taken. Preseason results remain test-only.",
                },
                indent=2,
            )
        )
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
