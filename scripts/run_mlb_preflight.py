#!/usr/bin/env python3
"""Fail-closed preflight for a real MLB paper-testing run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.mlb_persistence import validate_mlb_paper_database


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--require-model", action="store_true")
    args = parser.parse_args()

    settings = Settings.load(args.env)
    if settings.bot_mode not in {"research", "test_only"}:
        raise SystemExit("Unsupported research mode")
    settings.require_odds_api_key()
    if not settings.sportsbook_lists_approved():
        raise SystemExit("Accessible and market-consensus sportsbook lists are required")
    settings.require_nws_user_agent()

    health = validate_mlb_paper_database(
        settings.database_path,
        require_model_artifact=args.require_model,
    )
    print(
        json.dumps(
            {
                "status": "ready",
                "mode": "test_only",
                "settings": settings.safe_summary(),
                "database": health,
                "automatic_wagering": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

