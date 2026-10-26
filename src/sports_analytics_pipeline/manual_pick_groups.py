"""Group manual tracker tickets into system picks vs research recommendations.

This is presentation/tracking metadata only. It does not change model eligibility,
authorize wagering, or call any paid sports-data provider.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from sports_analytics_pipeline.manual_pick_tracker import ticket_summary

PICK_GROUPS = ("system", "recommended")


def ensure_pick_group_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS manual_pick_ticket_groups (
            ticket_id TEXT PRIMARY KEY
                REFERENCES manual_pick_tickets(ticket_id) ON DELETE CASCADE,
            pick_group TEXT NOT NULL
                CHECK (pick_group IN ('system','recommended'))
        )
        """
    )
    connection.commit()


def set_pick_group(connection: sqlite3.Connection, ticket_id: str, pick_group: str) -> None:
    ensure_pick_group_schema(connection)
    normalized = str(pick_group).strip().casefold()
    if normalized not in PICK_GROUPS:
        raise ValueError("pick_group must be 'system' or 'recommended'")
    exists = connection.execute(
        "SELECT 1 FROM manual_pick_tickets WHERE ticket_id=?", (ticket_id,)
    ).fetchone()
    if exists is None:
        raise ValueError("Unknown ticket")
    connection.execute(
        """
        INSERT INTO manual_pick_ticket_groups(ticket_id, pick_group)
        VALUES (?, ?)
        ON CONFLICT(ticket_id) DO UPDATE SET pick_group=excluded.pick_group
        """,
        (ticket_id, normalized),
    )
    connection.commit()


def get_pick_group(connection: sqlite3.Connection, ticket_id: str) -> str | None:
    ensure_pick_group_schema(connection)
    row = connection.execute(
        "SELECT pick_group FROM manual_pick_ticket_groups WHERE ticket_id=?", (ticket_id,)
    ).fetchone()
    return str(row[0]) if row else None


def _format_odds(value: Any) -> str:
    if value is None:
        return "Pending"
    number = float(value)
    return f"+{number:g}" if number > 0 else f"{number:g}"


def _latest_snapshot(connection: sqlite3.Connection, leg_id: str, snapshot_type: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT * FROM manual_pick_price_snapshots
        WHERE leg_id=? AND snapshot_type=?
        ORDER BY datetime(captured_at) DESC, price_snapshot_id DESC
        LIMIT 1
        """,
        (leg_id, snapshot_type),
    ).fetchone()
    return dict(row) if row else None


def render_grouped_ticket_message(connection: sqlite3.Connection, ticket_id: str) -> str:
    """Render one ticket as one uniform Telegram group message.

    System picks and recommended picks remain separate forever because the category
    is stored against the ticket, not inferred from results.
    """
    payload = ticket_summary(connection, ticket_id)
    pick_group = get_pick_group(connection, ticket_id)
    if pick_group is None:
        raise ValueError("Ticket has no pick_group; expected system or recommended")

    settled = str(payload["status"]).casefold() == "settled"
    if pick_group == "system":
        heading = "OUR SYSTEM PICKS"
        icon = "🎯"
    else:
        heading = "OUR RECOMMENDED PICKS"
        icon = "⭐"
    if settled:
        icon = "🏁"
        title = f"{icon} FINAL — {heading}"
    else:
        title = f"{icon} — {heading}"

    lines = [title, f"Status: {str(payload['status']).upper()}", ""]
    wins = losses = pushes = voids = pending = 0

    for leg in payload["legs"]:
        leg_id = str(leg["leg_id"])
        recheck = _latest_snapshot(connection, leg_id, "recheck")
        close = _latest_snapshot(connection, leg_id, "close")
        result = str(leg["result_status"]).casefold()
        if result == "win":
            result_display = "✅ WIN"
            wins += 1
        elif result == "loss":
            result_display = "❌ LOSS"
            losses += 1
        elif result == "push":
            result_display = "➖ PUSH"
            pushes += 1
        elif result in {"void", "cancelled"}:
            result_display = "🚫 VOID"
            voids += 1
        elif result == "unsettled":
            result_display = "⚠️ UNSETTLED"
            pending += 1
        else:
            result_display = "PENDING"
            pending += 1

        lines.extend(
            [
                f"Pick {leg['leg_order']} — {leg['selection']}",
                f"Event: {leg['event_label']}",
                f"Market: {leg['market_key']}",
                f"Entry Odds: {_format_odds(leg['entry_american_odds'])}",
                f"Near-Start Odds: {_format_odds(recheck['american_odds'] if recheck else None)}",
                f"Final Odds: {_format_odds(close['american_odds'] if close else None)}",
                f"Result: {result_display}",
            ]
        )
        if leg.get("final_score_or_result"):
            lines.append(f"Final: {leg['final_score_or_result']}")
        lines.append("")

    if settled:
        record = f"{wins}-{losses}"
        if pushes:
            record += f"-{pushes} push"
        if voids:
            record += f" | {voids} void"
        lines.append(f"Group Record: {record}")
    else:
        lines.append(f"Pending Results: {pending}")

    lines.append("Paid Sports-Data API Credits Used: 0")
    return "\n".join(lines).strip()
