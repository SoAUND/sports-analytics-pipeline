"""Exact spread matching and prediction-time safety checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .calculations import CalculationError, as_decimal


@dataclass(frozen=True)
class SpreadQuote:
    event_id: str
    bookmaker_key: str
    market_key: str
    period: str
    settlement_rule: str
    team_id: str
    opponent_team_id: str
    points: Decimal
    decimal_odds: Decimal
    source_updated_at: datetime
    collected_at: datetime
    status: str = "open"


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CalculationError(f"{field} must include a time zone")
    return value.astimezone(timezone.utc)


def validate_quote(quote: SpreadQuote) -> None:
    if quote.market_key != "spreads" or quote.period != "full_game":
        raise CalculationError("Only full-game spread quotes are valid in version one")
    if quote.team_id == quote.opponent_team_id:
        raise CalculationError("A team cannot be its own opponent")
    if as_decimal(quote.decimal_odds) <= Decimal("1"):
        raise CalculationError("Decimal odds must be greater than 1")
    if abs(as_decimal(quote.points)) > Decimal("40"):
        raise CalculationError("Spread point is outside the approved plausibility range")
    if quote.status not in {"open", "suspended", "stale", "unavailable"}:
        raise CalculationError("Unknown market status")
    source_time = _aware(quote.source_updated_at, "source_updated_at")
    collected_time = _aware(quote.collected_at, "collected_at")
    if source_time > collected_time + timedelta(minutes=5):
        raise CalculationError("Source update time cannot materially follow collection time")


def match_two_sided_spread(first: SpreadQuote, second: SpreadQuote) -> tuple[SpreadQuote, SpreadQuote]:
    validate_quote(first)
    validate_quote(second)
    identity_fields = (
        "event_id",
        "bookmaker_key",
        "market_key",
        "period",
        "settlement_rule",
    )
    for field in identity_fields:
        if getattr(first, field) != getattr(second, field):
            raise CalculationError(f"Spread sides do not match on {field}")
    if first.team_id != second.opponent_team_id or second.team_id != first.opponent_team_id:
        raise CalculationError("Spread sides do not contain opposite teams")
    if as_decimal(first.points) != -as_decimal(second.points):
        raise CalculationError("Spread sides do not use opposite point values")
    if first.status != "open" or second.status != "open":
        raise CalculationError("Both spread sides must be open")
    return first, second


def quote_is_fresh(
    quote: SpreadQuote,
    decision_time: datetime,
    stale_after: timedelta = timedelta(minutes=60),
) -> bool:
    validate_quote(quote)
    decision = _aware(decision_time, "decision_time")
    collected = _aware(quote.collected_at, "collected_at")
    source = _aware(quote.source_updated_at, "source_updated_at")
    if collected > decision or source > decision:
        return False
    return decision - max(collected, source) <= stale_after


def record_existed_by(record_time: datetime, prediction_time: datetime) -> bool:
    return _aware(record_time, "record_time") <= _aware(
        prediction_time, "prediction_time"
    )

@dataclass(frozen=True)
class MoneylineQuote:
    event_id: str
    snapshot_id: str
    bookmaker_key: str
    market_key: str
    period: str
    settlement_rule: str
    team_id: str
    opponent_team_id: str
    decimal_odds: Decimal
    source_updated_at: datetime
    collected_at: datetime
    status: str = "open"


def validate_moneyline_quote(quote: MoneylineQuote) -> None:
    if quote.market_key != "moneylines" or quote.period != "full_game":
        raise CalculationError("Only full-game moneyline quotes are valid for MLB Phase 1")
    if not quote.snapshot_id:
        raise CalculationError("Moneyline quote requires a snapshot identity")
    if quote.team_id == quote.opponent_team_id:
        raise CalculationError("A team cannot be its own opponent")
    if as_decimal(quote.decimal_odds) <= Decimal("1"):
        raise CalculationError("Decimal odds must be greater than 1")
    if quote.status not in {"open", "suspended", "stale", "unavailable"}:
        raise CalculationError("Unknown market status")
    source_time = _aware(quote.source_updated_at, "source_updated_at")
    collected_time = _aware(quote.collected_at, "collected_at")
    if source_time > collected_time + timedelta(minutes=5):
        raise CalculationError("Source update time cannot materially follow collection time")


def match_two_sided_moneyline(
    first: MoneylineQuote, second: MoneylineQuote
) -> tuple[MoneylineQuote, MoneylineQuote]:
    """Require the two MLB prices to be the exact same sportsbook market snapshot."""
    validate_moneyline_quote(first)
    validate_moneyline_quote(second)
    identity_fields = (
        "event_id",
        "snapshot_id",
        "bookmaker_key",
        "market_key",
        "period",
        "settlement_rule",
        "source_updated_at",
        "collected_at",
    )
    for field in identity_fields:
        if getattr(first, field) != getattr(second, field):
            raise CalculationError(f"Moneyline sides do not match on {field}")
    if first.team_id != second.opponent_team_id or second.team_id != first.opponent_team_id:
        raise CalculationError("Moneyline sides do not contain opposite teams")
    if first.status != "open" or second.status != "open":
        raise CalculationError("Both moneyline sides must be open")
    return first, second


def moneyline_quote_is_fresh(
    quote: MoneylineQuote,
    decision_time: datetime,
    stale_after: timedelta = timedelta(minutes=60),
) -> bool:
    validate_moneyline_quote(quote)
    decision = _aware(decision_time, "decision_time")
    collected = _aware(quote.collected_at, "collected_at")
    source = _aware(quote.source_updated_at, "source_updated_at")
    if collected > decision or source > decision:
        return False
    return decision - max(collected, source) <= stale_after
