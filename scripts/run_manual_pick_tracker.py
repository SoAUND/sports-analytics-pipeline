#!/usr/bin/env python3
"""CLI for the zero-credit manual/video pick tracker."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from sports_analytics_pipeline.manual_pick_tracker import (
    add_price_snapshot,
    create_ticket,
    due_rechecks,
    ensure_manual_pick_tracker_schema,
    render_ticket_message,
    settle_leg,
    ticket_summary,
)

UTC = timezone.utc


def _dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    ensure_manual_pick_tracker_schema(connection)
    return connection


def _load_json(path: str) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("Input must be a JSON object")
    return payload


def cmd_bootstrap(args: argparse.Namespace) -> None:
    connection = _connect(args.database)
    connection.close()
    print(f"Manual pick tracker ready: {args.database}")


def cmd_ingest(args: argparse.Namespace) -> None:
    payload = _load_json(args.file)
    legs = payload.get("legs")
    if not isinstance(legs, list) or not legs:
        raise SystemExit("Input requires a non-empty legs array")
    connection = _connect(args.database)
    ticket_id = create_ticket(
        connection,
        legs=legs,
        source_kind=str(payload.get("source_kind") or "chatgpt_video"),
        source_reference=payload.get("source_reference"),
        sportsbook_key=payload.get("sportsbook_key"),
        offered_american_odds=payload.get("offered_american_odds"),
        offered_decimal_odds=payload.get("offered_decimal_odds"),
        notes=str(payload.get("notes") or ""),
        created_at=_dt(payload.get("created_at")) if payload.get("created_at") else None,
        ticket_id=payload.get("ticket_id"),
    )
    connection.close()
    print(ticket_id)


def cmd_price(args: argparse.Namespace) -> None:
    connection = _connect(args.database)
    snapshot_id = add_price_snapshot(
        connection,
        leg_id=args.leg_id,
        snapshot_type=args.snapshot_type,
        american_odds=args.american_odds,
        captured_at=_dt(args.captured_at),
        source_key=args.source_key,
        sportsbook_key=args.sportsbook_key,
        source_url=args.source_url,
        provenance=args.provenance,
    )
    connection.close()
    print(snapshot_id)


def cmd_due(args: argparse.Namespace) -> None:
    connection = _connect(args.database)
    rows = due_rechecks(connection, as_of=_dt(args.as_of), window_minutes=args.window_minutes)
    connection.close()
    print(json.dumps(rows, indent=2, sort_keys=True))


def cmd_settle(args: argparse.Namespace) -> None:
    connection = _connect(args.database)
    settle_leg(
        connection,
        leg_id=args.leg_id,
        result_status=args.result,
        final_score_or_result=args.final,
        result_source_key=args.source_key,
        result_source_url=args.source_url,
        settled_at=_dt(args.settled_at),
    )
    connection.close()
    print(f"Settled {args.leg_id}: {args.result}")


def cmd_status(args: argparse.Namespace) -> None:
    connection = _connect(args.database)
    payload = ticket_summary(connection, args.ticket_id)
    connection.close()
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_render(args: argparse.Namespace) -> None:
    connection = _connect(args.database)
    message = render_ticket_message(connection, args.ticket_id)
    connection.close()
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(message + "\n", encoding="utf-8")
    else:
        print(message)


def main() -> None:
    parser = argparse.ArgumentParser(description="Zero-credit manual/video pick tracker")
    parser.add_argument("--database", default="var/manual_pick_tracker.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap = sub.add_parser("bootstrap")
    bootstrap.set_defaults(func=cmd_bootstrap)

    ingest = sub.add_parser("ingest")
    ingest.add_argument("--file", required=True)
    ingest.set_defaults(func=cmd_ingest)

    price = sub.add_parser("price")
    price.add_argument("--leg-id", required=True)
    price.add_argument("--snapshot-type", choices=("entry", "recheck", "close", "manual", "public"), required=True)
    price.add_argument("--american-odds", type=float, required=True)
    price.add_argument("--captured-at")
    price.add_argument("--source-key", required=True)
    price.add_argument("--sportsbook-key")
    price.add_argument("--source-url")
    price.add_argument("--provenance", default="manual_or_public_zero_credit")
    price.set_defaults(func=cmd_price)

    due = sub.add_parser("due")
    due.add_argument("--as-of")
    due.add_argument("--window-minutes", type=int, default=30)
    due.set_defaults(func=cmd_due)

    settle = sub.add_parser("settle")
    settle.add_argument("--leg-id", required=True)
    settle.add_argument("--result", choices=("win", "loss", "push", "void", "cancelled", "unsettled"), required=True)
    settle.add_argument("--final", required=True)
    settle.add_argument("--source-key", required=True)
    settle.add_argument("--source-url")
    settle.add_argument("--settled-at")
    settle.set_defaults(func=cmd_settle)

    status = sub.add_parser("status")
    status.add_argument("--ticket-id", required=True)
    status.set_defaults(func=cmd_status)

    render = sub.add_parser("render")
    render.add_argument("--ticket-id", required=True)
    render.add_argument("--out")
    render.set_defaults(func=cmd_render)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
