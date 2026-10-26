"""Persistence helpers for our additive multi-sport Phase 2 tables."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import sqlite3

from .collectors.multisport_odds import (
    MultiSportCollectionBatch,
    MultiSportEvent,
    MultiSportParticipant,
    MultiSportQuote,
)
from .sports_registry import SPORT_MARKETS, validation_bucket


UTC = timezone.utc


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a time zone")
    return value.astimezone(UTC).isoformat()


def internal_participant_id(participant: MultiSportParticipant) -> str:
    return f"{participant.sport}:{participant.source_participant_id}"


def internal_event_id(event: MultiSportEvent) -> str:
    return f"{event.sport}:{event.provider_sport_key}:{event.source_event_id}"


def _canonical_payload(payload: object) -> tuple[str, str]:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return encoded, sha256(encoded.encode("utf-8")).hexdigest()


def store_raw_ingest(
    connection: sqlite3.Connection,
    batch: MultiSportCollectionBatch,
    *,
    endpoint_name: str,
) -> int:
    payload_json, digest = _canonical_payload(batch.raw_payload)
    connection.execute(
        """
        INSERT OR IGNORE INTO multisport_raw_ingests (
            source_key, sport, provider_sport_key, endpoint_name,
            collected_at, payload_json, content_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch.provider_key,
            batch.sport,
            batch.provider_sport_key,
            endpoint_name,
            _iso(batch.collected_at),
            payload_json,
            digest,
        ),
    )
    row = connection.execute(
        """
        SELECT ingest_id FROM multisport_raw_ingests
        WHERE source_key=? AND provider_sport_key=? AND endpoint_name=? AND content_sha256=?
        """,
        (batch.provider_key, batch.provider_sport_key, endpoint_name, digest),
    ).fetchone()
    if row is None:
        raise RuntimeError("Failed to persist multi-sport raw ingest")
    return int(row[0])


def store_participants(
    connection: sqlite3.Connection,
    batch: MultiSportCollectionBatch,
) -> int:
    if not batch.participants:
        return 0
    seen_at = _iso(batch.collected_at)
    count = 0
    for participant in batch.participants:
        participant_id = internal_participant_id(participant)
        connection.execute(
            """
            INSERT INTO multisport_participants (
                participant_id, sport, provider_sport_key, source_participant_id,
                full_name, participant_type, first_seen_at, last_seen_at, active_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'provider_listed')
            ON CONFLICT(participant_id) DO UPDATE SET
                full_name=excluded.full_name,
                participant_type=excluded.participant_type,
                last_seen_at=excluded.last_seen_at,
                active_status='provider_listed'
            """,
            (
                participant_id,
                participant.sport,
                participant.provider_sport_key,
                participant.source_participant_id,
                participant.full_name,
                participant.participant_type,
                seen_at,
                seen_at,
            ),
        )
        count += 1
    return count


