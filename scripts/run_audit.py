#!/usr/bin/env python3
"""Print the short Phase 4.1 candidate-versus-market model verdict."""

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
from sports_analytics_pipeline.modeling import train_first_model


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env", help="Protected settings file")
    args = parser.parse_args()
    settings = Settings.load(args.env)
    db = connect(settings.database_path)
    try:
        initialize_schema(db)
        seed_reference_data(db)
        _, report = train_first_model(
            db, as_of=datetime.now(timezone.utc), persist=True
        )
        metrics = report.get("metrics", {})
        print(
            json.dumps(
                {
                    "decision": report.get("status"),
                    "candidate_brier": metrics.get("candidate_brier_on_market_rows"),
                    "market_brier": metrics.get("market_baseline_brier"),
                    "candidate_log_loss": metrics.get(
                        "candidate_log_loss_on_market_rows"
                    ),
                    "market_log_loss": metrics.get("market_baseline_log_loss"),
                    "market_coverage": metrics.get("market_baseline_coverage"),
                    "checks": report.get("acceptance_checks", {}),
                    "next": (
                        "Do not force a pick. Continue 2026 regular-season paper tracking."
                        if report.get("status") == "rejected"
                        else "The model may create test-only runs; all pick gates still apply."
                    ),
                },
                indent=2,
            )
        )
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
