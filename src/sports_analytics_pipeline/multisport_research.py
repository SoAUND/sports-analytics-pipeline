"""Sport-specific, fail-closed live research contracts for our newer sports.

NFL and MLB retain their protected research paths. This module governs NBA,
WNBA, EPL, MMA, ATP, and WTA and never turns missing research into a selection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import sqlite3
from typing import Any, Iterable, Mapping


UTC = timezone.utc


@dataclass(frozen=True)
class ResearchSourceSpec:
    source_key: str
    display_name: str
    category: str
    url: str
    official: bool
    collection_mode: str


@dataclass(frozen=True)
class SportResearchPolicy:
    sport: str
    freshness_minutes: Mapping[str, int]
    official_required_categories: frozenset[str]
    material_change_categories: frozenset[str]


@dataclass(frozen=True)
class LiveReadinessReport:
    event_id: str
    sport: str
    as_of: datetime
    ready: bool
    blockers: tuple[str, ...]
    category_statuses: Mapping[str, str]
    freshness_deadlines: Mapping[str, str]

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "sport": self.sport,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "ready": self.ready,
            "blockers": self.blockers,
            "category_statuses": dict(self.category_statuses),
            "freshness_deadlines": dict(self.freshness_deadlines),
        }


REQUIRED_CATEGORIES: dict[str, tuple[str, ...]] = {
    "nba": (
        "official_status", "injury_availability", "lineup_or_participants",
        "rest_travel", "statistics_analytics", "transactions_or_changes", "current_odds",
    ),
    "wnba": (
        "official_status", "injury_availability", "lineup_or_participants",
        "rest_travel", "statistics_analytics", "transactions_or_changes", "current_odds",
    ),
    "epl": (
        "official_status", "injury_availability", "lineup_or_participants",
        "weather_environment", "rest_travel", "statistics_analytics",
        "transactions_or_changes", "current_odds",
    ),
    "mma": (
        "official_status", "injury_availability", "lineup_or_participants",
        "weigh_in_status", "rest_travel", "statistics_analytics",
        "transactions_or_changes", "current_odds",
    ),
    "atp": (
        "official_status", "injury_availability", "lineup_or_participants",
        "weather_environment", "rest_travel", "statistics_analytics", "current_odds",
    ),
    "wta": (
        "official_status", "injury_availability", "lineup_or_participants",
        "weather_environment", "rest_travel", "statistics_analytics", "current_odds",
    ),
}


def _policy(
    sport: str,
    freshness: Mapping[str, int],
    *,
    official: Iterable[str],
    material: Iterable[str],
) -> SportResearchPolicy:
    return SportResearchPolicy(
        sport=sport,
        freshness_minutes=dict(freshness),
        official_required_categories=frozenset(official),
        material_change_categories=frozenset(material),
    )


RESEARCH_POLICIES: dict[str, SportResearchPolicy] = {
    "nba": _policy("nba", {
        "official_status": 180, "injury_availability": 90,
        "lineup_or_participants": 90, "rest_travel": 720,
        "statistics_analytics": 1440, "transactions_or_changes": 720,
        "current_odds": 60,
    }, official=("official_status", "injury_availability", "lineup_or_participants", "rest_travel", "transactions_or_changes"),
        material=("official_status", "injury_availability", "transactions_or_changes")),
    "wnba": _policy("wnba", {
        "official_status": 180, "injury_availability": 90,
        "lineup_or_participants": 90, "rest_travel": 720,
        "statistics_analytics": 1440, "transactions_or_changes": 720,
        "current_odds": 60,
    }, official=("official_status", "injury_availability", "lineup_or_participants", "rest_travel", "transactions_or_changes"),
        material=("official_status", "injury_availability", "transactions_or_changes")),
    "epl": _policy("epl", {
        "official_status": 180, "injury_availability": 180,
        "lineup_or_participants": 90, "weather_environment": 180,
        "rest_travel": 720, "statistics_analytics": 1440,
        "transactions_or_changes": 720, "current_odds": 60,
    }, official=("official_status", "lineup_or_participants", "rest_travel"),
        material=("official_status", "injury_availability", "transactions_or_changes")),
    "mma": _policy("mma", {
        "official_status": 360, "injury_availability": 360,
        "lineup_or_participants": 360, "weigh_in_status": 2160,
        "rest_travel": 1440, "statistics_analytics": 1440,
        "transactions_or_changes": 360, "current_odds": 60,
    }, official=("official_status", "injury_availability", "lineup_or_participants", "weigh_in_status", "rest_travel"),
        material=("official_status", "injury_availability", "transactions_or_changes", "weigh_in_status")),
    "atp": _policy("atp", {
        "official_status": 180, "injury_availability": 180,
        "lineup_or_participants": 180, "weather_environment": 180,
        "rest_travel": 720, "statistics_analytics": 1440, "current_odds": 60,
    }, official=("official_status", "injury_availability", "lineup_or_participants", "rest_travel"),
        material=("official_status", "injury_availability")),
    "wta": _policy("wta", {
        "official_status": 180, "injury_availability": 180,
        "lineup_or_participants": 180, "weather_environment": 180,
        "rest_travel": 720, "statistics_analytics": 1440, "current_odds": 60,
    }, official=("official_status", "injury_availability", "lineup_or_participants", "rest_travel"),
        material=("official_status", "injury_availability")),
}


RESEARCH_SOURCES: dict[str, tuple[ResearchSourceSpec, ...]] = {
    "nba": (
        ResearchSourceSpec("nba_official", "NBA Official", "official_status", "https://official.nba.com/", True, "structured_evidence_required"),
        ResearchSourceSpec("nba_injury_report", "NBA Injury Report", "injury_availability", "https://official.nba.com/nba-injury-report-2025-26-season/", True, "season_url_refresh_required"),
        ResearchSourceSpec("nba_lineups", "NBA Today's Lineups", "lineup_or_participants", "https://www.nba.com/players/todays-lineups", True, "structured_evidence_required"),
        ResearchSourceSpec("nba_schedule_rest", "NBA Schedule", "rest_travel", "https://www.nba.com/schedule", True, "derived_from_official_schedule"),
        ResearchSourceSpec("nba_stats", "NBA Stats", "statistics_analytics", "https://www.nba.com/stats", True, "structured_evidence_required"),
        ResearchSourceSpec("nba_transactions", "NBA Transactions", "transactions_or_changes", "https://www.nba.com/players/transactions", True, "structured_evidence_required"),
        ResearchSourceSpec("the_odds_api", "The Odds API", "current_odds", "https://the-odds-api.com/", False, "api"),
    ),
    "wnba": (
        ResearchSourceSpec("wnba_official", "WNBA", "official_status", "https://www.wnba.com/", True, "structured_evidence_required"),
        ResearchSourceSpec("wnba_injury_report", "WNBA Injury Report", "injury_availability", "https://www.wnba.com/wnba-injury-report", True, "structured_evidence_required"),
        ResearchSourceSpec("wnba_roster_tracker", "WNBA Roster Tracker", "lineup_or_participants", "https://www.wnba.com/players/roster-tracker", True, "structured_evidence_required"),
        ResearchSourceSpec("wnba_schedule_rest", "WNBA Schedule", "rest_travel", "https://www.wnba.com/schedule", True, "derived_from_official_schedule"),
        ResearchSourceSpec("basketball_reference", "Basketball Reference", "statistics_analytics", "https://www.basketball-reference.com/wnba/", False, "structured_evidence_required"),
        ResearchSourceSpec("wnba_transactions", "WNBA Transactions", "transactions_or_changes", "https://www.wnba.com/players/transactions", True, "structured_evidence_required"),
        ResearchSourceSpec("the_odds_api", "The Odds API", "current_odds", "https://the-odds-api.com/", False, "api"),
    ),
    "epl": (
        ResearchSourceSpec("premier_league", "Premier League", "official_status", "https://www.premierleague.com/", True, "structured_evidence_required"),
        ResearchSourceSpec("premier_league_team_news", "Premier League Team News", "injury_availability", "https://www.premierleague.com/en/news", True, "structured_evidence_required"),
        ResearchSourceSpec("premier_league_lineups", "Premier League Match Lineups", "lineup_or_participants", "https://www.premierleague.com/en/matchweek", True, "available_about_75_minutes_before_kickoff"),
        ResearchSourceSpec("open_meteo_epl", "Open-Meteo Forecast", "weather_environment", "https://open-meteo.com/en/docs", False, "api_with_venue_coordinates"),
        ResearchSourceSpec("premier_league_fixtures_rest", "Premier League Fixtures", "rest_travel", "https://www.premierleague.com/en/fixtures", True, "derived_from_official_schedule"),
        ResearchSourceSpec("premier_league_statistics", "Premier League Derived Form Statistics", "statistics_analytics", "https://www.premierleague.com/en/fixtures", True, "derived_from_official_results"),
        ResearchSourceSpec("premier_league_squad_changes", "Premier League Matchday Squad Changes", "transactions_or_changes", "https://www.premierleague.com/en/matchweek", True, "derived_from_official_matchday_squads"),
        ResearchSourceSpec("the_odds_api", "The Odds API", "current_odds", "https://the-odds-api.com/", False, "api"),
    ),
    "mma": (
        ResearchSourceSpec("ufc_news", "UFC News", "official_status", "https://www.ufc.com/news", True, "structured_evidence_required"),
        ResearchSourceSpec("ufc_news_injuries", "UFC News", "injury_availability", "https://www.ufc.com/news", True, "structured_evidence_required"),
        ResearchSourceSpec("ufc_event", "UFC Event", "lineup_or_participants", "https://www.ufc.com/events", True, "structured_evidence_required"),
        ResearchSourceSpec("ufc_weigh_in", "UFC Official Weigh-In Results", "weigh_in_status", "https://www.ufc.com/news", True, "structured_evidence_required"),
        ResearchSourceSpec("ufc_event_rest_travel", "UFC Event Schedule", "rest_travel", "https://www.ufc.com/events", True, "derived_from_official_schedule"),
        ResearchSourceSpec("ufcstats", "UFCStats", "statistics_analytics", "http://ufcstats.com/", True, "structured_evidence_required"),
        ResearchSourceSpec("tapology", "Tapology", "transactions_or_changes", "https://www.tapology.com/", False, "structured_evidence_required"),
        ResearchSourceSpec("the_odds_api", "The Odds API", "current_odds", "https://the-odds-api.com/", False, "api"),
    ),
    "atp": (
        ResearchSourceSpec("atp_tour", "ATP Tour", "official_status", "https://www.atptour.com/en/scores/current", True, "structured_evidence_required"),
        ResearchSourceSpec("atp_tour_status", "ATP Tour Status", "injury_availability", "https://www.atptour.com/en/scores/current", True, "structured_evidence_required"),
        ResearchSourceSpec("atp_draws", "ATP Draws and Order of Play", "lineup_or_participants", "https://www.atptour.com/en/scores/current", True, "structured_evidence_required"),
        ResearchSourceSpec("open_meteo_atp", "Open-Meteo Forecast", "weather_environment", "https://open-meteo.com/en/docs", False, "api_with_venue_coordinates_or_indoor_confirmation"),
        ResearchSourceSpec("atp_schedule_rest", "ATP Scores and Schedule", "rest_travel", "https://www.atptour.com/en/scores/current", True, "derived_from_official_schedule"),
        ResearchSourceSpec("tennis_abstract", "Tennis Abstract", "statistics_analytics", "https://www.tennisabstract.com/", False, "structured_evidence_required"),
        ResearchSourceSpec("the_odds_api", "The Odds API", "current_odds", "https://the-odds-api.com/", False, "api"),
    ),
    "wta": (
        ResearchSourceSpec("wta", "WTA", "official_status", "https://www.wtatennis.com/tournaments", True, "structured_evidence_required"),
        ResearchSourceSpec("wta_status", "WTA Status", "injury_availability", "https://www.wtatennis.com/tournaments", True, "structured_evidence_required"),
        ResearchSourceSpec("wta_draws", "WTA Draws and Order of Play", "lineup_or_participants", "https://www.wtatennis.com/tournaments", True, "structured_evidence_required"),
        ResearchSourceSpec("open_meteo_wta", "Open-Meteo Forecast", "weather_environment", "https://open-meteo.com/en/docs", False, "api_with_venue_coordinates_or_indoor_confirmation"),
        ResearchSourceSpec("wta_schedule_rest", "WTA Order of Play", "rest_travel", "https://www.wtatennis.com/tournaments", True, "derived_from_official_schedule"),
        ResearchSourceSpec("tennis_abstract", "Tennis Abstract", "statistics_analytics", "https://www.tennisabstract.com/", False, "structured_evidence_required"),
        ResearchSourceSpec("the_odds_api", "The Odds API", "current_odds", "https://the-odds-api.com/", False, "api"),
    ),
}


def research_sources(sport: str) -> tuple[ResearchSourceSpec, ...]:
    key = sport.casefold().strip()
    try:
        return RESEARCH_SOURCES[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported research sport: {sport!r}") from exc


def required_categories(sport: str) -> tuple[str, ...]:
    key = sport.casefold().strip()
    try:
        return REQUIRED_CATEGORIES[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported research sport: {sport!r}") from exc


def research_policy(sport: str) -> SportResearchPolicy:
    key = sport.casefold().strip()
    try:
        return RESEARCH_POLICIES[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported research sport: {sport!r}") from exc


def research_policy_summary(sport: str) -> dict[str, object]:
    policy = research_policy(sport)
    return {
        "required_categories": required_categories(sport),
        "freshness_minutes": dict(policy.freshness_minutes),
        "official_required_categories": tuple(sorted(policy.official_required_categories)),
        "late_change_detection": True,
    }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a time zone")
    return value.astimezone(UTC)


def _parsed(value: object) -> datetime:
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _canonical_evidence(evidence: Mapping[str, Any] | None) -> tuple[str, str | None]:
    payload = dict(evidence or {})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return encoded, sha256(encoded.encode("utf-8")).hexdigest() if payload else None


def record_research_check(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    sport: str,
    category: str,
    source_key: str,
    verification_status: str,
    official: bool,
    source_url: str | None = None,
    source_updated_at: datetime | None = None,
    collected_at: datetime | None = None,
    notes: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> int:
    if category not in required_categories(sport):
        raise ValueError(f"Category {category!r} is not required for {sport}")
    if verification_status not in {"verified", "conditional", "unavailable", "conflict"}:
        raise ValueError("Invalid verification status")
    directory = {spec.source_key: spec for spec in research_sources(sport)}
    spec = directory.get(source_key)
    if spec is None or spec.category != category:
        raise ValueError("Research source/category pairing is not approved")
    if bool(official) != bool(spec.official):
        raise ValueError("Research source official flag does not match our directory")
    collected = _utc(collected_at or datetime.now(UTC))
    updated = _utc(source_updated_at) if source_updated_at is not None else None
    if updated is not None and updated > collected:
        raise ValueError("Source update cannot follow collection time")
    evidence_json, evidence_hash = _canonical_evidence(evidence)
    cursor = connection.execute(
        """
        INSERT INTO multisport_research_checks (
            event_id, sport, category, source_key, source_url, official,
            verification_status, source_updated_at, collected_at, notes,
            evidence_json, evidence_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id, sport, category, source_key, source_url, 1 if official else 0,
            verification_status, updated.isoformat() if updated else None,
            collected.isoformat(), notes, evidence_json, evidence_hash,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def live_readiness_report(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    sport: str,
    as_of: datetime,
    odds_stale_after_minutes: int | None = None,
) -> LiveReadinessReport:
    """Apply sport-specific freshness, source, evidence, and late-change gates."""
    now = _utc(as_of)
    event = connection.execute(
        "SELECT sport, commence_time FROM multisport_events WHERE event_id=?", (event_id,)
    ).fetchone()
    if event is None or str(event["sport"]) != sport:
        raise ValueError("Unknown research event or sport mismatch")
    policy = research_policy(sport)
    freshness = dict(policy.freshness_minutes)
    if odds_stale_after_minutes is not None:
        freshness["current_odds"] = int(odds_stale_after_minutes)
    rows = connection.execute(
        """
        SELECT * FROM multisport_research_checks
        WHERE event_id=? AND sport=? AND collected_at<=?
        ORDER BY collected_at DESC, research_check_id DESC
        """,
        (event_id, sport, now.isoformat()),
    ).fetchall()
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest.setdefault(str(row["category"]), row)

    source_directory = {spec.source_key: spec for spec in research_sources(sport)}
    blockers: list[str] = []
    statuses: dict[str, str] = {}
    deadlines: dict[str, str] = {}
    for category in required_categories(sport):
        row = latest.get(category)
        if row is None:
            statuses[category] = "missing"
            blockers.append(f"{category}:missing")
            continue
        status = str(row["verification_status"])
        statuses[category] = status
        if status != "verified":
            blockers.append(f"{category}:{status}")
            continue
        spec = source_directory.get(str(row["source_key"]))
        if spec is None or spec.category != category:
            blockers.append(f"{category}:unapproved_source")
            continue
        if category in policy.official_required_categories and not bool(row["official"]):
            blockers.append(f"{category}:official_source_required")
        evidence = json.loads(str(row["evidence_json"] or "{}"))
        if not isinstance(evidence, dict) or not evidence:
            blockers.append(f"{category}:missing_event_specific_evidence")
        effective = _parsed(row["source_updated_at"] or row["collected_at"])
        deadline = effective + timedelta(minutes=freshness[category])
        deadlines[category] = deadline.isoformat()
        if now > deadline:
            age = int((now - effective).total_seconds() // 60)
            blockers.append(f"{category}:stale({age}m>{freshness[category]}m)")

    lineup = latest.get("lineup_or_participants")
    if lineup is not None:
        lineup_time = _parsed(lineup["collected_at"])
        for category in policy.material_change_categories:
            change = latest.get(category)
            if change is None or _parsed(change["collected_at"]) <= lineup_time:
                continue
            evidence = json.loads(str(change["evidence_json"] or "{}"))
            material = str(change["verification_status"]) == "conflict" or bool(
                isinstance(evidence, dict) and evidence.get("material_change")
            )
            if material:
                blockers.append(f"lineup_or_participants:refresh_required_after_{category}")

    deduplicated = tuple(dict.fromkeys(blockers))
    return LiveReadinessReport(
        event_id=event_id,
        sport=sport,
        as_of=now,
        ready=not deduplicated,
        blockers=deduplicated,
        category_statuses=statuses,
        freshness_deadlines=deadlines,
    )


def research_gate(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    sport: str,
    as_of: datetime | None = None,
    odds_stale_after_minutes: int | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """Return the strict live gate, with a compatibility view for old diagnostics."""
    if as_of is not None:
        report = live_readiness_report(
            connection,
            event_id=event_id,
            sport=sport,
            as_of=as_of,
            odds_stale_after_minutes=odds_stale_after_minutes,
        )
        return report.ready, report.blockers
    rows = connection.execute(
        """
        SELECT category, verification_status, collected_at
        FROM multisport_research_checks
        WHERE event_id=? AND sport=?
        ORDER BY collected_at DESC, research_check_id DESC
        """,
        (event_id, sport),
    ).fetchall()
    latest: dict[str, str] = {}
    for row in rows:
        latest.setdefault(str(row["category"]), str(row["verification_status"]))
    blockers = tuple(
        f"{category}:{latest.get(category, 'missing')}"
        for category in required_categories(sport)
        if latest.get(category) != "verified"
    )
    return not blockers, blockers


def verify_source_coverage(sport: str) -> tuple[bool, tuple[str, ...]]:
    covered = {spec.category for spec in research_sources(sport)}
    missing = tuple(category for category in required_categories(sport) if category not in covered)
    return not missing, missing


def all_research_sports() -> Iterable[str]:
    return REQUIRED_CATEGORIES.keys()