def _participant_id_from_source(
    connection: sqlite3.Connection,
    *,
    sport: str,
    source_participant_id: str,
) -> str:
    candidate = f"{sport}:{source_participant_id}"
    row = connection.execute(
        "SELECT participant_id FROM multisport_participants WHERE participant_id=?",
        (candidate,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Participant {candidate!r} has not been persisted")
    return str(row[0])


def store_events(
    connection: sqlite3.Connection,
    batch: MultiSportCollectionBatch,
) -> int:
    if not batch.events:
        return 0
    seen_at = _iso(batch.collected_at)
    count = 0
    for event in batch.events:
        event_id = internal_event_id(event)
        home_id = _participant_id_from_source(
            connection,
            sport=event.sport,
            source_participant_id=event.home_participant_id,
        )
        away_id = _participant_id_from_source(
            connection,
            sport=event.sport,
            source_participant_id=event.away_participant_id,
        )
        connection.execute(
            """
            INSERT INTO multisport_events (
                event_id, sport, provider_sport_key, source_event_id, commence_time,
                home_participant_id, away_participant_id,
                venue_id, venue_name, source_venue_id, status,
                first_seen_at, last_seen_at, test_only
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, 1)
            ON CONFLICT(event_id) DO UPDATE SET
                commence_time=excluded.commence_time,
                home_participant_id=excluded.home_participant_id,
                away_participant_id=excluded.away_participant_id,
                venue_id=excluded.venue_id,
                venue_name=excluded.venue_name,
                source_venue_id=excluded.source_venue_id,
                last_seen_at=excluded.last_seen_at,
                test_only=1
            """,
            (
                event_id,
                event.sport,
                event.provider_sport_key,
                event.source_event_id,
                _iso(event.commence_time),
                home_id,
                away_id,
                event.venue_id,
                event.venue_name,
                event.source_venue_id,
                seen_at,
                seen_at,
            ),
        )
        count += 1
    return count


def _event_id_from_quote(connection: sqlite3.Connection, quote: MultiSportQuote) -> str:
    event_id = f"{quote.sport}:{quote.provider_sport_key}:{quote.source_event_id}"
    row = connection.execute(
        "SELECT event_id FROM multisport_events WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Event {event_id!r} has not been persisted")
    return str(row[0])


def store_odds(
    connection: sqlite3.Connection,
    batch: MultiSportCollectionBatch,
) -> int:
    if not batch.quotes:
        return 0
    ingest_id = store_raw_ingest(connection, batch, endpoint_name="odds")
    count = 0
    for quote in batch.quotes:
        event_id = _event_id_from_quote(connection, quote)
        outcome_id = None
        if quote.outcome_participant_id is not None:
            outcome_id = _participant_id_from_source(
                connection,
                sport=quote.sport,
                source_participant_id=quote.outcome_participant_id,
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO multisport_odds_snapshots (
                event_id, ingest_id, sport, provider_sport_key,
                sportsbook_key, sportsbook_title, market_key, provider_market_key,
                period, settlement_rule, outcome_participant_id, outcome_name,
                outcome_role, points, decimal_odds, source_updated_at,
                collected_at, market_status, test_only
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                event_id,
                ingest_id,
                quote.sport,
                quote.provider_sport_key,
                quote.bookmaker_key,
                quote.bookmaker_title,
                quote.market_key,
                quote.provider_market_key,
                quote.period,
                quote.settlement_rule,
                outcome_id,
                quote.outcome_name,
                quote.outcome_role,
                float(quote.points) if quote.points is not None else None,
                float(quote.decimal_odds),
                _iso(quote.source_updated_at),
                _iso(quote.collected_at),
                quote.status,
            ),
        )
        count += 1
    return count


def persist_collection_cycle(
    connection: sqlite3.Connection,
    *,
    participants: MultiSportCollectionBatch,
    events: MultiSportCollectionBatch,
    odds: MultiSportCollectionBatch,
) -> dict[str, int]:
    """Persist one provider-key cycle in dependency order and commit atomically."""
    keys = {
        (participants.sport, participants.provider_sport_key),
        (events.sport, events.provider_sport_key),
        (odds.sport, odds.provider_sport_key),
    }
    if len(keys) != 1:
        raise ValueError("Collection batches do not describe the same sport/provider key")
    try:
        store_raw_ingest(connection, participants, endpoint_name="participants")
        participant_count = store_participants(connection, participants)
        store_raw_ingest(connection, events, endpoint_name="events")
        event_count = store_events(connection, events)
        odds_count = store_odds(connection, odds)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "participants": participant_count,
        "events": event_count,
        "quotes": odds_count,
    }


def seed_validation_state(
    connection: sqlite3.Connection,
    *,
    as_of: datetime | None = None,
) -> int:
    now = _iso(as_of or datetime.now(UTC))
    count = 0
    for sport, spec in SPORT_MARKETS.items():
        bucket = validation_bucket(sport)
        connection.execute(
            """
            INSERT INTO multisport_validation_state (
                validation_bucket, sport, market_key, period,
                target_decisions, completed_decisions, approved, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 0, 'test_only', ?)
            ON CONFLICT(validation_bucket) DO UPDATE SET
                target_decisions=excluded.target_decisions,
                approved=0,
                status='test_only',
                updated_at=excluded.updated_at
            """,
            (
                bucket,
                sport,
                spec.internal_market_key,
                spec.period,
                spec.validation_target,
                now,
            ),
        )
        count += 1
    connection.commit()
    return count
