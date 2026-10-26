"""Common interfaces and normalized quote records for read-only collectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, Sequence


@dataclass(frozen=True)
class CollectedSpreadQuote:
    source_event_id: str
    bookmaker_key: str
    bookmaker_title: str
    home_team_id: str
    away_team_id: str
    commence_time: datetime
    market_key: str
    period: str
    settlement_rule: str
    outcome_team_id: str
    points: Decimal
    decimal_odds: Decimal
    source_updated_at: datetime
    collected_at: datetime
    status: str
    sport: str = "nfl"


@dataclass(frozen=True)
class CollectedMoneylineQuote:
    source_event_id: str
    bookmaker_key: str
    bookmaker_title: str
    home_team_id: str
    away_team_id: str
    commence_time: datetime
    market_key: str
    period: str
    settlement_rule: str
    outcome_team_id: str
    decimal_odds: Decimal
    source_updated_at: datetime
    collected_at: datetime
    status: str
    sport: str = "mlb"
    points: None = None


NormalizedQuote = CollectedSpreadQuote | CollectedMoneylineQuote


@dataclass(frozen=True)
class CollectionBatch:
    provider_key: str
    collected_at: datetime
    raw_payload: Any
    quotes: tuple[NormalizedQuote, ...]
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class OddsCollector(Protocol):
    provider_key: str


class TeamResolver(Protocol):
    def resolve(self, source_name: str) -> str:
        """Return a controlled internal team ID or raise for an unknown name."""


class MappingTeamResolver:
    def __init__(self, aliases: dict[str, str]):
        self._aliases = {self._normalize(name): team_id for name, team_id in aliases.items()}

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.casefold().strip().split())

    def resolve(self, source_name: str) -> str:
        key = self._normalize(source_name)
        try:
            return self._aliases[key]
        except KeyError as exc:
            raise ValueError(f"Unknown team alias: {source_name!r}") from exc


def ensure_two_sides(quotes: Sequence[NormalizedQuote]) -> None:
    if len(quotes) != 2:
        raise ValueError("A two-sided market must contain exactly two team outcomes")
