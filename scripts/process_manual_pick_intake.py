#!/usr/bin/env python3
"""Import zero-credit manual/video pick JSON files into the tracker ledger."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from sports_analytics_pipeline.manual_pick_groups import PICK_GROUPS, ensure_pick_group_schema, set_pick_group
from sports_analytics_pipeline.manual_pick_tracker import create_ticket, ensure_manual_pick_tracker_schema

UTC = timezone.utc


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def main() -> None:
    parser = argparse.ArgumentParser(description="Process manual/video pick intake files")
    parser.add_argument("--database", default="var/manual_pick_tracker.sqlite3")
    parser.add_argument("--input-dir", default="data/manual_pick_intake")
    parser.add_argument("--report", default="var/manual-pick-intake-report.json")
    args = parser.parse_args()

    Path(args.database).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    ensure_manual_pick_tracker_schema(connection)
    ensure_pick_group_schema(connection)

    imported: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []
    input_dir = Path(args.input_dir)
    for path in sorted(input_dir.glob("*.json")) if input_dir.exists() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("root must be an object")
            ticket_id = str(payload.get("ticket_id") or path.stem)
            exists = connection.execute(
                "SELECT 1 FROM manual_pick_tickets WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
            if exists:
                skipped.append(ticket_id)
                continue
            legs = payload.get("legs")
            if not isinstance(legs, list) or not legs:
                raise ValueError("legs must be a non-empty array")
            pick_group = str(payload.get("pick_group") or "").strip().casefold()
            if pick_group not in PICK_GROUPS:
                raise ValueError("pick_group must be 'system' or 'recommended'")
            create_ticket(
                connection,
                ticket_id=ticket_id,
                legs=legs,
                source_kind=str(payload.get("source_kind") or "chatgpt_video"),
                source_reference=payload.get("source_reference"),
                sportsbook_key=payload.get("sportsbook_key"),
                offered_american_odds=payload.get("offered_american_odds"),
                offered_decimal_odds=payload.get("offered_decimal_odds"),
                notes=str(payload.get("notes") or ""),
                created_at=_parse_time(payload.get("created_at")),
            )
            set_pick_group(connection, ticket_id, pick_group)
            imported.append(ticket_id)
        except Exception as exc:  # fail one intake file without corrupting the ledger
            connection.rollback()
            failed.append({"file": str(path), "error": str(exc)})

    report = {
        "imported": imported,
        "skipped_existing": skipped,
        "failed": failed,
        "paid_sports_data_credits_used": 0,
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    connection.close()
    print(json.dumps(report, sort_keys=True))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
