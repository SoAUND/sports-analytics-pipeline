"""Read-only result collection from The Odds API scores endpoint.

The provider's scores endpoint is useful for supported team sports and selected tennis
tournaments. We only normalize completed numeric results whose source event already
exists in our database. Tennis scores are accepted only when they contain a regulation
match-winning set count; ambiguous retirements and walkovers remain unsettled. MMA
results remain adapter-driven because decisions, draws, and no-contests need richer
settlement context.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from ..sports_registry import get_sport_market
from .http import HttpTransport


BASE_URL = "https://api.the-odds-api.com/v4"
PROVIDER_KEY = "the_odds_api"
TEAM_SCORE_SPORTS = {"nba", "wnba", "epl"}
TENNIS_SCORE_SPORTS = {"atp", "wta"}
AUTO_SCORE_SPORTS = TEAM_SCORE_SPORTS | TENNIS_SCORE_SPORTS


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Score timestamp must include a time zone")
    return parsed.astimezone(timezone.utc)


def _score(value: Any) -> float:
    try:
        result = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Non-numeric final score: {value!r}") from exc
    if result < 0:
        raise ValueError("Final score cannot be negative")
    return result


def _tennis_target_sets(*, sport: str, provider_sport_key: str) -> int:
    """Return the regulation winning-set count for a covered singles tournament."""
    if sport == "wta":
        return 2
    grand_slams = (
        "tennis_atp_aus_open",
        "tennis_atp_french_open",
        "tennis_atp_us_open",
        "tennis_atp_wimbledon",
    )
    return 3 if provider_sport_key.startswith(grand_slams) else 2


def _regulation_tennis_final(
    *, sport: str, provider_sport_key: str, home_score: float, away_score: float
) -> bool:
    """Reject generic completed rows that could represent a retirement or walkover."""
    if not home_score.is_integer() or not away_score.is_integer() or home_score == away_score:
        return False
    winner_sets = max(home_score, away_score)
    loser_sets = min(home_score, away_score)
    target = _tennis_target_sets(sport=sport, provider_sport_key=provider_sport_key)
    return winner_sets == target and 0 <= loser_sets < target


@dataclass(frozen=True)
class CollectedFinalScore:
    sport: str
    provider_sport_key: str
    source_event_id: str
    home_name: str
    away_name: str
    home_score: float
    away_score: float
    winner_role: str
    source_updated_at: datetime
    collected_at: datetime
    raw_result: dict[str, Any]


@dataclass(frozen=True)
class ScoreBatch:
    sport: str
    provider_sport_key: str
    collected_at: datetime
    scores: tuple[CollectedFinalScore, ...]
    raw_payload: Any
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] | None = None


def parse_scores(
    payload: list[dict[str, Any]],
    *,
    sport: str,
    provider_sport_key: str,
    collected_at: datetime,
) -> ScoreBatch:
    if sport not in AUTO_SCORE_SPORTS:
        raise ValueError(f"Automatic numeric score normalization is not approved for {sport}")
    spec = get_sport_market(sport)
    if not spec.matches_provider_sport_key(provider_sport_key):
        raise ValueError("Score provider key does not match controlled sport")
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must include a time zone")
    collected = collected_at.astimezone(timezone.utc)
    normalized: list[CollectedFinalScore] = []
    warnings: list[str] = []
    for event in payload:
        if str(event.get("sport_key", "")) != provider_sport_key:
            warnings.append(f"Skipped score event {event.get('id', '<missing>')} from another sport key")
            continue
        if event.get("completed") is not True:
            continue
        source_event_id = str(event.get("id", "")).strip()
        home_name = str(event.get("home_team", "")).strip()
        away_name = str(event.get("away_team", "")).strip()
        if not source_event_id or not home_name or not away_name:
            raise ValueError("Completed score event is missing identity fields")
        score_rows = event.get("scores")
        if not isinstance(score_rows, list) or len(score_rows) != 2:
            raise ValueError("Completed team-sport event must have exactly two score rows")
        scores_by_name = {str(row.get("name", "")): _score(row.get("score")) for row in score_rows}
        if set(scores_by_name) != {home_name, away_name}:
            raise ValueError("Score participant names do not match event participants")
        home_score = scores_by_name[home_name]
        away_score = scores_by_name[away_name]
        if sport in TENNIS_SCORE_SPORTS and not _regulation_tennis_final(
            sport=sport,
            provider_sport_key=provider_sport_key,
            home_score=home_score,
            away_score=away_score,
        ):
            warnings.append(
                f"Skipped ambiguous completed tennis score {source_event_id}; "
                "retirement/walkover status is not available from this source"
            )
            continue
        if home_score > away_score:
            winner = "home"
        elif away_score > home_score:
            winner = "away"
        else:
            winner = "draw"
        updated = _timestamp(str(event.get("last_update") or event.get("commence_time") or ""))
        if updated > collected:
            raise ValueError("Score source update cannot follow collection time")
        normalized.append(
            CollectedFinalScore(
                sport=sport,
                provider_sport_key=provider_sport_key,
                source_event_id=source_event_id,
                home_name=home_name,
                away_name=away_name,
                home_score=home_score,
                away_score=away_score,
                winner_role=winner,
                source_updated_at=updated,
                collected_at=collected,
                raw_result={
                    **dict(event),
                    **(
                        {
                            "first_set_completed": True,
                            "at_least_one_point_completed": True,
                        }
                        if sport in TENNIS_SCORE_SPORTS
                        else {}
                    ),
                },
            )
        )
    return ScoreBatch(
        sport=sport,
        provider_sport_key=provider_sport_key,
        collected_at=collected,
        scores=tuple(normalized),
        raw_payload=payload,
        warnings=tuple(warnings),
    )


class MultiSportScoresCollector:
    provider_key = PROVIDER_KEY

    def __init__(self, *, api_key: str, transport: HttpTransport):
        if not api_key.strip():
            raise ValueError("A non-empty Odds API key is required")
        self._api_key = api_key
        self._transport = transport

    def collect(
        self,
        *,
        sport: str,
        provider_sport_key: str,
        days_from: int = 3,
    ) -> ScoreBatch:
        if sport not in AUTO_SCORE_SPORTS:
            raise ValueError(f"Automatic score collection is not approved for {sport}")
        if not 1 <= days_from <= 3:
            raise ValueError("days_from must be between 1 and 3 for routine result checks")
        response = self._transport.get(
            f"{BASE_URL}/sports/{provider_sport_key}/scores/",
            params={
                "apiKey": self._api_key,
                "daysFrom": str(days_from),
                "dateFormat": "iso",
            },
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Scores response must be a list")
        batch = parse_scores(
            payload,
            sport=sport,
            provider_sport_key=provider_sport_key,
            collected_at=response.received_at,
        )
        headers: Mapping[str, str] = response.headers
        metadata = {
            "requests_remaining": headers.get("x-requests-remaining"),
            "requests_used": headers.get("x-requests-used"),
            "request_cost": headers.get("x-requests-last"),
            "endpoint": "scores",
        }
        return ScoreBatch(
            sport=batch.sport,
            provider_sport_key=batch.provider_sport_key,
            collected_at=batch.collected_at,
            scores=batch.scores,
            raw_payload=batch.raw_payload,
            warnings=batch.warnings,
            metadata=metadata,
        )
