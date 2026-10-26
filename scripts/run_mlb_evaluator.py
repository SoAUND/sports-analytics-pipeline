#!/usr/bin/env python3
"""Evaluate upcoming stored MLB moneylines under our strict Phase 3 rules."""

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
from sports_analytics_pipeline.mlb_evaluation import evaluate_mlb_upcoming


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate upcoming MLB full-game moneylines in test_only mode"
    )
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--game-id")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--no-persist", action="store_true")
    args = parser.parse_args()
    if args.hours < 1 or args.hours > 168:
        raise SystemExit("--hours must be between 1 and 168")

    settings = Settings.load(args.env)
    if settings.bot_mode not in {"research", "test_only"}:
        raise SystemExit("MLB Phase 3 requires a supported research mode")

    db = connect(settings.database_path)
    initialize_schema(db)
    seed_reference_data(db, include_mlb=True)
    output = evaluate_mlb_upcoming(
        db,
        as_of=datetime.now(timezone.utc),
        hours=args.hours,
        game_id=args.game_id,
        stale_after_minutes=settings.odds_stale_after_minutes,
        persist=not args.no_persist,
    )
    print(json.dumps(output, indent=2, sort_keys=True))
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

