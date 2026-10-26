"""Zero-credit lifecycle tracking for manually supplied sportsbook picks.

This module is additive and deliberately separate from the protected NFL/MLB and
multi-sport model decision tables. It records picks extracted from a screenshot,
screen recording, or other user-supplied source without authorizing a real-money action or
calling any paid odds provider.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Iterable
from uuid import uuid4

UTC = timezone.utc

TICKET_STATUSES = (
    "pre_pick",
    "recheck",
    "locked",
    "hold",
    "cancelled",
    "final",
    "settled",
)
LEG_RESULTS = ("pending", "win", "loss", "push", "void", "cancelled", "unsettled")
SNAPSHOT_TYPES = ("entry", "recheck", "close", "manual", "public")


def _iso(value: datetime | None = None) -> str:
    value = value or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a time zone")
    return value.astimezone(UTC).isoformat()


def american_to_decimal(american_odds: float | int) -> float:
    odds = float(american_odds)
    if odds == 0:
        raise ValueError("American odds cannot be zero")
    return 1.0 + (odds / 100.0 if odds > 0 else 100.0 / abs(odds))


def ensure_manual_pick_tracker_schema(connection: sqlite3.Connection) -> None:
    """Create an isolated manual/video pick ledger.

    The schema keeps the original entry price immutable by storing all later prices
    as append-only snapshots. paid_credit_used is constrained to zero so this lane
    cannot be mistaken for a paid-provider collection path.
    """
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS manual_pick_tickets (
            ticket_id TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL,
            source_reference TEXT,
            sportsbook_key TEXT,
            ticket_type TEXT NOT NULL CHECK (ticket_type IN ('single','parlay')),
            offered_american_odds REAL,
            offered_decimal_odds REAL,
            status TEXT NOT NULL CHECK (
                status IN ('pre_pick','recheck','locked','hold','cancelled','final','settled')
            ),
            test_only INTEGER NOT NULL DEFAULT 1 CHECK (test_only = 1),
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS manual_pick_legs (
            leg_id TEXT PRIMARY KEY,
            ticket_id TEXT NOT NULL REFERENCES manual_pick_tickets(ticket_id) ON DELETE CASCADE,
            leg_order INTEGER NOT NULL CHECK (leg_order >= 1),
            sport TEXT NOT NULL,
            event_label TEXT NOT NULL,
            native_event_id TEXT,
            market_key TEXT NOT NULL,
            selection TEXT NOT NULL,
            points REAL,
            sportsbook_key TEXT,
            commence_time TEXT,
            entry_american_odds REAL NOT NULL,
            entry_decimal_odds REAL NOT NULL CHECK (entry_decimal_odds > 1),
            entry_captured_at TEXT NOT NULL,
            entry_source_key TEXT NOT NULL,
            entry_source_url TEXT,
            model_status TEXT,
            eligibility_status TEXT NOT NULL DEFAULT 'test_only',
            tracker_status TEXT NOT NULL DEFAULT 'pre_pick' CHECK (
                tracker_status IN ('pre_pick','recheck','locked','hold','cancelled','final','settled')
            ),
            result_status TEXT NOT NULL DEFAULT 'pending' CHECK (
                result_status IN ('pending','win','loss','push','void','cancelled','unsettled')
            ),
            final_score_or_result TEXT,
            result_source_key TEXT,
            result_source_url TEXT,
            settled_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(ticket_id, leg_order)
        );

        CREATE INDEX IF NOT EXISTS idx_manual_pick_legs_due
        ON manual_pick_legs(tracker_status, commence_time);

        CREATE TABLE IF NOT EXISTS manual_pick_price_snapshots (
            price_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            leg_id TEXT NOT NULL REFERENCES manual_pick_legs(leg_id) ON DELETE CASCADE,
            snapshot_type TEXT NOT NULL CHECK (
                snapshot_type IN ('entry','recheck','close','manual','public')
            ),
            sportsbook_key TEXT,
            american_odds REAL NOT NULL,
            decimal_odds REAL NOT NULL CHECK (decimal_odds > 1),
            captured_at TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_url TEXT,
            provenance TEXT NOT NULL,
            paid_credit_used INTEGER NOT NULL DEFAULT 0 CHECK (paid_credit_used = 0),
            UNIQUE(leg_id, snapshot_type, captured_at, source_key)
        );

        CREATE INDEX IF NOT EXISTS idx_manual_pick_prices_leg_time
        ON manual_pick_price_snapshots(leg_id, captured_at);

        CREATE TABLE IF NOT EXISTS manual_pick_notifications (
            notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT NOT NULL REFERENCES manual_pick_tickets(ticket_id) ON DELETE CASCADE,
            leg_id TEXT REFERENCES manual_pick_legs(leg_id) ON DELETE CASCADE,
            stage TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'telegram',
            telegram_message_id TEXT,
            source_workflow_run_id TEXT,
            sent_at TEXT NOT NULL,
            UNIQUE(ticket_id, leg_id, stage, channel)
        );
        """
    )
    connection.commit()


