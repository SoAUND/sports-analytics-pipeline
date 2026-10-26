#!/usr/bin/env python3
"""Show the latest MLB Phase 7 operations run summary."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.mlb_operations import latest_mlb_operations_summary

def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env",default=".env")
    args=parser.parse_args()
    settings=Settings.load(args.env)
    db=connect(settings.database_path)
    try:
        initialize_schema(db)
        print(json.dumps(latest_mlb_operations_summary(db) or {"status":"no_runs"},indent=2,sort_keys=True))
    finally:
        db.close()
    return 0
if __name__ == "__main__": raise SystemExit(main())
