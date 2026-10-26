"""Bounded automatic intake for Our event-specific live research feeds.

The league sites in Our approved source directory do not all expose stable public
JSON APIs.  This collector therefore accepts normalized HTTPS feeds produced by
approved source adapters.  It fetches each configured source once per cycle, locks
the source/category/official identity to Our directory, and then passes every record
through the same fail-closed research validation used by manual imports.

It never treats page reachability as research, never takes a real-money action, and never logs
feed credentials or configured feed URLs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
import sqlite3
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .http import HttpTransport
from ..multisport_research import (
    all_research_sports,
    live_readiness_report,
    research_sources,
)
from ..multisport_research_pipeline import (
    StructuredResearchAdapter,
    persist_observation,
)
from ..venue_identity import multisport_venue_directory


UTC = timezone.utc
MAX_FEED_BYTES = 5 * 1024 * 1024
CRITICAL_CATEGORY_ORDER = {
    "official_status": 0,
    "injury_availability": 1,
    "lineup_or_participants": 2,
    "weigh_in_status": 3,
    "weather_environment": 4,
}

LEGACY_FEED_URL_KEYS = {
    "premier_league_statistics": "fbref",
    "premier_league_squad_changes": "transfermarkt_changes",
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a time zone")
    return value.astimezone(UTC)


def _validated_https_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme.casefold() != "https" or not parts.netloc or parts.username or parts.password:
        raise ValueError("Research feed URLs must be credential-free HTTPS URLs")
    return url


def _feed_rows(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("observations"), list):
        rows = payload["observations"]
    else:
        raise ValueError("Research feed must be a list or an object with observations")
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("Every research feed observation must be an object")
    return [dict(row) for row in rows]


def _feed_status(payload: object) -> tuple[str, str | None]:
    """Read the adapter envelope without trusting arbitrary upstream text."""
    if not isinstance(payload, dict):
        return "collected", None
    status = str(payload.get("status") or "collected").casefold().strip()
    if status not in {"collected", "unavailable"}:
        raise ValueError("Research feed returned an unsupported adapter status")
    reason = str(payload.get("reason") or "").casefold().strip() or None
    if reason and not re.fullmatch(r"[a-z0-9_]{1,100}", reason):
        reason = "adapter_reported_unavailable"
    return status, reason


def _feed_unavailable_events(
    payload: object,
    *,
    expected_event_ids: Iterable[str],
) -> tuple[dict[str, str], ...]:
    """Preserve bounded adapter diagnostics without trusting free-form text."""
    if not isinstance(payload, dict) or payload.get("unavailable") is None:
        return ()
    rows = payload["unavailable"]
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Research feed unavailable diagnostics must be a list of objects")
    expected = set(expected_event_ids)
    seen: set[str] = set()
    diagnostics: list[dict[str, str]] = []
    for row in rows:
        event_id = str(row.get("event_id") or "").strip()
        if event_id not in expected or event_id in seen:
            raise ValueError("Research feed unavailable diagnostic has an invalid event ID")
        seen.add(event_id)
        raw_reason = str(row.get("reason") or "").strip()
        match = re.fullmatch(
            r"([a-z0-9_]{1,100})(?::([A-Za-z0-9_.' -]{1,80}))?",
            raw_reason,
        )
        reason = match.group(0) if match else "adapter_reported_unavailable"
        diagnostics.append({"event_id": event_id, "reason": reason})
    return tuple(diagnostics)


def _source_specs(sports: Iterable[str]) -> dict[tuple[str, str], object]:
    return {
        (sport, spec.source_key): spec
        for sport in sports
        for spec in research_sources(sport)
        if spec.category != "current_odds"
    }


def _payload_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _ordered_source_keys(
    configured: Mapping[str, str],
    *,
    specs: Mapping[tuple[str, str], object],
    event_rows: Iterable[sqlite3.Row],
    now: datetime,
) -> tuple[str, ...]:
    """Keep live safety checks first and rotate slower research fairly each hour."""
    active_sports = {str(event["sport"]) for event in event_rows}
    critical: list[tuple[int, str]] = []
    rotating: list[str] = []
    for source_key in configured:
        categories = {
            str(spec.category)
            for (sport, key), spec in specs.items()
            if key == source_key and sport in active_sports
        }
        if not categories:
            continue
        priorities = [CRITICAL_CATEGORY_ORDER[value] for value in categories if value in CRITICAL_CATEGORY_ORDER]
        if priorities:
            critical.append((min(priorities), source_key))
        else:
            rotating.append(source_key)
    critical_keys = [source_key for _, source_key in sorted(critical)]
    rotating.sort()
    if rotating:
        offset = int(now.timestamp() // 3600) % len(rotating)
        rotating = rotating[offset:] + rotating[:offset]
    return tuple((*critical_keys, *rotating))


def _event_context(event: sqlite3.Row) -> dict[str, object]:
    """Build the bounded, non-secret event context sent to one adapter."""
    venue: dict[str, object] | None = None
    venue_name = str(event["venue_name"] or "").strip()
    venue_id = str(event["venue_id"] or "").strip()
    directory = multisport_venue_directory()
    if venue_name or venue_id:
        resolved = directory.resolve_optional(
            str(event["sport"]),
            source_name=venue_name or None,
        )
        venue = {
            "venue_id": venue_id or None,
            "venue_name": venue_name or None,
            "source_venue_id": str(event["source_venue_id"] or "").strip() or None,
            "city": resolved.venue.city if resolved.venue else None,
            "state_or_region": resolved.venue.state_or_region if resolved.venue else None,
            "country_code": resolved.venue.country_code if resolved.venue else None,
            "time_zone": resolved.venue.time_zone if resolved.venue else None,
            "reviewed": bool(resolved.venue),
        }
    elif str(event["sport"]) == "epl":
        reviewed_home = directory.resolve_reviewed_home("epl", str(event["home_name"]))
        if reviewed_home is not None:
            venue = {
                "venue_id": reviewed_home.venue_id,
                "venue_name": reviewed_home.official_name,
                "source_venue_id": None,
                "city": reviewed_home.city,
                "state_or_region": reviewed_home.state_or_region,
                "country_code": reviewed_home.country_code,
                "time_zone": reviewed_home.time_zone,
                "reviewed": True,
            }
    return {
        "event_id": str(event["event_id"]),
        "sport": str(event["sport"]),
        "provider_sport_key": str(event["provider_sport_key"]),
        "source_event_id": str(event["source_event_id"]),
        "commence_time": str(event["commence_time"]),
        "home": {
            "participant_id": str(event["home_participant_id"]),
            "name": str(event["home_name"]),
        },
        "away": {
            "participant_id": str(event["away_participant_id"]),
            "name": str(event["away_name"]),
        },
        "venue": venue,
        "test_only": True,
    }


def _already_stored(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    category: str,
    source_key: str,
    collected_at: str,
    payload: Mapping[str, Any],
) -> tuple[bool, bool]:
    row = connection.execute(
        """
        SELECT evidence_hash FROM multisport_research_checks
        WHERE event_id=? AND category=? AND source_key=? AND collected_at=?
        """,
        (event_id, category, source_key, collected_at),
    ).fetchone()
    if row is None:
        return False, False
    return True, str(row["evidence_hash"] or "") == _payload_hash(payload)


def collect_automatic_live_research(
    connection: sqlite3.Connection,
    *,
    sports: Iterable[str],
    as_of: datetime,
    feed_urls: Mapping[str, str],
    bearer_token: str | None,
    transport: HttpTransport,
    horizon_minutes: int = 360,
    max_requests: int = 30,
) -> dict[str, object]:
    """Fetch normalized live research once per source and persist valid observations."""
    now = _utc(as_of)
    selected_sports = tuple(dict.fromkeys(str(sport).casefold().strip() for sport in sports))
    supported = set(all_research_sports())
    if not selected_sports:
        raise ValueError("At least one research sport is required")
    invalid_sports = tuple(sport for sport in selected_sports if sport not in supported)
    if invalid_sports:
        raise ValueError(f"Unsupported research sports: {', '.join(invalid_sports)}")
    if horizon_minutes < 30 or horizon_minutes > 1440:
        raise ValueError("Research horizon must be between 30 and 1440 minutes")
    if max_requests < 1 or max_requests > 30:
        raise ValueError("Research request limit must be between 1 and 30")

    specs = _source_specs(selected_sports)
    relevant_keys = {source_key for _, source_key in specs}
    globally_approved_keys = {
        source_key
        for _, source_key in _source_specs(tuple(sorted(supported)))
    }
    configured = {str(key).casefold().strip(): str(url).strip() for key, url in feed_urls.items()}
    accepted_config_keys = globally_approved_keys | set(LEGACY_FEED_URL_KEYS.values())
    unknown = tuple(sorted(key for key in configured if key not in accepted_config_keys))
    if unknown:
        raise ValueError(f"Unapproved or irrelevant research feed keys: {', '.join(unknown)}")
    configured = {
        key: configured.get(key) or configured.get(LEGACY_FEED_URL_KEYS.get(key, ""), "")
        for key in relevant_keys
        if configured.get(key) or configured.get(LEGACY_FEED_URL_KEYS.get(key, ""))
    }
    if not configured:
        return {
            "status": "disabled",
            "requests_made": 0,
            "stored": 0,
            "duplicates": 0,
            "ignored": 0,
            "errors": (),
            "deferred_sources": (),
            "sources": {},
            "events": {},
        }

    event_rows = connection.execute(
        f"""
        SELECT e.*, home.full_name AS home_name, away.full_name AS away_name
        FROM multisport_events e
        JOIN multisport_participants home ON home.participant_id=e.home_participant_id
        JOIN multisport_participants away ON away.participant_id=e.away_participant_id
        WHERE e.status='scheduled' AND e.commence_time>? AND e.commence_time<=?
          AND e.sport IN ({','.join('?' for _ in selected_sports)})
        ORDER BY e.commence_time, e.event_id
        """,
        (
            now.isoformat(),
            (now + timedelta(minutes=horizon_minutes)).isoformat(),
            *selected_sports,
        ),
    ).fetchall()
    events = {str(row["event_id"]): row for row in event_rows}
    if not events:
        return {
            "status": "no_events_in_horizon",
            "requests_made": 0,
            "stored": 0,
            "duplicates": 0,
            "ignored": 0,
            "errors": (),
            "deferred_sources": (),
            "sources": {},
            "events": {},
        }

    requests_made = stored = duplicates = ignored = 0
    unavailable_sources = 0
    errors: list[str] = []
    deferred_sources: list[str] = []
    source_results: dict[str, dict[str, object]] = {}
    ordered_sources = _ordered_source_keys(
        configured,
        specs=specs,
        event_rows=event_rows,
        now=now,
    )
    for source_index, source_key in enumerate(ordered_sources):
        if requests_made >= max_requests:
            deferred_sources.extend(ordered_sources[source_index:])
            break
        relevant_specs = [
            spec for (sport, key), spec in specs.items()
            if key == source_key and any(str(event["sport"]) == sport for event in event_rows)
        ]
        if not relevant_specs:
            continue
        if any(bool(spec.official) for spec in relevant_specs) and not bearer_token:
            message = "authenticated_feed_required_for_official_source"
            errors.append(f"{source_key}:{message}")
            source_results[source_key] = {"status": "blocked", "reason": message, "stored": 0}
            continue

        url = _validated_https_url(configured[source_key])
        headers = {
            "Accept": "application/json",
            "X-Our-Research-Contract": "1",
            "X-Our-Research-Source": source_key,
        }
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        requests_made += 1
        source_stored = source_duplicates = source_ignored = 0
        try:
            source_sports = {
                sport
                for (sport, key) in specs
                if key == source_key and any(str(event["sport"]) == sport for event in event_rows)
            }
            categories = sorted({str(spec.category) for spec in relevant_specs})
            response = transport.post_json(
                url,
                headers=headers,
                payload={
                    "contract_version": 1,
                    "source_key": source_key,
                    "categories": categories,
                    "requested_at": now.isoformat(),
                    "events": [
                        _event_context(event)
                        for event in event_rows
                        if str(event["sport"]) in source_sports
                    ],
                    "test_only": True,
                },
            )
            if response.status != 200:
                raise ValueError(f"Research feed returned HTTP {response.status}")
            if len(response.body) > MAX_FEED_BYTES:
                raise ValueError("Research feed response is larger than 5 MiB")
            feed_payload = response.json()
            adapter_status, adapter_reason = _feed_status(feed_payload)
            rows = _feed_rows(feed_payload)
            requested_event_ids = {
                str(event["event_id"])
                for event in event_rows
                if str(event["sport"]) in source_sports
            }
            source_unavailable_events = _feed_unavailable_events(
                feed_payload,
                expected_event_ids=requested_event_ids,
            )
            if adapter_status == "unavailable":
                if rows:
                    raise ValueError(
                        "Unavailable research feed cannot include observations"
                    )
                unavailable_sources += 1
                source_results[source_key] = {
                    "status": "unavailable",
                    "reason": adapter_reason or "adapter_reported_unavailable",
                    "stored": 0,
                    "duplicates": 0,
                    "ignored": 0,
                    "unavailable_events": source_unavailable_events,
                }
                continue
            if source_unavailable_events:
                unavailable_sources += 1
            for raw in rows:
                event_id = str(raw.get("event_id", "")).strip()
                event = events.get(event_id)
                if event is None:
                    source_ignored += 1
                    continue
                sport = str(event["sport"])
                spec = specs.get((sport, source_key))
                if spec is None:
                    errors.append(f"{source_key}:{event_id}:source_not_approved_for_event_sport")
                    continue
                if str(raw.get("sport", "")).casefold().strip() != sport:
                    errors.append(f"{source_key}:{event_id}:sport_mismatch")
                    continue
                if str(raw.get("source_key", "")).casefold().strip() != source_key:
                    errors.append(f"{source_key}:{event_id}:source_key_mismatch")
                    continue
                if str(raw.get("category", "")).strip() != spec.category:
                    errors.append(f"{source_key}:{event_id}:category_mismatch")
                    continue

                sanitized = dict(raw)
                sanitized["source_key"] = source_key
                sanitized["sport"] = sport
                sanitized["category"] = spec.category
                sanitized["official"] = bool(spec.official)
                sanitized["source_url"] = spec.url
                try:
                    adapter = StructuredResearchAdapter(source_key, (sanitized,))
                    observations = tuple(adapter.collect(event=event, as_of=now))
                    if len(observations) != 1:
                        raise ValueError("Research feed row did not produce one observation")
                    observation = observations[0]
                    collected_iso = _utc(observation.collected_at).isoformat()
                    exists, same_payload = _already_stored(
                        connection,
                        event_id=event_id,
                        category=observation.category,
                        source_key=source_key,
                        collected_at=collected_iso,
                        payload=observation.payload or {},
                    )
                    if exists:
                        if not same_payload:
                            raise ValueError(
                                "Research feed changed evidence without changing collected_at"
                            )
                        source_duplicates += 1
                        continue
                    persist_observation(connection, observation, as_of=now)
                    source_stored += 1
                except Exception as exc:
                    errors.append(f"{source_key}:{event_id}:{exc}")
        except Exception as exc:
            safe_error = str(exc).replace(url, "[REDACTED_FEED_URL]")
            errors.append(f"{source_key}:{safe_error}")
            source_results[source_key] = {
                "status": "failed",
                "stored": source_stored,
                "duplicates": source_duplicates,
                "ignored": source_ignored,
            }
            continue

        stored += source_stored
        duplicates += source_duplicates
        ignored += source_ignored
        source_results[source_key] = {
            "status": "collected",
            "stored": source_stored,
            "duplicates": source_duplicates,
            "ignored": source_ignored,
            "unavailable_events": source_unavailable_events,
        }

    event_reports = {
        event_id: live_readiness_report(
            connection,
            event_id=event_id,
            sport=str(event["sport"]),
            as_of=now,
        ).as_dict()
        for event_id, event in events.items()
    }
    return {
        "status": (
            "completed"
            if not errors and unavailable_sources == 0
            else "completed_with_blockers"
        ),
        "requests_made": requests_made,
        "stored": stored,
        "duplicates": duplicates,
        "ignored": ignored,
        "errors": tuple(errors),
        "deferred_sources": tuple(deferred_sources),
        "sources": source_results,
        "events": event_reports,
    }
