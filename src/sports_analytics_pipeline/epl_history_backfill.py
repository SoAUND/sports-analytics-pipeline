"""Leakage-safe EPL feature backfill from football-data.org match archives.

Only features that can be reconstructed from completed matches strictly before
the target kickoff are written.  Expected-goals and availability features are
intentionally left untouched because the match archive does not prove them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
from statistics import mean
from typing import Any, Iterable

from .collectors.http import HttpTransport
from .multisport_history_builder import import_feature_evidence


UTC = timezone.utc
FOOTBALL_DATA_MATCHES_URL = "https://api.football-data.org/v4/competitions/PL/matches"
FOOTBALL_DATA_CHAMPIONSHIP_MATCHES_URL = (
    "https://api.football-data.org/v4/competitions/ELC/matches"
)
FOOTBALL_DATA_ARCHIVES_URL = "https://api.football-data.org/v4/competitions"
SOURCE_KEY = "football_data_epl_results_archive"
FEATURE_SOURCE_KEY = "football_data_epl_championship_results_archive"
DERIVED_FEATURES = (
    "recent5_points_diff",
    "home_away_strength_diff",
    "rest_days_diff",
    "fixture_congestion_diff",
)
FORM_MATCHES = 5
CONGESTION_DAYS = 14


@dataclass(frozen=True)
class ArchivedMatch:
    match_id: str
    kickoff: datetime
    last_updated: datetime
    home_name: str
    away_name: str
    home_key: str
    away_key: str
    home_score: float
    away_score: float
    competition_code: str = "PL"
    source_url: str = FOOTBALL_DATA_MATCHES_URL


def _dt(value: Any, *, label: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a time zone")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _team_key(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()
    tokens = [token for token in text.split() if token not in {"fc", "afc", "the"}]
    aliases = {
        "brighton hove albion": "brighton",
        "manchester city": "man city",
        "manchester united": "man united",
        "newcastle united": "newcastle",
        "nottingham forest": "nottingham forest",
        "tottenham hotspur": "tottenham",
        "west ham united": "west ham",
        "wolverhampton wanderers": "wolves",
    }
    normalized = " ".join(tokens)
    return aliases.get(normalized, normalized)


def parse_football_data_matches(
    payload: dict[str, Any],
    *,
    competition_code: str = "PL",
    source_url: str = FOOTBALL_DATA_MATCHES_URL,
) -> list[ArchivedMatch]:
    """Parse only finished matches with scores and auditable update times."""
    if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
        raise ValueError("football-data response is missing matches")
    matches: list[ArchivedMatch] = []
    for raw in payload["matches"]:
        if not isinstance(raw, dict) or str(raw.get("status", "")).upper() != "FINISHED":
            continue
        home = raw.get("homeTeam")
        away = raw.get("awayTeam")
        score = raw.get("score")
        full_time = score.get("fullTime") if isinstance(score, dict) else None
        if not isinstance(home, dict) or not isinstance(away, dict) or not isinstance(full_time, dict):
            continue
        if full_time.get("home") is None or full_time.get("away") is None:
            continue
        try:
            kickoff = _dt(raw.get("utcDate"), label="football-data utcDate")
            updated = _dt(raw.get("lastUpdated"), label="football-data lastUpdated")
            home_name = str(home["name"]).strip()
            away_name = str(away["name"]).strip()
            match_id = str(raw["id"]).strip()
            home_score = float(full_time["home"])
            away_score = float(full_time["away"])
        except (KeyError, TypeError, ValueError):
            continue
        if not match_id or not home_name or not away_name or home_name == away_name:
            continue
        matches.append(
            ArchivedMatch(
                match_id=match_id,
                kickoff=kickoff,
                last_updated=updated,
                home_name=home_name,
                away_name=away_name,
                home_key=_team_key(home_name),
                away_key=_team_key(away_name),
                home_score=home_score,
                away_score=away_score,
                competition_code=competition_code,
                source_url=source_url,
            )
        )
    return sorted(matches, key=lambda row: (row.kickoff, row.match_id))


def _points(match: ArchivedMatch, team_key: str) -> float:
    if match.home_score == match.away_score:
        return 1.0
    won = (
        team_key == match.home_key and match.home_score > match.away_score
    ) or (
        team_key == match.away_key and match.away_score > match.home_score
    )
    return 3.0 if won else 0.0


def _goal_difference(match: ArchivedMatch, team_key: str) -> float:
    if team_key == match.home_key:
        return match.home_score - match.away_score
    return match.away_score - match.home_score


def _safe_prior_matches(
    archive: Iterable[ArchivedMatch],
    *,
    team_key: str,
    target_time: datetime,
) -> list[ArchivedMatch]:
    return sorted(
        (
            match
            for match in archive
            if team_key in {match.home_key, match.away_key}
            and match.kickoff < target_time
            and match.last_updated < target_time
        ),
        key=lambda row: row.kickoff,
        reverse=True,
    )


def _evidence_row(
    event: sqlite3.Row,
    *,
    feature_name: str,
    feature_value: float,
    source_matches: list[ArchivedMatch],
    formula: str,
) -> dict[str, Any]:
    cutoff = max(match.last_updated for match in source_matches)
    competitions = sorted({match.competition_code for match in source_matches})
    source_urls = sorted({match.source_url for match in source_matches})
    uses_championship = "ELC" in competitions
    return {
        "provider_key": str(event["provider_key"]),
        "source_event_id": str(event["source_event_id"]),
        "sport": "epl",
        "feature_name": feature_name,
        "feature_value": feature_value,
        "source_key": FEATURE_SOURCE_KEY if uses_championship else SOURCE_KEY,
        "source_url": FOOTBALL_DATA_ARCHIVES_URL if len(source_urls) > 1 else source_urls[0],
        "source_updated_at": _iso(cutoff),
        "evidence": {
            "formula": formula,
            "archive_match_ids": sorted({match.match_id for match in source_matches}),
            "archive_competitions": competitions,
            "archive_source_urls": source_urls,
            "target_home": str(event["home_name"]),
            "target_away": str(event["away_name"]),
        },
    }


def derive_event_features(
    event: sqlite3.Row,
    archive: list[ArchivedMatch],
    *,
    verification_archive: list[ArchivedMatch] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return safe result-derived evidence and exact blockers for one event."""
    target_time = _dt(event["event_time"], label="historical event_time")
    home_key = _team_key(event["home_name"])
    away_key = _team_key(event["away_name"])
    target_archive = verification_archive if verification_archive is not None else archive
    target_matches = [
        match
        for match in target_archive
        if match.home_key == home_key
        and match.away_key == away_key
        and abs((match.kickoff - target_time).total_seconds()) <= 6 * 60 * 60
    ]
    if not target_matches:
        return [], ["target_match_not_found_in_football_data_archive"]

    home_prior = _safe_prior_matches(archive, team_key=home_key, target_time=target_time)
    away_prior = _safe_prior_matches(archive, team_key=away_key, target_time=target_time)
    rows: list[dict[str, Any]] = []
    blockers: list[str] = []

    if len(home_prior) >= FORM_MATCHES and len(away_prior) >= FORM_MATCHES:
        home_form = home_prior[:FORM_MATCHES]
        away_form = away_prior[:FORM_MATCHES]
        rows.append(
            _evidence_row(
                event,
                feature_name="recent5_points_diff",
                feature_value=sum(_points(match, home_key) for match in home_form)
                - sum(_points(match, away_key) for match in away_form),
                source_matches=home_form + away_form,
                formula="home league points over previous five minus away league points over previous five",
            )
        )
    else:
        blockers.append("recent5_requires_five_safe_prior_matches_per_team")

    home_venue = [match for match in home_prior if match.home_key == home_key]
    away_venue = [match for match in away_prior if match.away_key == away_key]
    if len(home_venue) >= FORM_MATCHES and len(away_venue) >= FORM_MATCHES:
        home_sample = home_venue[:FORM_MATCHES]
        away_sample = away_venue[:FORM_MATCHES]
        rows.append(
            _evidence_row(
                event,
                feature_name="home_away_strength_diff",
                feature_value=mean(_goal_difference(match, home_key) for match in home_sample)
                - mean(_goal_difference(match, away_key) for match in away_sample),
                source_matches=home_sample + away_sample,
                formula="home prior-five home goal difference per match minus away prior-five away goal difference per match",
            )
        )
    else:
        blockers.append("home_away_strength_requires_five_safe_venue_matches_per_team")

    if home_prior and away_prior:
        home_last = home_prior[0]
        away_last = away_prior[0]
        rows.append(
            _evidence_row(
                event,
                feature_name="rest_days_diff",
                feature_value=(target_time - home_last.kickoff).total_seconds() / 86400.0
                - (target_time - away_last.kickoff).total_seconds() / 86400.0,
                source_matches=[home_last, away_last],
                formula="home days since previous league match minus away days since previous league match",
            )
        )

        window_start = target_time - timedelta(days=CONGESTION_DAYS)
        home_window = [match for match in home_prior if match.kickoff >= window_start]
        away_window = [match for match in away_prior if match.kickoff >= window_start]
        source_matches = list(
            {
                (match.competition_code, match.match_id): match
                for match in home_prior[:1] + away_prior[:1] + home_window + away_window
            }.values()
        )
        rows.append(
            _evidence_row(
                event,
                feature_name="fixture_congestion_diff",
                feature_value=float(len(home_window) - len(away_window)),
                source_matches=source_matches,
                formula=f"home minus away completed domestic league matches in previous {CONGESTION_DAYS} days",
            )
        )
    else:
        blockers.extend(("rest_days_requires_a_safe_prior_match_per_team", "fixture_congestion_requires_a_safe_prior_match_per_team"))

    return rows, blockers


