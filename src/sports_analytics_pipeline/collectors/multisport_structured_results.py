"""Automatic, fail-closed final-result collection for MMA and tennis.

SportsGameOdds exposes finalized event status plus scores/results for these sports.
We only accept an exact two-participant match and never infer an unfinished result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import unicodedata
from typing import Any

from .http import HttpTransport


BASE_URL = "https://api.sportsgameodds.com/v2"
PROVIDER_KEY = "sports_game_odds"
SUPPORTED_SPORTS = {"mma"}
PARSER_SUPPORTED_SPORTS = {"mma", "atp", "wta"}
SPORT_IDS = {"mma": "MMA"}
UTC = timezone.utc


def _normalized_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(character for character in text if not unicodedata.combining(character))
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _team_name(team: Any) -> str:
    if not isinstance(team, dict):
        return ""
    names = team.get("names")
    if isinstance(names, dict):
        for key in ("long", "medium", "short", "display"):
            if names.get(key):
                return str(names[key]).strip()
    for key in ("name", "displayName", "fullName"):
        if team.get(key):
            return str(team[key]).strip()
    return ""


def _score(team: Any) -> float | None:
    if not isinstance(team, dict):
        return None
    value = team.get("score")
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _status_text(status: dict[str, Any]) -> str:
    values = [
        status.get("displayLong"),
        status.get("displayShort"),
        status.get("description"),
        status.get("state"),
    ]
    return " ".join(str(value) for value in values if value).casefold()


def _period_codes(status: dict[str, Any], key: str) -> set[str]:
    periods = status.get("periods")
    if not isinstance(periods, dict):
        return set()
    raw = periods.get(key)
    if not isinstance(raw, list):
        return set()
    return {str(value).casefold() for value in raw}


@dataclass(frozen=True)
class StructuredFinalResult:
    result_status: str
    home_score: float | None
    away_score: float | None
    winner_role: str
    source_updated_at: datetime
    collected_at: datetime
    raw_result: dict[str, Any]


def parse_finalized_event(
    event: dict[str, Any],
    *,
    sport: str,
    local_home_name: str,
    local_away_name: str,
    collected_at: datetime,
) -> StructuredFinalResult | None:
    """Normalize one exact finalized provider event into local home/away orientation."""
    if sport not in PARSER_SUPPORTED_SPORTS:
        raise ValueError(f"Structured results are not approved for {sport}")
    status = event.get("status")
    if not isinstance(status, dict):
        return None
    cancelled = status.get("cancelled") is True
    if status.get("finalized") is not True and not cancelled:
        return None

    teams = event.get("teams")
    if not isinstance(teams, dict):
        return None
    provider_home = teams.get("home")
    provider_away = teams.get("away")
    provider_home_name = _team_name(provider_home)
    provider_away_name = _team_name(provider_away)
    local_pair = (_normalized_name(local_home_name), _normalized_name(local_away_name))
    provider_pair = (_normalized_name(provider_home_name), _normalized_name(provider_away_name))
    if not all((*local_pair, *provider_pair)):
        return None
    if provider_pair == local_pair:
        reversed_orientation = False
    elif provider_pair == local_pair[::-1]:
        reversed_orientation = True
    else:
        return None

    provider_home_score = _score(provider_home)
    provider_away_score = _score(provider_away)
    if reversed_orientation:
        home_score, away_score = provider_away_score, provider_home_score
    else:
        home_score, away_score = provider_home_score, provider_away_score

    text = _status_text(status)
    if cancelled:
        result_status = "cancelled"
        winner_role = "void"
    elif sport == "mma" and ("no contest" in text or "no-contest" in text):
        result_status = "no_contest"
        winner_role = "void"
    elif sport in {"atp", "wta"} and ("walkover" in text or re.search(r"\bw/?o\b", text)):
        result_status = "walkover"
        winner_role = "void"
    else:
        result_status = (
            "retirement"
            if sport in {"atp", "wta"} and ("retired" in text or "retirement" in text)
            else "final"
        )
        if home_score is None or away_score is None:
            return None
        winner_role = "home" if home_score > away_score else "away" if away_score > home_score else "draw"

    ended_periods = _period_codes(status, "ended")
    started_periods = _period_codes(status, "started")
    raw_result = dict(event)
    raw_result["provider_event_id"] = str(event.get("eventID") or event.get("eventId") or "")
    raw_result["provider_status"] = dict(status)
    raw_result["first_set_completed"] = any(
        code in {"1", "1s", "set1", "set_1", "first_set"} or code.startswith("1s")
        for code in ended_periods
    )
    raw_result["at_least_one_point_completed"] = bool(
        started_periods
        or ended_periods
        or (home_score is not None and away_score is not None and (home_score != 0 or away_score != 0))
    )
    return StructuredFinalResult(
        result_status=result_status,
        home_score=home_score,
        away_score=away_score,
        winner_role=winner_role,
        source_updated_at=collected_at.astimezone(UTC),
        collected_at=collected_at.astimezone(UTC),
        raw_result=raw_result,
    )


class StructuredResultsCollector:
    provider_key = PROVIDER_KEY

    def __init__(self, *, api_key: str, transport: HttpTransport):
        if not api_key.strip():
            raise ValueError("A non-empty SportsGameOdds API key is required")
        self._api_key = api_key
        self._transport = transport
        self._event_cache: dict[str, tuple[dict[str, Any], ...]] = {}
        self._received_at_cache: dict[str, datetime] = {}
        self._event_errors: dict[str, str] = {}

    def _current_events(self, sport: str) -> tuple[dict[str, Any], ...]:
        if sport in self._event_cache:
            return self._event_cache[sport]
        if sport in self._event_errors:
            raise RuntimeError(self._event_errors[sport])
        params = {"started": "true", "limit": "30", "sportID": SPORT_IDS[sport]}
        try:
            response = self._transport.get(
                f"{BASE_URL}/events/",
                headers={"x-api-key": self._api_key},
                params=params,
            )
            payload = response.json()
        except Exception as exc:
            self._event_errors[sport] = str(exc)
            raise
        if isinstance(payload, dict):
            events = payload.get("data")
            if not isinstance(events, list):
                events = payload.get("events")
        else:
            events = payload
        if not isinstance(events, list):
            raise ValueError("SportsGameOdds events response must contain a list")
        self._event_cache[sport] = tuple(event for event in events if isinstance(event, dict))
        self._received_at_cache[sport] = response.received_at
        return self._event_cache[sport]

    def collect_event(
        self,
        *,
        sport: str,
        local_home_name: str,
        local_away_name: str,
        commence_time: datetime,
    ) -> StructuredFinalResult | None:
        if sport not in SUPPORTED_SPORTS:
            raise ValueError(f"Structured results are not approved for {sport}")
        if commence_time.tzinfo is None or commence_time.utcoffset() is None:
            raise ValueError("commence_time must include a time zone")
        matches: list[StructuredFinalResult] = []
        for event in self._current_events(sport):
            normalized = parse_finalized_event(
                event,
                sport=sport,
                local_home_name=local_home_name,
                local_away_name=local_away_name,
                collected_at=self._received_at_cache[sport],
            )
            if normalized is not None:
                matches.append(normalized)
        if len(matches) > 1:
            raise ValueError("Multiple finalized provider events matched the same local participants")
        return matches[0] if matches else None
