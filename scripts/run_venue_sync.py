#!/usr/bin/env python3
"""Synchronize safe venue-name changes without consuming betting API requests."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
import urllib.request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sports_analytics_pipeline.venue_identity import SUPPORTED_VENUE_SPORTS
from sports_analytics_pipeline.venue_sync import (
    NFLVERSE_SCHEDULE_URL,
    default_window,
    fetch_espn_observations,
    fetch_mlb_observations,
    fetch_nflverse_name_observations,
    synchronize_catalogs,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sports",
        default="all",
        help="Comma-separated sports or 'all' for NFL, MLB, NBA, WNBA, EPL, MMA, ATP, WTA",
    )
    parser.add_argument("--apply", action="store_true", help="Write safe links and name changes")
    parser.add_argument("--report", default="var/venue-sync-report.json")
    parser.add_argument("--start", help="Observation-window start in YYYY-MM-DD")
    parser.add_argument("--end", help="Observation-window end in YYYY-MM-DD")
    args = parser.parse_args()

    sports = sorted(SUPPORTED_VENUE_SPORTS) if args.sports == "all" else [
        value.casefold().strip() for value in args.sports.split(",") if value.strip()
    ]
    default_start, default_end = default_window()
    starting = date.fromisoformat(args.start) if args.start else default_start
    ending = date.fromisoformat(args.end) if args.end else default_end
    if ending < starting:
        raise SystemExit("--end cannot be earlier than --start")

    observations = []
    source_status = {}
    if "mlb" in sports:
        try:
            rows = fetch_mlb_observations()
            observations.extend(rows)
            source_status["mlb_statsapi"] = {"status": "ok", "venues": len(rows)}
        except Exception as exc:
            source_status["mlb_statsapi"] = {"status": "unavailable", "error": str(exc)}

    for sport in sports:
        if sport not in {"mlb"}:
            try:
                rows = fetch_espn_observations(sport, starting=starting, ending=ending)
                observations.extend(rows)
                source_status[f"espn_{sport}"] = {"status": "ok", "venues": len(rows)}
            except Exception as exc:
                source_status[f"espn_{sport}"] = {"status": "unavailable", "error": str(exc)}

    if "nfl" in sports:
        try:
            request = urllib.request.Request(
                NFLVERSE_SCHEDULE_URL,
                headers={"User-Agent": "OurSportsBotVenueSync/1.0"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode("utf-8")
            rows = fetch_nflverse_name_observations(season=ending.year, text=text)
            observations.extend(rows)
            source_status["nflverse"] = {"status": "ok", "venues": len(rows)}
        except Exception as exc:
            source_status["nflverse"] = {"status": "unavailable", "error": str(exc)}

    report = synchronize_catalogs(
        PROJECT_ROOT,
        observations,
        sports=sports,
        apply=args.apply,
    )
    report.update(
        {
            "window": {"start": starting.isoformat(), "end": ending.isoformat()},
            "source_status": source_status,
            "betting_api_requests_used": 0,
        }
    )
    report_path = PROJECT_ROOT / args.report
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"counts": report["counts"], "source_status": source_status}, indent=2))
    print(f"Full venue sync report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

