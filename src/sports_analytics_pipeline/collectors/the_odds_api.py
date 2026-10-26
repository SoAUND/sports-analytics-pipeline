"""Current and historical NFL spread collection from The Odds API v4."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .base import (
    CollectedMoneylineQuote,
    CollectedSpreadQuote,
    CollectionBatch,
    TeamResolver,
    ensure_two_sides,
)
from .http import HttpTransport


PROVIDER_KEY = "the_odds_api"
SPORT_KEY = "americanfootball_nfl"
PRESEASON_SPORT_KEY = "americanfootball_nfl_preseason"
MLB_SPORT_KEY = "baseball_mlb"
ALLOWED_SPORT_KEYS = {SPORT_KEY, PRESEASON_SPORT_KEY, MLB_SPORT_KEY}
MARKET_KEY = "spreads"
MLB_MARKET_KEY = "h2h"
BASE_URL = "https://api.the-odds-api.com/v4"


def parse_timestamp(value: str) -> datetime:
    if not value:
        raise ValueError("Missing required timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a time zone")
    return parsed.astimezone(timezone.utc)


def parse_current_spreads(
    payload: list[dict[str, Any]],
    resolver: TeamResolver,
    collected_at: datetime | None = None,
    *,
    sport_key: str = SPORT_KEY,
) -> CollectionBatch:
    if sport_key not in ALLOWED_SPORT_KEYS:
        raise ValueError("Unsupported Odds API sport key")
    collected = collected_at or datetime.now(timezone.utc)
    if collected.tzinfo is None or collected.utcoffset() is None:
        raise ValueError("collected_at must include a time zone")
    collected = collected.astimezone(timezone.utc)
    normalized: list[CollectedSpreadQuote] = []
    warnings: list[str] = []

    for event in payload:
        if event.get("sport_key") != sport_key:
            warnings.append(
                f"Skipped event {event.get('id', '<missing>')} from a different NFL feed"
            )
            continue
        source_event_id = str(event["id"])
        home_team_id = resolver.resolve(str(event["home_team"]))
        away_team_id = resolver.resolve(str(event["away_team"]))
        if home_team_id == away_team_id:
            raise ValueError(f"Event {source_event_id} resolves both sides to one team")
        commence_time = parse_timestamp(str(event["commence_time"]))

        for bookmaker in event.get("bookmakers", []):
            book_key = str(bookmaker["key"])
            book_title = str(bookmaker.get("title", book_key))
            book_updated = parse_timestamp(str(bookmaker["last_update"]))
            for market in bookmaker.get("markets", []):
                if market.get("key") != MARKET_KEY:
                    continue
                source_updated = parse_timestamp(
                    str(market.get("last_update") or bookmaker["last_update"])
                )
                effective_update = max(source_updated, book_updated)
                if effective_update > collected + timedelta(minutes=5):
                    raise ValueError("Source update time cannot materially follow collection time")
                market_quotes: list[CollectedSpreadQuote] = []
                for outcome in market.get("outcomes", []):
                    outcome_team_id = resolver.resolve(str(outcome["name"]))
                    if outcome_team_id not in {home_team_id, away_team_id}:
                        raise ValueError(
                            f"Event {source_event_id} contains an unrelated spread outcome"
                        )
                    price = Decimal(str(outcome["price"]))
                    points = Decimal(str(outcome["point"]))
                    if price <= Decimal("1"):
                        raise ValueError("Decimal spread price must be greater than 1")
                    if abs(points) > Decimal("40"):
                        raise ValueError("Spread point is outside the approved range")
                    market_quotes.append(
                        CollectedSpreadQuote(
                            source_event_id=source_event_id,
                            bookmaker_key=book_key,
                            bookmaker_title=book_title,
                            home_team_id=home_team_id,
                            away_team_id=away_team_id,
                            commence_time=commence_time,
                            market_key=MARKET_KEY,
                            period="full_game",
                            settlement_rule="includes_overtime_push_returns_stake",
                            outcome_team_id=outcome_team_id,
                            points=points,
                            decimal_odds=price,
                            source_updated_at=effective_update,
                            collected_at=collected,
                            status="open",
                        )
                    )
                ensure_two_sides(market_quotes)
                first, second = market_quotes
                if first.outcome_team_id == second.outcome_team_id:
                    raise ValueError("Spread market repeats one team")
                if first.points != -second.points:
                    raise ValueError("Spread market does not contain opposite points")
                normalized.extend(market_quotes)

    return CollectionBatch(
        provider_key=PROVIDER_KEY,
        collected_at=collected,
        raw_payload=payload,
        quotes=tuple(normalized),
        warnings=tuple(warnings),
    )



def parse_current_mlb_moneylines(
    payload: list[dict[str, Any]],
    resolver: TeamResolver,
    collected_at: datetime | None = None,
) -> CollectionBatch:
    """Normalize current MLB full-game h2h prices into shared moneyline quotes."""
    collected = collected_at or datetime.now(timezone.utc)
    if collected.tzinfo is None or collected.utcoffset() is None:
        raise ValueError("collected_at must include a time zone")
    collected = collected.astimezone(timezone.utc)
    normalized: list[CollectedMoneylineQuote] = []
    warnings: list[str] = []

    for event in payload:
        if event.get("sport_key") != MLB_SPORT_KEY:
            warnings.append(
                f"Skipped event {event.get('id', '<missing>')} from a different sport feed"
            )
            continue
        source_event_id = str(event["id"])
        home_team_id = resolver.resolve(str(event["home_team"]))
        away_team_id = resolver.resolve(str(event["away_team"]))
        if home_team_id == away_team_id:
            raise ValueError(f"Event {source_event_id} resolves both sides to one MLB team")
        commence_time = parse_timestamp(str(event["commence_time"]))

        for bookmaker in event.get("bookmakers", []):
            book_key = str(bookmaker["key"])
            book_title = str(bookmaker.get("title", book_key))
            book_updated = parse_timestamp(str(bookmaker["last_update"]))
            for market in bookmaker.get("markets", []):
                if market.get("key") != MLB_MARKET_KEY:
                    continue
                source_updated = parse_timestamp(
                    str(market.get("last_update") or bookmaker["last_update"])
                )
                effective_update = max(source_updated, book_updated)
                if effective_update > collected + timedelta(minutes=5):
                    raise ValueError("Source update time cannot materially follow collection time")
                market_quotes: list[CollectedMoneylineQuote] = []
                for outcome in market.get("outcomes", []):
                    outcome_team_id = resolver.resolve(str(outcome["name"]))
                    if outcome_team_id not in {home_team_id, away_team_id}:
                        raise ValueError(
                            f"Event {source_event_id} contains an unrelated moneyline outcome"
                        )
                    price = Decimal(str(outcome["price"]))
                    if price <= Decimal("1") or price > Decimal("100"):
                        raise ValueError("Decimal moneyline price is outside the approved range")
                    market_quotes.append(
                        CollectedMoneylineQuote(
                            source_event_id=source_event_id,
                            bookmaker_key=book_key,
                            bookmaker_title=book_title,
                            home_team_id=home_team_id,
                            away_team_id=away_team_id,
                            commence_time=commence_time,
                            market_key="moneylines",
                            period="full_game",
                            settlement_rule="full_game_including_extra_innings",
                            outcome_team_id=outcome_team_id,
                            decimal_odds=price,
                            source_updated_at=effective_update,
                            collected_at=collected,
                            status="open",
                        )
                    )
                ensure_two_sides(market_quotes)
                first, second = market_quotes
                if first.outcome_team_id == second.outcome_team_id:
                    raise ValueError("Moneyline market repeats one team")
                normalized.extend(market_quotes)

    return CollectionBatch(
        provider_key=PROVIDER_KEY,
        collected_at=collected,
        raw_payload=payload,
        quotes=tuple(normalized),
        warnings=tuple(warnings),
    )

def _header_int(headers: dict[str, str] | Any, name: str) -> int | None:
    value = headers.get(name.casefold())
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class TheOddsApiCollector:
    """Read-only collector with no secret values in returned metadata."""

    provider_key = PROVIDER_KEY

    def __init__(
        self,
        *,
        api_key: str,
        resolver: TeamResolver,
        transport: HttpTransport,
        bookmakers: tuple[str, ...] = (),
        regions: tuple[str, ...] = ("us",),
        sport_key: str = SPORT_KEY,
    ):
        if not api_key.strip():
            raise ValueError("A non-empty Odds API key is required")
        if not bookmakers and not regions:
            raise ValueError("At least one bookmaker or region must be configured")
        if sport_key not in ALLOWED_SPORT_KEYS:
            raise ValueError("Unsupported Odds API sport key")
        self._api_key = api_key
        self._resolver = resolver
        self._transport = transport
        self._bookmakers = tuple(dict.fromkeys(bookmakers))
        self._regions = tuple(dict.fromkeys(regions))
        self._sport_key = sport_key

    @property
    def endpoint_name(self) -> str:
        if self._sport_key == PRESEASON_SPORT_KEY:
            return "current_preseason_odds"
        if self._sport_key == MLB_SPORT_KEY:
            return "current_mlb_odds"
        return "current_odds"

    def _base_params(self) -> dict[str, str]:
        params = {
            "apiKey": self._api_key,
            "markets": MLB_MARKET_KEY if self._sport_key == MLB_SPORT_KEY else MARKET_KEY,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        if self._bookmakers:
            params["bookmakers"] = ",".join(self._bookmakers)
        else:
            params["regions"] = ",".join(self._regions)
        return params

    @staticmethod
    def _metadata(headers: Any, **extra: Any) -> dict[str, Any]:
        return {
            "requests_remaining": _header_int(headers, "x-requests-remaining"),
            "requests_used": _header_int(headers, "x-requests-used"),
            "request_cost": _header_int(headers, "x-requests-last"),
            **extra,
        }

    def collect_current_spreads(self) -> CollectionBatch:
        response = self._transport.get(
            f"{BASE_URL}/sports/{self._sport_key}/odds/",
            params=self._base_params(),
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Current odds response must be a list of events")
        if self._sport_key == MLB_SPORT_KEY:
            batch = parse_current_mlb_moneylines(payload, self._resolver, response.received_at)
        else:
            batch = parse_current_spreads(
                payload,
                self._resolver,
                response.received_at,
                sport_key=self._sport_key,
            )
        return CollectionBatch(
            provider_key=batch.provider_key,
            collected_at=batch.collected_at,
            raw_payload=batch.raw_payload,
            quotes=batch.quotes,
            warnings=batch.warnings,
            metadata=self._metadata(response.headers, endpoint=self.endpoint_name),
        )

    def collect_historical_spreads(self, requested_at: datetime) -> CollectionBatch:
        if requested_at.tzinfo is None or requested_at.utcoffset() is None:
            raise ValueError("requested_at must include a time zone")
        requested_utc = requested_at.astimezone(timezone.utc)
        if requested_utc > datetime.now(timezone.utc):
            raise ValueError("Historical odds cannot be requested from the future")
        params = self._base_params()
        params["date"] = requested_utc.isoformat().replace("+00:00", "Z")
        response = self._transport.get(
            f"{BASE_URL}/historical/sports/{self._sport_key}/odds/",
            params=params,
        )
        wrapper = response.json()
        if not isinstance(wrapper, dict) or not isinstance(wrapper.get("data"), list):
            raise ValueError("Historical odds response is missing its data list")
        snapshot_at = parse_timestamp(str(wrapper["timestamp"]))
        if snapshot_at > requested_utc:
            raise ValueError("Historical source returned a snapshot after the requested time")
        batch = parse_current_spreads(
            wrapper["data"],
            self._resolver,
            response.received_at,
            sport_key=self._sport_key,
        )
        return CollectionBatch(
            provider_key=batch.provider_key,
            collected_at=batch.collected_at,
            raw_payload=wrapper,
            quotes=batch.quotes,
            warnings=batch.warnings,
            metadata=self._metadata(
                response.headers,
                endpoint="historical_odds",
                requested_at=requested_utc.isoformat(),
                snapshot_at=snapshot_at.isoformat(),
                previous_timestamp=wrapper.get("previous_timestamp"),
                next_timestamp=wrapper.get("next_timestamp"),
            ),
        )
