"""Automatic, fail-closed MMA final-result collection from API-Sports."""

from __future__ import annotations

from datetime import datetime, timezone
import re
import unicodedata
from typing import Any

from .http import HttpTransport
from .multisport_structured_results import StructuredFinalResult


BASE_URL = "https://v1.mma.api-sports.io"
PROVIDER_KEY = "api_sports_mma"
UTC = timezone.utc
TERMINAL_SHORT = {"FT", "AET", "CANC", "CANCELLED", "NC", "WO"}


def _name(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("full_name") or value.get("fullName")
    return str(value or "").strip()


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _name(value))
    text = "".join(character for character in text if not unicodedata.combining(character))
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _status_text(fight: dict[str, Any]) -> str:
    status = fight.get("status")
    values: list[Any] = []
    if isinstance(status, dict):
        values.extend((status.get("long"), status.get("short"), status.get("description")))
    else:
        values.append(status)
    results = fight.get("results")
    if isinstance(results, dict):
        values.extend((results.get("won_by"), results.get("result"), results.get("method")))
    return " ".join(str(value) for value in values if value).casefold()


def parse_api_sports_fight(
    fight: dict[str, Any],
    *,
    local_home_name: str,
    local_away_name: str,
    collected_at: datetime,
) -> StructuredFinalResult | None:
    """Accept one terminal fight only when both fighters and the outcome are explicit."""
    fighters = fight.get("fighters")
    if not isinstance(fighters, dict):
        return None
    first = fighters.get("first") or fighters.get("home")
    second = fighters.get("second") or fighters.get("away")
    provider_pair = (_normalized(first), _normalized(second))
    local_pair = (_normalized(local_home_name), _normalized(local_away_name))
    if not all((*provider_pair, *local_pair)):
        return None
    if provider_pair == local_pair:
        reversed_orientation = False
    elif provider_pair == local_pair[::-1]:
        reversed_orientation = True
    else:
        return None

    status = fight.get("status")
    short = str(status.get("short") or "").upper() if isinstance(status, dict) else ""
    text = _status_text(fight)
    terminal = short in TERMINAL_SHORT or any(
        phrase in text
        for phrase in ("finished", "final", "cancelled", "canceled", "no contest", "draw")
    )
    if not terminal:
        return None

    if "cancelled" in text or "canceled" in text or short in {"CANC", "CANCELLED"}:
        result_status, provider_winner = "cancelled", "void"
    elif "no contest" in text or "no-contest" in text or short == "NC":
        result_status, provider_winner = "no_contest", "void"
    elif re.search(r"\bdraw\b", text):
        result_status, provider_winner = "final", "draw"
    else:
        results = fight.get("results")
        winner = results.get("winner") if isinstance(results, dict) else None
        winner_name = _normalized(winner)
        if not winner_name:
            winning_fighters = [
                index
                for index, fighter in enumerate((first, second))
                if isinstance(fighter, dict) and fighter.get("winner") is True
            ]
            if len(winning_fighters) != 1:
                return None
            provider_winner = "home" if winning_fighters[0] == 0 else "away"
        elif winner_name == provider_pair[0]:
            provider_winner = "home"
        elif winner_name == provider_pair[1]:
            provider_winner = "away"
        else:
            return None
        result_status = "final"

    if provider_winner in {"void", "draw"}:
        winner_role = provider_winner
    elif reversed_orientation:
        winner_role = "away" if provider_winner == "home" else "home"
    else:
        winner_role = provider_winner
    raw = dict(fight)
    raw["provider_fight_id"] = str(fight.get("id") or "")
    return StructuredFinalResult(
        result_status=result_status,
        home_score=None,
        away_score=None,
        winner_role=winner_role,
        source_updated_at=collected_at.astimezone(UTC),
        collected_at=collected_at.astimezone(UTC),
        raw_result=raw,
    )


class ApiSportsMmaResultsCollector:
    provider_key = PROVIDER_KEY

    def __init__(self, *, api_key: str, transport: HttpTransport):
        if not api_key.strip():
            raise ValueError("A non-empty API-Sports key is required")
        self._api_key = api_key
        self._transport = transport
        self._cache: dict[str, tuple[dict[str, Any], ...]] = {}
        self._received: dict[str, datetime] = {}
        self._errors: dict[str, str] = {}

    def _fights(self, date_key: str) -> tuple[dict[str, Any], ...]:
        if date_key in self._cache:
            return self._cache[date_key]
        if date_key in self._errors:
            raise RuntimeError(self._errors[date_key])
        try:
            response = self._transport.get(
                f"{BASE_URL}/fights",
                headers={"x-apisports-key": self._api_key},
                params={"date": date_key},
            )
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("API-Sports MMA response must be an object")
            errors = payload.get("errors")
            if errors:
                raise ValueError(f"API-Sports MMA rejected the request: {errors}")
            fights = payload.get("response")
            if not isinstance(fights, list):
                raise ValueError("API-Sports MMA response must contain a response list")
        except Exception as exc:
            self._errors[date_key] = str(exc)
            raise
        self._cache[date_key] = tuple(row for row in fights if isinstance(row, dict))
        self._received[date_key] = response.received_at
        return self._cache[date_key]

    def collect_event(
        self,
        *,
        local_home_name: str,
        local_away_name: str,
        commence_time: datetime,
    ) -> StructuredFinalResult | None:
        if commence_time.tzinfo is None or commence_time.utcoffset() is None:
            raise ValueError("commence_time must include a time zone")
        date_key = commence_time.astimezone(UTC).date().isoformat()
        matches = [
            result
            for fight in self._fights(date_key)
            if (
                result := parse_api_sports_fight(
                    fight,
                    local_home_name=local_home_name,
                    local_away_name=local_away_name,
                    collected_at=self._received[date_key],
                )
            )
            is not None
        ]
        if len(matches) > 1:
            raise ValueError("Multiple API-Sports fights matched the same local participants")
        return matches[0] if matches else None
