#!/usr/bin/env python3
"""Mark one Telegram result as delivered after the send command succeeds."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import sqlite3

from sports_analytics_pipeline.multisport_results import mark_result_notified


def main() -> None:
    parser = argparse.ArgumentParser(description="Mark a settled test result as notified")
    parser.add_argument("--database", required=True)
    parser.add_argument("--paper-decision-id", required=True)
    parser.add_argument("--source-workflow-run-id", required=True)
    parser.add_argument("--channel", default="telegram")
    args = parser.parse_args()

    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    try:
        inserted = mark_result_notified(
            connection,
            paper_decision_id=args.paper_decision_id,
            channel=args.channel,
            notified_at=datetime.now(timezone.utc),
            source_workflow_run_id=args.source_workflow_run_id,
        )
    finally:
        connection.close()
    print("marked" if inserted else "already_marked")


if __name__ == "__main__":
    main()
