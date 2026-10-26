#!/usr/bin/env python3
"""Apply reviewed result manifests and edit the original Telegram tracker messages."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from urllib.error import HTTPError

from sports_analytics_pipeline.manual_pick_tracker import settle_leg
from sports_analytics_pipeline.manual_pick_groups import get_pick_group, render_grouped_ticket_message
from send_telegram_notification import edit_message
from verify_telegram_bot import main as verify_bot


def apply_results(db, payload):
    # Validate the complete manifest before changing any leg.
    resolved = []
    seen = set()
    for item in payload["legs"]:
        key = (item["ticket_id"], item["leg_order"])
        if key in seen:
            raise ValueError("Duplicate settlement leg")
        seen.add(key)
        row = db.execute(
            "SELECT * FROM manual_pick_legs WHERE ticket_id=? AND leg_order=?", key
        ).fetchone()
        if row is None or row["selection"] != item["selection"]:
            raise ValueError(f"Selection mismatch: {key}")
        if row["entry_american_odds"] != item["entry_american_odds"]:
            raise ValueError(f"Entry odds mismatch: {key}")
        if get_pick_group(db, item["ticket_id"]) != item["group"]:
            raise ValueError(f"Group mismatch: {key}")
        if item["result"] not in {"win", "loss", "push", "void", "cancelled"}:
            raise ValueError("A confirmed terminal result is required")
        if not item["final"] or not item["source_url"].startswith("https://"):
            raise ValueError("Result and source URL are required")
        if row["result_status"] != "pending" and (
            row["result_status"] != item["result"]
            or row["final_score_or_result"] != item["final"]
            or row["result_source_url"] != item["source_url"]
        ):
            raise ValueError(f"Conflicting existing settlement: {key}")
        resolved.append((row, item))
    for row, item in resolved:
        if row["result_status"] == "pending":
            settle_leg(db, leg_id=row["leg_id"], result_status=item["result"],
                       final_score_or_result=item["final"],
                       result_source_key="reviewed_public_result",
                       result_source_url=item["source_url"])
    return sorted({item["ticket_id"] for _, item in resolved})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--input-dir", default="data/manual_pick_settlements")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    db = sqlite3.connect(args.database)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    verified = False
    for path in sorted(Path(args.input_dir).glob("*.json")):
        payload = json.loads(path.read_text())
        stage = "result:" + hashlib.sha256(path.read_bytes()).hexdigest()[:20]
        for ticket in apply_results(db, payload):
            if db.execute(
                "SELECT 1 FROM manual_pick_notifications WHERE ticket_id=? AND stage=? AND channel='telegram'",
                (ticket, stage)
            ).fetchone():
                continue
            entry = db.execute(
                "SELECT telegram_message_id FROM manual_pick_notifications "
                "WHERE ticket_id=? AND stage='entry' AND channel='telegram' "
                "ORDER BY notification_id DESC LIMIT 1", (ticket,)
            ).fetchone()
            if entry is None or not entry[0]:
                raise ValueError(f"No original Telegram message for {ticket}")
            message = "Pick Tracker — Results Update\n\n" + render_grouped_ticket_message(db, ticket)
            message = message.replace("Near-Start Odds: Pending", "Near-Start Odds: Unavailable (not recorded)")
            message = message.replace("Final Odds: Pending", "Final Odds: Unavailable (not recorded)")
            sources = [item["source_url"] for item in payload["legs"] if item["ticket_id"] == ticket]
            message += "\n\nResult sources:\n" + "\n".join(sources)
            message += "\n\nTest-only tracking."
            if len(message) > 4096:
                raise ValueError("Result message exceeds Telegram limit")
            if args.dry_run:
                print(message)
                continue
            if not verified:
                verify_bot()
                verified = True
            try:
                edit_message(message, str(entry[0]))
            except HTTPError as exc:
                # Retrying a successful edit after artifact-save failure is safe.
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code != 400 or "message is not modified" not in body.lower():
                    raise RuntimeError(f"Telegram edit failed (HTTP {exc.code})") from None
            db.execute(
                "INSERT INTO manual_pick_notifications "
                "(ticket_id, leg_id, stage, channel, telegram_message_id, source_workflow_run_id, sent_at) "
                "VALUES (?, NULL, ?, 'telegram', ?, ?, ?)",
                (ticket, stage, str(entry[0]), os.environ.get("GITHUB_RUN_ID"),
                 datetime.now(timezone.utc).isoformat())
            )
            db.commit()
            print(f"Telegram results updated: {ticket}; message_id={entry[0]}")
    db.close()


if __name__ == "__main__":
    main()
