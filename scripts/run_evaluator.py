#!/usr/bin/env python3
"""Evaluate upcoming NFL spreads with our strict test-only strategy."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.evaluation import evaluate_upcoming


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--as-of must include a time-zone offset")
    return parsed.astimezone(timezone.utc)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--env", default=".env", help="Protected settings file")
    result.add_argument("--as-of", type=_timestamp, help="Auditable ISO-8601 cutoff")
    result.add_argument("--hours", type=int, default=24, help="Upcoming window (default: 24)")
    result.add_argument("--game-id", help="Evaluate only one stored game")
    result.add_argument(
        "--no-persist", action="store_true", help="Preview without writing decisions"
    )
    return result


def main() -> int:
    args = parser().parse_args()
    if args.hours < 1 or args.hours > 168:
        raise SystemExit("--hours must be between 1 and 168")
    settings = Settings.load(args.env)
    db = connect(settings.database_path)
    try:
        initialize_schema(db)
        seed_reference_data(db)
        output = evaluate_upcoming(
            db,
            as_of=args.as_of or datetime.now(timezone.utc),
            hours=args.hours,
            game_id=args.game_id,
            stale_after_minutes=settings.odds_stale_after_minutes,
            persist=not args.no_persist,
        )
        print(json.dumps(output, indent=2))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
