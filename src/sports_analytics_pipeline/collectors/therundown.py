"""NFL pregame full-game spread collection from TheRundown API v2."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from sports_analytics_pipeline.calculations import CalculationError, american_to_decimal

from .base import CollectedSpreadQuote, CollectionBatch, TeamResolver, ensure_two_sides
from .http import HttpTransport
from .the_odds_api import parse_timestamp


PROVIDER_KEY = "therundown"
BASE_URL = "https://therundown.io/api/v2"
SUPPORTED_SPORT_IDS = {2, 25, 26}
SPREAD_MARKET_ID = 2
FULL_GAME_PERIOD_ID = 0
AFFILIATES = {
    "19": ("draftkings", "DraftKings"),
    "22": ("betmgm", "BetMGM"),
    "23": ("fanduel", "FanDuel"),
}


def _header_int(headers: Mapping[str, str], name: str) -> int | None:
    value = headers.get(name.casefold())
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_teams(event: dict[str, Any], resolver: TeamResolver) -> tuple[str, str]:
    teams = event.get("teams")
    if (
        not isinstance(teams, list)
        or len(teams) != 2
        or not all(isinstance(team, dict) for team in teams)
    ):
        raise ValueError("TheRundown event must contain exactly two teams")
    home = [team for team in teams if team.get("is_home") is True]
    away = [team for team in teams if team.get("is_away") is True]
    if len(home) != 1 or len(away) != 1:
        raise ValueError("TheRundown event must identify one home and one away team")
    home_id = resolver.resolve(str(home[0]["name"]))
    away_id = resolver.resolve(str(away[0]["name"]))
    if home_id == away_id:
        raise ValueError("TheRundown event resolves both sides to one team")
    return home_id, away_id


def parse_spreads(
    payload: dict[str, Any],
    resolver: TeamResolver,
    *,
    expected_sport_id: int,
    collected_at: datetime,
) -> CollectionBatch:
    if expected_sport_id not in SUPPORTED_SPORT_IDS:
        raise ValueError("Unsupported TheRundown NFL sport ID")
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must include a time zone")
    collected = collected_at.astimezone(timezone.utc)
    events = payload.get("events")
    if not isinstance(events, list):
        raise ValueError("TheRundown response is missing its events list")

    normalized: list[CollectedSpreadQuote] = []
    warnings: list[str] = []
    for event in events:
        source_event_id = str(event.get("event_id") or "")
        if not source_event_id:
            warnings.append("Skipped TheRundown event with no event_id")
            continue
        if event.get("sport_id") != expected_sport_id:
            warnings.append(f"Skipped event {source_event_id} with an unexpected sport ID")
            continue
        score = event.get("score") or {}
        if score.get("event_status") != "STATUS_SCHEDULED":
            warnings.append(f"Skipped non-pregame event {source_event_id}")
            continue

        home_team_id, away_team_id = _event_teams(event, resolver)
        commence_time = parse_timestamp(str(event.get("event_date") or ""))
        quotes_by_book: dict[str, list[CollectedSpreadQuote]] = {}

        for market in event.get("markets") or []:
            if market.get("market_id") != SPREAD_MARKET_ID:
                continue
            if market.get("period_id") != FULL_GAME_PERIOD_ID:
                warnings.append(f"Skipped non-full-game spread for event {source_event_id}")
                continue
            for participant in market.get("participants") or []:
                if participant.get("type") != "TYPE_TEAM":
                    warnings.append(f"Skipped non-team spread participant for event {source_event_id}")
                    continue
                outcome_team_id = resolver.resolve(str(participant.get("name") or ""))
                if outcome_team_id not in {home_team_id, away_team_id}:
                    raise ValueError(
                        f"Event {source_event_id} contains an unrelated spread participant"
                    )
                for line in participant.get("lines") or []:
                    try:
                        points = Decimal(str(line["value"]))
                    except (KeyError, ValueError, InvalidOperation):
                        warnings.append(f"Skipped malformed spread line for event {source_event_id}")
                        continue
                    if not points.is_finite() or abs(points) > Decimal("40"):
                        warnings.append(f"Skipped out-of-range spread for event {source_event_id}")
                        continue
                    prices = line.get("prices") or {}
                    if not isinstance(prices, dict):
                        warnings.append(f"Skipped malformed price map for event {source_event_id}")
                        continue
                    for affiliate_id, price_object in prices.items():
                        affiliate = AFFILIATES.get(str(affiliate_id))
                        if not affiliate:
                            continue
                        if not isinstance(price_object, dict):
                            warnings.append(f"Skipped malformed sportsbook price for event {source_event_id}")
                            continue
                        if price_object.get("is_main_line") is not True:
                            warnings.append(f"Skipped alternate spread for event {source_event_id}")
                            continue
                        if price_object.get("closed_at"):
                            warnings.append(f"Skipped closed spread for event {source_event_id}")
                            continue
                        try:
                            raw_price = Decimal(str(price_object.get("price")))
                            decimal_odds = american_to_decimal(raw_price)
                        except (InvalidOperation, CalculationError):
                            warnings.append(
                                f"Skipped malformed sportsbook price for event {source_event_id}"
                            )
                            continue
                        if raw_price == Decimal("0.0001"):
                            warnings.append(f"Skipped unavailable spread for event {source_event_id}")
                            continue
                        source_updated = parse_timestamp(
                            str(price_object.get("updated_at") or "")
                        )
                        if source_updated > collected + timedelta(minutes=5):
                            raise ValueError(
                                "Source update time cannot materially follow collection time"
                            )
                        book_key, book_title = affiliate
                        quotes_by_book.setdefault(book_key, []).append(
                            CollectedSpreadQuote(
                                source_event_id=source_event_id,
                                bookmaker_key=book_key,
                                bookmaker_title=book_title,
                                home_team_id=home_team_id,
                                away_team_id=away_team_id,
                                commence_time=commence_time,
                                market_key="spreads",
                                period="full_game",
                                settlement_rule="includes_overtime_push_returns_stake",
                                outcome_team_id=outcome_team_id,
                                points=points,
                                decimal_odds=decimal_odds,
                                source_updated_at=source_updated,
                                collected_at=collected,
                                status="open",
                            )
                        )

        for book_key, book_quotes in quotes_by_book.items():
            try:
                ensure_two_sides(book_quotes)
                first, second = book_quotes
                if first.outcome_team_id == second.outcome_team_id:
                    raise ValueError("Spread market repeats one team")
                if first.points != -second.points:
                    raise ValueError("Spread market does not contain opposite points")
            except ValueError as exc:
                warnings.append(
                    f"Skipped incomplete {book_key} spread for event {source_event_id}: {exc}"
                )
                continue
            normalized.extend(book_quotes)

    return CollectionBatch(
        provider_key=PROVIDER_KEY,
        collected_at=collected,
        raw_payload=payload,
        quotes=tuple(normalized),
        warnings=tuple(warnings),
    )


class TheRundownCollector:
    """Read-only date collector using protected header authentication."""

    provider_key = PROVIDER_KEY

    def __init__(
        self,
        *,
        api_key: str,
        resolver: TeamResolver,
        transport: HttpTransport,
        event_date: date,
        sport_id: int,
    ):
        if not api_key.strip():
            raise ValueError("A non-empty TheRundown API key is required")
        if sport_id not in SUPPORTED_SPORT_IDS:
            raise ValueError("Unsupported TheRundown NFL sport ID")
        self._api_key = api_key
        self._resolver = resolver
        self._transport = transport
        self._event_date = event_date
        self._sport_id = sport_id

    def collect_current_spreads(self) -> CollectionBatch:
        response = self._transport.get(
            f"{BASE_URL}/sports/{self._sport_id}/events/{self._event_date.isoformat()}",
            params={
                "market_ids": str(SPREAD_MARKET_ID),
                "affiliate_ids": ",".join(AFFILIATES),
                "main_line": "true",
                "hide_closed": "true",
                "offset": "300",
            },
            headers={"X-TheRundown-Key": self._api_key},
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("TheRundown response must be an object")
        batch = parse_spreads(
            payload,
            self._resolver,
            expected_sport_id=self._sport_id,
            collected_at=response.received_at,
        )
        metadata = {
            "endpoint": "current_odds",
            "requested_date": self._event_date.isoformat(),
            "sport_id": self._sport_id,
            "datapoints_remaining": _header_int(
                response.headers, "x-datapoints-remaining"
            ),
            "datapoints_used": _header_int(response.headers, "x-datapoints-used"),
            "datapoints_limit": _header_int(response.headers, "x-datapoints-limit"),
            "datapoints_cost": _header_int(response.headers, "x-datapoints"),
        }
        metadata.update(
            requests_remaining=metadata["datapoints_remaining"],
            requests_used=metadata["datapoints_used"],
            request_cost=metadata["datapoints_cost"],
        )
        return CollectionBatch(
            provider_key=batch.provider_key,
            collected_at=batch.collected_at,
            raw_payload=batch.raw_payload,
            quotes=batch.quotes,
            warnings=batch.warnings,
            metadata=metadata,
        )