def _matching_archive_events(
    event: sqlite3.Row,
    archive: list[ArchivedMatch],
) -> list[ArchivedMatch]:
    target_time = _dt(event["event_time"], label="historical event_time")
    home_key = _team_key(event["home_name"])
    away_key = _team_key(event["away_name"])
    return sorted(
        (
            match
            for match in archive
            if match.home_key == home_key
            and match.away_key == away_key
            and abs((match.kickoff - target_time).total_seconds()) <= 6 * 60 * 60
        ),
        key=lambda match: abs((match.kickoff - target_time).total_seconds()),
    )


def _record_event_verification(
    connection: sqlite3.Connection,
    event: sqlite3.Row,
    *,
    archive_matches: list[ArchivedMatch],
    verified_at: datetime,
) -> str:
    matched = _matching_archive_events(event, archive_matches)
    status = "verified" if matched else "unmatched"
    verifier_event_id = matched[0].match_id if matched else None
    evidence = {
        "target_home": str(event["home_name"]),
        "target_away": str(event["away_name"]),
        "target_time": str(event["event_time"]),
        "matching_archive_event_ids": [match.match_id for match in matched],
    }
    connection.execute(
        """
        INSERT INTO multisport_historical_event_verifications (
            provider_key, source_event_id, sport, verifier_key,
            verifier_event_id, verification_status, verified_at,
            evidence_json, test_only
        ) VALUES (?, ?, 'epl', ?, ?, ?, ?, ?, 1)
        ON CONFLICT(provider_key, source_event_id, sport, verifier_key) DO UPDATE SET
            verifier_event_id=excluded.verifier_event_id,
            verification_status=excluded.verification_status,
            verified_at=excluded.verified_at,
            evidence_json=excluded.evidence_json,
            test_only=1
        """,
        (
            str(event["provider_key"]),
            str(event["source_event_id"]),
            SOURCE_KEY,
            verifier_event_id,
            status,
            _iso(verified_at),
            _canonical(evidence),
        ),
    )
    return status