def create_ticket(
    connection: sqlite3.Connection,
    *,
    legs: Iterable[dict[str, Any]],
    source_kind: str,
    source_reference: str | None = None,
    sportsbook_key: str | None = None,
    offered_american_odds: float | None = None,
    offered_decimal_odds: float | None = None,
    notes: str = "",
    created_at: datetime | None = None,
    ticket_id: str | None = None,
) -> str:
    ensure_manual_pick_tracker_schema(connection)
    rows = list(legs)
    if not rows:
        raise ValueError("At least one leg is required")
    stamp = _iso(created_at)
    ticket_id = ticket_id or f"manual-{uuid4().hex}"
    ticket_type = "single" if len(rows) == 1 else "parlay"
    if offered_decimal_odds is None and offered_american_odds is not None:
        offered_decimal_odds = american_to_decimal(offered_american_odds)

    connection.execute(
        """
        INSERT INTO manual_pick_tickets (
            ticket_id, source_kind, source_reference, sportsbook_key, ticket_type,
            offered_american_odds, offered_decimal_odds, status, test_only,
            notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pre_pick', 1, ?, ?, ?)
        """,
        (
            ticket_id,
            source_kind.strip(),
            source_reference,
            sportsbook_key,
            ticket_type,
            offered_american_odds,
            offered_decimal_odds,
            notes,
            stamp,
            stamp,
        ),
    )

    for index, leg in enumerate(rows, start=1):
        american = float(leg["entry_american_odds"])
        decimal = float(leg.get("entry_decimal_odds") or american_to_decimal(american))
        captured = leg.get("entry_captured_at")
        if isinstance(captured, datetime):
            captured_at = _iso(captured)
        elif captured:
            captured_at = str(captured)
        else:
            captured_at = stamp
        leg_id = str(leg.get("leg_id") or f"{ticket_id}-leg-{index}")
        entry_source_key = str(leg.get("entry_source_key") or source_kind)
        connection.execute(
            """
            INSERT INTO manual_pick_legs (
                leg_id, ticket_id, leg_order, sport, event_label, native_event_id,
                market_key, selection, points, sportsbook_key, commence_time,
                entry_american_odds, entry_decimal_odds, entry_captured_at,
                entry_source_key, entry_source_url, model_status, eligibility_status,
                tracker_status, result_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'pre_pick', 'pending', ?, ?)
            """,
            (
                leg_id,
                ticket_id,
                index,
                str(leg["sport"]).casefold(),
                str(leg["event_label"]),
                leg.get("native_event_id"),
                str(leg["market_key"]),
                str(leg["selection"]),
                leg.get("points"),
                leg.get("sportsbook_key") or sportsbook_key,
                leg.get("commence_time"),
                american,
                decimal,
                captured_at,
                entry_source_key,
                leg.get("entry_source_url"),
                leg.get("model_status"),
                str(leg.get("eligibility_status") or "test_only"),
                stamp,
                stamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO manual_pick_price_snapshots (
                leg_id, snapshot_type, sportsbook_key, american_odds, decimal_odds,
                captured_at, source_key, source_url, provenance, paid_credit_used
            ) VALUES (?, 'entry', ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                leg_id,
                leg.get("sportsbook_key") or sportsbook_key,
                american,
                decimal,
                captured_at,
                entry_source_key,
                leg.get("entry_source_url"),
                str(leg.get("provenance") or "user_supplied_video_or_screenshot"),
            ),
        )
    connection.commit()
    return ticket_id


def add_price_snapshot(
    connection: sqlite3.Connection,
    *,
    leg_id: str,
    snapshot_type: str,
    american_odds: float,
    captured_at: datetime,
    source_key: str,
    sportsbook_key: str | None = None,
    source_url: str | None = None,
    provenance: str = "manual_or_public_zero_credit",
) -> int:
    ensure_manual_pick_tracker_schema(connection)
    if snapshot_type not in SNAPSHOT_TYPES:
        raise ValueError(f"Unsupported snapshot_type: {snapshot_type}")
    exists = connection.execute(
        "SELECT 1 FROM manual_pick_legs WHERE leg_id=?", (leg_id,)
    ).fetchone()
    if exists is None:
        raise ValueError("Unknown leg")
    decimal = american_to_decimal(american_odds)
    cursor = connection.execute(
        """
        INSERT INTO manual_pick_price_snapshots (
            leg_id, snapshot_type, sportsbook_key, american_odds, decimal_odds,
            captured_at, source_key, source_url, provenance, paid_credit_used
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            leg_id,
            snapshot_type,
            sportsbook_key,
            float(american_odds),
            decimal,
            _iso(captured_at),
            source_key,
            source_url,
            provenance,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def set_leg_status(
    connection: sqlite3.Connection,
    *,
    leg_id: str,
    tracker_status: str,
    as_of: datetime | None = None,
) -> None:
    if tracker_status not in TICKET_STATUSES:
        raise ValueError("Unsupported tracker status")
    stamp = _iso(as_of)
    cursor = connection.execute(
        "UPDATE manual_pick_legs SET tracker_status=?, updated_at=? WHERE leg_id=?",
        (tracker_status, stamp, leg_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("Unknown leg")
    connection.commit()


def settle_leg(
    connection: sqlite3.Connection,
    *,
    leg_id: str,
    result_status: str,
    final_score_or_result: str,
    result_source_key: str,
    result_source_url: str | None = None,
    settled_at: datetime | None = None,
) -> None:
    ensure_manual_pick_tracker_schema(connection)
    if result_status not in LEG_RESULTS or result_status == "pending":
        raise ValueError("Unsupported settled result")
    stamp = _iso(settled_at)
    cursor = connection.execute(
        """
        UPDATE manual_pick_legs
        SET result_status=?, final_score_or_result=?, result_source_key=?,
            result_source_url=?, tracker_status='settled', settled_at=?, updated_at=?
        WHERE leg_id=?
        """,
        (
            result_status,
            final_score_or_result,
            result_source_key,
            result_source_url,
            stamp,
            stamp,
            leg_id,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("Unknown leg")
    _refresh_ticket_status(connection, leg_id=leg_id, as_of=stamp)
    connection.commit()


def _refresh_ticket_status(connection: sqlite3.Connection, *, leg_id: str, as_of: str) -> None:
    row = connection.execute(
        "SELECT ticket_id FROM manual_pick_legs WHERE leg_id=?", (leg_id,)
    ).fetchone()
    if row is None:
        return
    ticket_id = str(row[0])
    statuses = [
        str(item[0])
        for item in connection.execute(
            "SELECT result_status FROM manual_pick_legs WHERE ticket_id=? ORDER BY leg_order",
            (ticket_id,),
        ).fetchall()
    ]
    if statuses and all(status != "pending" for status in statuses):
        connection.execute(
            "UPDATE manual_pick_tickets SET status='settled', updated_at=? WHERE ticket_id=?",
            (as_of, ticket_id),
        )


def due_rechecks(
    connection: sqlite3.Connection,
    *,
    as_of: datetime,
    window_minutes: int = 30,
) -> tuple[dict[str, Any], ...]:
    ensure_manual_pick_tracker_schema(connection)
    if window_minutes < 0:
        raise ValueError("window_minutes must be non-negative")
    now = _iso(as_of)
    rows = connection.execute(
        """
        SELECT l.*, t.status AS ticket_status
        FROM manual_pick_legs l
        JOIN manual_pick_tickets t ON t.ticket_id=l.ticket_id
        WHERE l.result_status='pending'
          AND l.tracker_status IN ('pre_pick','recheck','hold')
          AND l.commence_time IS NOT NULL
          AND datetime(l.commence_time) > datetime(?)
          AND datetime(l.commence_time) <= datetime(?, ?)
        ORDER BY datetime(l.commence_time), l.ticket_id, l.leg_order
        """,
        (now, now, f"+{int(window_minutes)} minutes"),
    ).fetchall()
    return tuple(dict(row) for row in rows)


def latest_price(connection: sqlite3.Connection, leg_id: str) -> dict[str, Any] | None:
    ensure_manual_pick_tracker_schema(connection)
    row = connection.execute(
        """
        SELECT * FROM manual_pick_price_snapshots
        WHERE leg_id=?
        ORDER BY datetime(captured_at) DESC, price_snapshot_id DESC
        LIMIT 1
        """,
        (leg_id,),
    ).fetchone()
    return dict(row) if row else None


def ticket_summary(connection: sqlite3.Connection, ticket_id: str) -> dict[str, Any]:
    ensure_manual_pick_tracker_schema(connection)
    ticket = connection.execute(
        "SELECT * FROM manual_pick_tickets WHERE ticket_id=?", (ticket_id,)
    ).fetchone()
    if ticket is None:
        raise ValueError("Unknown ticket")
    legs = []
    for row in connection.execute(
        "SELECT * FROM manual_pick_legs WHERE ticket_id=? ORDER BY leg_order", (ticket_id,)
    ).fetchall():
        leg = dict(row)
        leg["latest_price"] = latest_price(connection, str(row["leg_id"]))
        legs.append(leg)
    payload = dict(ticket)
    payload["legs"] = legs
    return payload


def render_ticket_message(connection: sqlite3.Connection, ticket_id: str) -> str:
    payload = ticket_summary(connection, ticket_id)
    title = "Manual Pick Tracker"
    lines = [
        f"🎟️ {title}",
        f"Ticket: {payload['ticket_type'].upper()} | Status: {str(payload['status']).upper()}",
        "Test-only tracker. No real-money action is taken.",
        "",
    ]
    for leg in payload["legs"]:
        entry = float(leg["entry_american_odds"])
        entry_text = f"+{entry:g}" if entry > 0 else f"{entry:g}"
        latest = leg.get("latest_price") or {}
        current = latest.get("american_odds")
        current_text = "Unavailable"
        if current is not None:
            current = float(current)
            current_text = f"+{current:g}" if current > 0 else f"{current:g}"
        lines.extend(
            [
                f"{leg['leg_order']}. {leg['selection']} — {leg['event_label']}",
                f"Sport/Market: {str(leg['sport']).upper()} / {leg['market_key']}",
                f"Pre-odds: {entry_text}",
                f"Latest odds: {current_text}",
                f"Result: {str(leg['result_status']).upper()}",
            ]
        )
        if leg.get("final_score_or_result"):
            lines.append(f"Final: {leg['final_score_or_result']}")
        lines.append("")
    lines.append("Zero paid sports-data credits used by this tracker lane.")
    return "\n".join(lines).strip()
