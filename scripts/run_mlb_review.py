#!/usr/bin/env python3
"""Generate and store the MLB Phase 6 validation review snapshot."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.mlb_validation import build_mlb_validation_report, store_mlb_validation_review


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--target", type=int, default=100)
    parser.add_argument("--no-store", action="store_true")
    args = parser.parse_args()
    settings = Settings.load(args.env)
    db = connect(settings.database_path)
    try:
        initialize_schema(db)
        seed_reference_data(db, include_mlb=True)
        report = build_mlb_validation_report(db, target=args.target)
        if not args.no_store:
            report["review_id"] = store_mlb_validation_review(db, report=report)
        print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        db.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