def _season_start_year(value: datetime) -> int:
    return value.year if value.month >= 7 else value.year - 1


def backfill_epl_result_features(
    connection: sqlite3.Connection,
    *,
    api_key: str,
    transport: HttpTransport,
    collected_at: datetime,
    max_requests: int = 4,
) -> dict[str, Any]:
    """Fetch bounded EPL/Championship archives and import safe pre-event features."""
    if not api_key:
        raise ValueError("football-data API key is required")
    if max_requests < 1 or max_requests > 10:
        raise ValueError("max_requests must be between 1 and 10")
    events = connection.execute(
        """
        SELECT * FROM multisport_historical_events
        WHERE sport='epl' AND status='settled' AND result_role IN ('home','away','draw')
        ORDER BY event_time, historical_event_id
        """
    ).fetchall()
    if not events:
        return {
            "status": "complete",
            "events_scanned": 0,
            "requests_made": 0,
            "requested_seasons": [],
            "requested_archives": [],
            "archive_matches": 0,
            "premier_league_archive_matches": 0,
            "championship_archive_matches": 0,
            "matched_events": 0,
            "event_verification_counts": {"verified": 0, "unmatched": 0},
            "feature_evidence_accepted": 0,
            "feature_evidence_duplicates": 0,
            "feature_evidence_rejected": 0,
            "feature_coverage_added": {name: 0 for name in DERIVED_FEATURES},
            "blocker_counts": {},
            "test_only": True,
        }

    event_times = [_dt(event["event_time"], label="historical event_time") for event in events]
    seasons = sorted(
        {
            season
            for event_time in event_times
            for season in (_season_start_year(event_time) - 1, _season_start_year(event_time))
        }
    )
    premier_archive_by_id: dict[str, ArchivedMatch] = {}
    feature_archive_by_id: dict[tuple[str, str], ArchivedMatch] = {}
    requests_made = 0
    requested_seasons: list[int] = []
    requested_archives: list[dict[str, Any]] = []
    championship_seasons = sorted(
        {_season_start_year(event_time) - 1 for event_time in event_times}
    )
    request_plan = [
        ("PL", FOOTBALL_DATA_MATCHES_URL, season) for season in seasons
    ] + [
        ("ELC", FOOTBALL_DATA_CHAMPIONSHIP_MATCHES_URL, season)
        for season in championship_seasons
    ]
    for competition_code, source_url, season in request_plan:
        if requests_made >= max_requests:
            break
        response = transport.get(
            source_url,
            params={"season": str(season), "status": "FINISHED"},
            headers={"X-Auth-Token": api_key},
        )
        requests_made += 1
        requested_archives.append(
            {"competition": competition_code, "season": season}
        )
        if competition_code == "PL":
            requested_seasons.append(season)
        for match in parse_football_data_matches(
            response.json(),
            competition_code=competition_code,
            source_url=source_url,
        ):
            feature_archive_by_id[(competition_code, match.match_id)] = match
            if competition_code == "PL":
                premier_archive_by_id[match.match_id] = match
    premier_archive = sorted(
        premier_archive_by_id.values(), key=lambda row: (row.kickoff, row.match_id)
    )
    feature_archive = sorted(
        feature_archive_by_id.values(), key=lambda row: (row.kickoff, row.match_id)
    )

    evidence: list[dict[str, Any]] = []
    blocker_counts: Counter[str] = Counter()
    verification_counts: Counter[str] = Counter()
    matched_events = 0
    coverage: Counter[str] = Counter()
    for event in events:
        verification_counts[
            _record_event_verification(
                connection,
                event,
                archive_matches=premier_archive,
                verified_at=collected_at,
            )
        ] += 1
        rows, blockers = derive_event_features(
            event,
            feature_archive,
            verification_archive=premier_archive,
        )
        if "target_match_not_found_in_football_data_archive" not in blockers:
            matched_events += 1
        evidence.extend(rows)
        for row in rows:
            coverage[str(row["feature_name"])] += 1
        blocker_counts.update(blockers)

    imported = import_feature_evidence(connection, evidence, collected_at=collected_at)
    return {
        "status": "complete" if len(requested_archives) == len(request_plan) else "partial",
        "events_scanned": len(events),
        "requests_made": requests_made,
        "requested_seasons": requested_seasons,
        "requested_archives": requested_archives,
        "archive_matches": len(feature_archive),
        "premier_league_archive_matches": len(premier_archive),
        "championship_archive_matches": sum(
            match.competition_code == "ELC" for match in feature_archive
        ),
        "matched_events": matched_events,
        "event_verification_counts": {
            status: int(verification_counts[status])
            for status in ("verified", "unmatched")
        },
        "feature_evidence_accepted": imported["accepted"],
        "feature_evidence_duplicates": imported["duplicates"],
        "feature_evidence_rejected": imported["rejected"],
        "feature_coverage_added": {name: int(coverage[name]) for name in DERIVED_FEATURES},
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "remaining_features_not_provided": ["availability_impact_diff", "xg_diff", "xga_diff"],
        "test_only": True,
    }
