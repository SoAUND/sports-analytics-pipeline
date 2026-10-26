#!/usr/bin/env python3
"""Refresh one official MLB schedule date and settle MLB paper decisions only."""
from __future__ import annotations
import argparse, json, sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sports_analytics_pipeline.collectors.base import MappingTeamResolver
from sports_analytics_pipeline.collectors.http import UrllibTransport
from sports_analytics_pipeline.collectors.mlb_official import MappingStadiumResolver, MlbScheduleCollector
from sports_analytics_pipeline.collectors.storage import CollectorRepository
from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import (
    alias_mapping,
    seed_reference_data,
    stadium_alias_mapping,
    stadium_provider_id_mapping,
)
from sports_analytics_pipeline.paper import paper_report, settle_paper_decisions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", help="Official MLB schedule date in YYYY-MM-DD form")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--skip-refresh", action="store_true")
    args = parser.parse_args()
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit("date must use YYYY-MM-DD") from exc

    settings = Settings.load(args.env)
    if settings.bot_mode not in {"research", "test_only"}:
        raise SystemExit("MLB Phase 5 requires a supported research mode")
    db = connect(settings.database_path)
    try:
        initialize_schema(db)
        seed_reference_data(db, include_mlb=True)
        refresh = {"status": "skipped", "reason": "--skip-refresh was used"}
        if not args.skip_refresh:
            transport = UrllibTransport(timeout_seconds=settings.http_timeout_seconds, attempts=settings.http_retry_attempts)
            collector = MlbScheduleCollector(
                transport=transport,
                team_resolver=MappingTeamResolver(alias_mapping(db, "mlb")),
                stadium_resolver=MappingStadiumResolver(
                    stadium_alias_mapping(db, "mlb"),
                    provider_ids=stadium_provider_id_mapping("mlb"),
                ),
            )
            batch = collector.collect_date(args.date)
            stored = CollectorRepository(db).persist_mlb_schedule(batch)
            refresh = {"status": "succeeded", "received": len(batch.games), "stored": stored, "warnings": list(batch.warnings)}
        settlement = settle_paper_decisions(db, as_of=datetime.now(timezone.utc), sport="mlb")
        print(json.dumps({
            "schedule_refresh": refresh,
            "settlement": settlement,
            "paper_report": paper_report(db, sport="mlb"),
        }, indent=2, sort_keys=True))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

