#!/usr/bin/env python3
"""Pull regular-season odds, run the model, and lock one paper decision per game."""

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
from sports_analytics_pipeline.paper import paper_report
from scripts.run_collectors import collect_odds


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--as-of must include a time-zone offset")
    return parsed.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--as-of", type=_timestamp)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--game-id")
    parser.add_argument("--skip-pull", action="store_true")
    parser.add_argument(
        "--preseason", action="store_true",
        help="Track preseason as test-only; it never counts toward the 100 target",
    )
    args = parser.parse_args()
    if args.hours < 1 or args.hours > 168:
        raise SystemExit("--hours must be between 1 and 168")

    pull = {"status": "skipped", "reason": "--skip-pull was used"}
    if not args.skip_pull:
        pull = collect_odds(SimpleNamespace(env=args.env), preseason=args.preseason)
    settings = Settings.load(args.env)
    as_of = args.as_of or datetime.now(timezone.utc)
    db = connect(settings.database_path)
    try:
        initialize_schema(db)
        seed_reference_data(db)
        model = run_phase4(
            db, as_of=as_of, hours=args.hours, game_id=args.game_id,
            stale_after_minutes=settings.odds_stale_after_minutes, persist=True,
        )
        evaluation = evaluate_upcoming(
            db, as_of=as_of, hours=args.hours, game_id=args.game_id,
            stale_after_minutes=settings.odds_stale_after_minutes, persist=True,
        )
        print(json.dumps({
            "odds_pull": pull,
            "model": model,
            "evaluation": evaluation,
            "paper_report": paper_report(db),
            "notice": "One immutable paper decision per game. No real-money action is taken.",
        }, indent=2))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
