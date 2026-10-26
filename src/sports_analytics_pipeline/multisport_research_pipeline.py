"""Fail-closed research ingestion for NBA, WNBA, EPL, MMA, ATP, and WTA.

Some approved sources expose stable APIs while others publish dynamic pages, reports,
or licensed feeds.  This module gives every source the same strict contract without
pretending an HTML page was successfully verified.  A check becomes ``verified`` only
when an adapter supplies event-specific, timestamped evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Any, Iterable, Protocol

from .multisport_research import (
    live_readiness_report,
    record_research_check,
    research_policy,
    required_categories,
    research_sources,
)


UTC = timezone.utc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a time zone")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class ResearchObservation:
    sport: str
    event_id: str
    category: str
    source_key: str
    verification_status: str
    collected_at: datetime
    source_updated_at: datetime | None = None
    source_url: str | None = None
    official: bool = False
    notes: str | None = None
    payload: dict[str, Any] | None = None


class ResearchAdapter(Protocol):
    source_key: str

    def collect(self, *, event: sqlite3.Row, as_of: datetime) -> Iterable[ResearchObservation]:
        """Return event-specific observations or no observations; never invent success."""


class StructuredResearchAdapter:
    """Adapter for a trusted structured payload from an approved collector/feed.

    This is useful for official reports, licensed APIs, or a separate scraper whose
    output has already been validated.  It refuses payloads that do not explicitly
    name the event, category, source, status, and timestamps.
    """

    def __init__(self, source_key: str, records: Iterable[dict[str, Any]]):
        self.source_key = source_key
        self._records = tuple(records)

    def collect(self, *, event: sqlite3.Row, as_of: datetime) -> Iterable[ResearchObservation]:
        as_of_utc = _utc(as_of)
        for row in self._records:
            if str(row.get("event_id", "")) != str(event["event_id"]):
                continue
            if str(row.get("sport", "")) != str(event["sport"]):
                continue
            if str(row.get("source_key", "")) != self.source_key:
                raise ValueError("Structured research source_key mismatch")
            category = str(row.get("category", ""))
            status = str(row.get("verification_status", ""))
            if not category or not status:
                raise ValueError("Structured research row is missing category/status")
            collected = datetime.fromisoformat(str(row["collected_at"]).replace("Z", "+00:00"))
            collected = _utc(collected)
            source_updated = None
            if row.get("source_updated_at"):
                source_updated = _utc(
                    datetime.fromisoformat(str(row["source_updated_at"]).replace("Z", "+00:00"))
                )
            if collected > as_of_utc:
                raise ValueError("Research evidence cannot come from the future")
            if source_updated and source_updated > collected:
                raise ValueError("Research source update cannot follow collection time")
            yield ResearchObservation(
                sport=str(event["sport"]),
                event_id=str(event["event_id"]),
                category=category,
                source_key=self.source_key,
                verification_status=status,
                collected_at=collected,
                source_updated_at=source_updated,
                source_url=str(row.get("source_url")) if row.get("source_url") else None,
                official=bool(row.get("official", False)),
                notes=str(row.get("notes")) if row.get("notes") is not None else None,
                payload=dict(row.get("payload") or {}),
            )


def source_directory(sport: str) -> dict[str, object]:
    return {spec.source_key: spec for spec in research_sources(sport)}


def validate_observation(
    observation: ResearchObservation,
    *,
    event: sqlite3.Row,
    as_of: datetime,
    max_age_minutes: int | None = None,
) -> ResearchObservation:
    if observation.sport != str(event["sport"]) or observation.event_id != str(event["event_id"]):
        raise ValueError("Research observation does not match the event")
    if observation.category not in required_categories(observation.sport):
        raise ValueError("Research category is not approved for this sport")
    directory = source_directory(observation.sport)
    if observation.source_key not in directory:
        raise ValueError(f"Unapproved research source: {observation.source_key}")
    spec = directory[observation.source_key]
    if spec.category != observation.category:
        raise ValueError("Research source is being used for an unapproved category")
    if observation.verification_status not in {"verified", "conditional", "unavailable", "conflict"}:
        raise ValueError("Unsupported research verification status")
    collected = _utc(observation.collected_at)
    as_of_utc = _utc(as_of)
    if collected > as_of_utc:
        raise ValueError("Research evidence cannot come from the future")
    if observation.source_updated_at and _utc(observation.source_updated_at) > collected:
        raise ValueError("Source update cannot follow collection time")
    if observation.verification_status == "verified":
        allowed_age = (
            int(max_age_minutes)
            if max_age_minutes is not None
            else int(research_policy(observation.sport).freshness_minutes[observation.category])
        )
        effective = _utc(observation.source_updated_at) if observation.source_updated_at else collected
        if as_of_utc - effective > timedelta(minutes=allowed_age):
            raise ValueError("Verified research is too old for the configured freshness window")
        if not observation.payload:
            raise ValueError("Verified research requires event-specific evidence payload")
    return observation


def persist_observation(
    connection: sqlite3.Connection,
    observation: ResearchObservation,
    *,
    as_of: datetime,
    max_age_minutes: int | None = None,
) -> int:
    event = connection.execute(
        "SELECT * FROM multisport_events WHERE event_id=?",
        (observation.event_id,),
    ).fetchone()
    if event is None:
        raise ValueError("Unknown research event")
    observation = validate_observation(
        observation,
        event=event,
        as_of=as_of,
        max_age_minutes=max_age_minutes,
    )
    return record_research_check(
        connection,
        event_id=observation.event_id,
        sport=observation.sport,
        category=observation.category,
        source_key=observation.source_key,
        verification_status=observation.verification_status,
        official=observation.official,
        source_url=observation.source_url,
        source_updated_at=observation.source_updated_at,
        collected_at=observation.collected_at,
        notes=observation.notes,
        evidence=observation.payload,
    )


def run_research_adapters(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    as_of: datetime,
    adapters: Iterable[ResearchAdapter],
    max_age_minutes: int | None = None,
) -> dict[str, object]:
    """Run configured adapters and report missing categories without fabricating them."""
    event = connection.execute("SELECT * FROM multisport_events WHERE event_id=?", (event_id,)).fetchone()
    if event is None:
        raise ValueError("Unknown research event")
    stored = 0
    errors: list[str] = []
    for adapter in adapters:
        try:
            observations = adapter.collect(event=event, as_of=as_of)
            for observation in observations:
                persist_observation(
                    connection,
                    observation,
                    as_of=as_of,
                    max_age_minutes=max_age_minutes,
                )
                stored += 1
        except Exception as exc:
            errors.append(f"{getattr(adapter, 'source_key', '<unknown>')}: {exc}")

    report = live_readiness_report(
        connection,
        event_id=event_id,
        sport=str(event["sport"]),
        as_of=as_of,
        odds_stale_after_minutes=max_age_minutes,
    )
    missing = [
        category
        for category, status in report.category_statuses.items()
        if status == "missing"
    ]
    return {
        "stored": stored,
        "missing_categories": tuple(missing),
        "blockers": report.blockers,
        "errors": tuple(errors),
        "ready": report.ready,
    }


def mark_current_odds_research(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    as_of: datetime,
    max_age_minutes: int = 60,
) -> int:
    """Use persisted current odds as evidence for the mandatory current-odds category."""
    event = connection.execute("SELECT * FROM multisport_events WHERE event_id=?", (event_id,)).fetchone()
    if event is None:
        raise ValueError("Unknown event")
    row = connection.execute(
        """
        SELECT sportsbook_key, market_key, collected_at, source_updated_at, COUNT(*) AS outcome_count
        FROM multisport_odds_snapshots
        WHERE event_id=? AND sport=? AND market_status='open' AND collected_at<=?
        GROUP BY sportsbook_key, market_key, collected_at, source_updated_at
        ORDER BY collected_at DESC
        LIMIT 1
        """,
        (event_id, event["sport"], _utc(as_of).isoformat()),
    ).fetchone()
    if row is None:
        raise ValueError("No current open odds evidence is available")
    collected = _utc(datetime.fromisoformat(str(row["collected_at"])))
    if _utc(as_of) - collected > timedelta(minutes=max_age_minutes):
        raise ValueError("Current odds evidence is stale")
    source_updated = _utc(datetime.fromisoformat(str(row["source_updated_at"])))
    observation = ResearchObservation(
        sport=str(event["sport"]),
        event_id=event_id,
        category="current_odds",
        source_key="the_odds_api",
        verification_status="verified",
        collected_at=collected,
        source_updated_at=source_updated,
        source_url="https://the-odds-api.com/",
        official=False,
        notes="Fresh named-book market snapshot stored by the read-only Odds API collector.",
        payload={
            "sportsbook_key": row["sportsbook_key"],
            "market_key": row["market_key"],
            "outcome_count": int(row["outcome_count"]),
        },
    )
    return persist_observation(
        connection,
        observation,
        as_of=as_of,
        max_age_minutes=max_age_minutes,
    )
