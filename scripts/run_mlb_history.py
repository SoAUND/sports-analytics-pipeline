#!/usr/bin/env python3
"""Collect final MLB regular-season history for Phase 4 model development."""
from __future__ import annotations
import argparse, json
from pathlib import Path
import sys
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from sports_analytics_pipeline.collectors.base import MappingTeamResolver
from sports_analytics_pipeline.collectors.http import UrllibTransport
from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import alias_mapping, seed_reference_data
from sports_analytics_pipeline.mlb_history import collect_historical_mlb_season

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seasons", default="2020,2021,2022,2023,2024,2025,2026")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    seasons = tuple(int(item.strip()) for item in args.seasons.split(",") if item.strip())
    settings = Settings.load(args.env)
    if settings.bot_mode not in {"research", "test_only"}:
        raise SystemExit("MLB Phase 4 requires a supported research mode")
    db = connect(settings.database_path)
    initialize_schema(db)
    seed_reference_data(db, include_mlb=True)
    resolver = MappingTeamResolver(alias_mapping(db, "mlb"))
    transport = UrllibTransport(timeout_seconds=settings.http_timeout_seconds, attempts=settings.http_retry_attempts)
    results = [collect_historical_mlb_season(db, transport=transport, resolver=resolver, season=season) for season in seasons]
    print(json.dumps({"mode":"test_only", "seasons":results}, indent=2, sort_keys=True))
    db.close()
    return 0
if __name__ == "__main__":
    raise SystemExit(main())

