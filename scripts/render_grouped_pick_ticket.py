#!/usr/bin/env python3
"""Render a manual pick ticket using its permanent system/recommended group."""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3

from sports_analytics_pipeline.manual_pick_groups import render_grouped_ticket_message
from sports_analytics_pipeline.manual_pick_tracker import ensure_manual_pick_tracker_schema


def main() -> None:
    parser = argparse.ArgumentParser(description="Render grouped manual pick ticket")
    parser.add_argument("--database", required=True)
    parser.add_argument("--ticket-id", required=True)
    parser.add_argument("--out")
    args = parser.parse_args()

    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    ensure_manual_pick_tracker_schema(connection)
    message = render_grouped_ticket_message(connection, args.ticket_id)
    connection.close()

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(message + "\n", encoding="utf-8")
    else:
        print(message)


if __name__ == "__main__":
    main()
