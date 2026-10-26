#!/usr/bin/env python3
"""Refresh final scores and settle Phase 5 paper decisions."""

from __future__ import annotations

import argparse
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
from sports_analytics_pipeline.paper import paper_report, settle_paper_decisions
from scripts.run_collectors import collect_nflverse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--skip-refresh", action="store_true")
    args = parser.parse_args()
    refresh = {"status": "skipped", "reason": "--skip-refresh was used"}
    if not args.skip_refresh:
        refresh = collect_nflverse(
            SimpleNamespace(env=args.env, season=args.season, include_pbp=False)
        )
    settings = Settings.load(args.env)
    db = connect(settings.database_path)
    try:
        initialize_schema(db)
        seed_reference_data(db)
        print(json.dumps({
            "schedule_refresh": refresh,
            "settlement": settle_paper_decisions(db),
            "paper_report": paper_report(db),
        }, indent=2))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
