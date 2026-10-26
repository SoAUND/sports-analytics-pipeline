"""Read-only Odds API collectors for our Phase 2 multi-sport expansion.

Supported first markets:
- NBA/WNBA full-game spreads
- Premier League full-game three-way h2h
- UFC/MMA fight-winner h2h
- ATP/WTA match-winner h2h

The collector deliberately keeps all new sports test-only. It does not place wagers,
approve models, or infer sportsbook settlement rules that have not been verified.

Provider reference: https://the-odds-api.com/liveapi/guides/v4/
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from ..sports_registry import discover_active_provider_keys, get_sport_market
from ..venue_identity import multisport_venue_directory
from .http import HttpTransport


PROVIDER_KEY = "the_odds_api"
BASE_URL = "https://api.the-odds-api.com/v4"
MAX_DECIMAL_ODDS = Decimal("100")
MAX_BASKETBALL_SPREAD = Decimal("100")
PENDING_SETTLEMENT_RULE = "pending_sportsbook_rule_verification"


class InvalidOddsPriceError(ValueError):
    """One bookmaker market supplied a price outside our approved decimal contract."""


def parse_timestamp(value: str) -> datetime:
    if not value:
        raise ValueError("Missing required timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a time zone")
    return parsed.astimezone(timezone.utc)


def _header_int(headers: Mapping[str, str], name: str) -> int | None:
    value = headers.get(name.casefold())
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normal_name(value: str) -> str:
    return " ".join(value.casefold().strip().split())


@dataclass(frozen=True)
class MultiSportParticipant:
    sport: str
    provider_sport_key: str
    source_participant_id: str
    full_name: str
    participant_type: str


@dataclass(frozen=True)
class MultiSportEvent:
    sport: str
    provider_sport_key: str
    source_event_id: str
    commence_time: datetime
    home_participant_id: str
    away_participant_id: str
    home_name: str
    away_name: str
    venue_id: str | None = None
    venue_name: str | None = None
    source_venue_id: str | None = None


@dataclass(frozen=True)
class MultiSportQuote:
    sport: str
    provider_sport_key: str
    source_event_id: str
    bookmaker_key: str
    bookmaker_title: str
    market_key: str
    provider_market_key: str
    period: str
    settlement_rule: str
    home_participant_id: str
    away_participant_id: str
    outcome_participant_id: str | None
    outcome_name: str
    outcome_role: str
    points: Decimal | None
    decimal_odds: Decimal
    source_updated_at: datetime
    collected_at: datetime
    status: str


@dataclass(frozen=True)
class MultiSportCollectionBatch:
    provider_key: str
    sport: str
    provider_sport_key: str
    collected_at: datetime
    raw_payload: Any
    participants: tuple[MultiSportParticipant, ...] = ()
    events: tuple[MultiSportEvent, ...] = ()
    quotes: tuple[MultiSportQuote, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SportsCatalogBatch:
    provider_key: str
    collected_at: datetime
    raw_payload: list[dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SportCollectionBundle:
    """One complete test-only collection cycle for a controlled sport."""

    sport: str
    active_provider_keys: tuple[str, ...]
    participant_batches: tuple[MultiSportCollectionBatch, ...]
    event_batches: tuple[MultiSportCollectionBatch, ...]
    odds_batches: tuple[MultiSportCollectionBatch, ...]
    warnings: tuple[str, ...] = ()


class ParticipantDirectory:
    """Exact, provider-backed participant whitelist for one sport/tournament key."""

    def __init__(self, participants: tuple[MultiSportParticipant, ...]):
        if not participants:
            raise ValueError("Participant directory cannot be empty")
        provider_keys = {row.provider_sport_key for row in participants}
        sports = {row.sport for row in participants}
        if len(provider_keys) != 1 or len(sports) != 1:
            raise ValueError("Participant directory must contain one sport/provider key")
        self.sport = next(iter(sports))
        self.provider_sport_key = next(iter(provider_keys))
        self._by_name: dict[str, MultiSportParticipant] = {}
        for participant in participants:
            key = _normal_name(participant.full_name)
            previous = self._by_name.get(key)
            if previous and previous.source_participant_id != participant.source_participant_id:
                raise ValueError(f"Ambiguous participant name: {participant.full_name!r}")
            self._by_name[key] = participant

    def resolve(self, full_name: str) -> MultiSportParticipant:
        try:
            return self._by_name[_normal_name(full_name)]
        except KeyError as exc:
            raise ValueError(f"Unknown provider participant: {full_name!r}") from exc


def _validate_provider_key(sport: str, provider_sport_key: str) -> None:
    spec = get_sport_market(sport)
    if not spec.matches_provider_sport_key(provider_sport_key):
        raise ValueError(
            f"Provider key {provider_sport_key!r} does not belong to controlled sport {sport!r}"
        )


def parse_participants(
    payload: list[dict[str, Any]],
    *,
    sport: str,
    provider_sport_key: str,
) -> tuple[MultiSportParticipant, ...]:
    _validate_provider_key(sport, provider_sport_key)
    participant_type = "team" if sport in {"nba", "wnba", "epl"} else "individual"
    normalized: list[MultiSportParticipant] = []
    seen_ids: set[str] = set()
    seen_names: dict[str, str] = {}

    for row in payload:
        source_id = str(row.get("id", "")).strip()
        full_name = str(row.get("full_name", "")).strip()
        if not source_id or not full_name:
            raise ValueError("Participant payload contains a missing id or full_name")
        name_key = _normal_name(full_name)
        if source_id in seen_ids:
            raise ValueError(f"Duplicate participant id: {source_id}")
        existing = seen_names.get(name_key)
        if existing and existing != source_id:
            raise ValueError(f"Duplicate participant name with different ids: {full_name}")
        seen_ids.add(source_id)
        seen_names[name_key] = source_id
        normalized.append(
            MultiSportParticipant(
                sport=sport,
                provider_sport_key=provider_sport_key,
                source_participant_id=source_id,
                full_name=full_name,
                participant_type=participant_type,
            )
        )
    return tuple(normalized)


def parse_events(
    payload: list[dict[str, Any]],
    *,
    sport: str,
    provider_sport_key: str,
    participants: ParticipantDirectory,
) -> tuple[MultiSportEvent, ...]:
    _validate_provider_key(sport, provider_sport_key)
    if participants.sport != sport or participants.provider_sport_key != provider_sport_key:
        raise ValueError("Participant directory does not match the requested sport key")
    events: list[MultiSportEvent] = []
    seen: set[str] = set()
    venue_directory = multisport_venue_directory()

    for row in payload:
        if row.get("sport_key") != provider_sport_key:
            raise ValueError("Events response contains a different provider sport key")
        source_event_id = str(row.get("id", "")).strip()
        if not source_event_id or source_event_id in seen:
            raise ValueError("Event payload contains a missing or duplicate event id")
        seen.add(source_event_id)
        home_name = str(row.get("home_team", "")).strip()
        away_name = str(row.get("away_team", "")).strip()
        home = participants.resolve(home_name)
        away = participants.resolve(away_name)
        if home.source_participant_id == away.source_participant_id:
            raise ValueError("Event resolves both sides to the same participant")
        raw_venue = row.get("venue")
        if isinstance(raw_venue, dict):
            source_venue_id = str(raw_venue.get("id") or "").strip() or None
            source_venue_name = str(raw_venue.get("name") or "").strip() or None
        elif raw_venue is None:
            source_venue_id = None
            source_venue_name = None
        else:
            source_venue_id = None
            source_venue_name = str(raw_venue).strip() or None
        venue = venue_directory.resolve_optional(
            sport,
            source_name=source_venue_name,
            source_id=source_venue_id,
            provider_key=PROVIDER_KEY,
            provider_sport_key=provider_sport_key,
        )
        events.append(
            MultiSportEvent(
                sport=sport,
                provider_sport_key=provider_sport_key,
                source_event_id=source_event_id,
                commence_time=parse_timestamp(str(row.get("commence_time", ""))),
                home_participant_id=home.source_participant_id,
                away_participant_id=away.source_participant_id,
                home_name=home.full_name,
                away_name=away.full_name,
                venue_id=venue.venue.venue_id if venue.venue else None,
                venue_name=venue.venue.official_name if venue.venue else source_venue_name,
                source_venue_id=source_venue_id,
            )
        )
    return tuple(events)


def _validate_price(value: Any) -> Decimal:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidOddsPriceError("Decimal odds are not numeric") from exc
    if price <= Decimal("1") or price > MAX_DECIMAL_ODDS:
        raise InvalidOddsPriceError("Decimal odds are outside the approved range")
    return price


def _outcome_role(
    *,
    sport: str,
    outcome_name: str,
    home_name: str,
    away_name: str,
) -> str:
    normalized = _normal_name(outcome_name)
    if normalized == _normal_name(home_name):
        return "home"
    if normalized == _normal_name(away_name):
        return "away"
    if sport == "epl" and normalized == "draw":
        return "draw"
    raise ValueError(f"Unexpected outcome {outcome_name!r} for {sport}")


def parse_current_odds(
    payload: list[dict[str, Any]],
    *,
    sport: str,
    provider_sport_key: str,
    participants: ParticipantDirectory,
    collected_at: datetime,
) -> MultiSportCollectionBatch:
    spec = get_sport_market(sport)
    _validate_provider_key(sport, provider_sport_key)
    if participants.sport != sport or participants.provider_sport_key != provider_sport_key:
        raise ValueError("Participant directory does not match the requested sport key")
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must include a time zone")
    collected = collected_at.astimezone(timezone.utc)
    quotes: list[MultiSportQuote] = []
    warnings: list[str] = []

    for event in payload:
        if event.get("sport_key") != provider_sport_key:
            warnings.append(
                f"Skipped event {event.get('id', '<missing>')} from a different sport key"
            )
            continue
        source_event_id = str(event.get("id", "")).strip()
        if not source_event_id:
            raise ValueError("Odds event is missing its source event id")
        home_name = str(event.get("home_team", "")).strip()
        away_name = str(event.get("away_team", "")).strip()
        home = participants.resolve(home_name)
        away = participants.resolve(away_name)
        if home.source_participant_id == away.source_participant_id:
            raise ValueError("Odds event resolves both sides to one participant")
        parse_timestamp(str(event.get("commence_time", "")))

        for bookmaker in event.get("bookmakers", []):
            book_key = str(bookmaker.get("key", "")).strip()
            if not book_key:
                raise ValueError("Bookmaker is missing its provider key")
            book_title = str(bookmaker.get("title") or book_key)
            book_updated = parse_timestamp(str(bookmaker.get("last_update", "")))
            for market in bookmaker.get("markets", []):
                if market.get("key") != spec.provider_market_key:
                    continue
                source_updated = parse_timestamp(
                    str(market.get("last_update") or bookmaker.get("last_update", ""))
                )
                effective_update = max(book_updated, source_updated)
                if effective_update > collected + timedelta(minutes=5):
                    raise ValueError("Source update time cannot materially follow collection time")
                outcomes = market.get("outcomes", [])
                if len(outcomes) != spec.outcome_count:
                    raise ValueError(
                        f"{spec.display_name} {spec.provider_market_key} market requires "
                        f"exactly {spec.outcome_count} outcomes"
                    )

                market_quotes: list[MultiSportQuote] = []
                roles: set[str] = set()
                spread_points: list[Decimal] = []
                invalid_price = False
                for outcome in outcomes:
                    outcome_name = str(outcome.get("name", "")).strip()
                    role = _outcome_role(
                        sport=sport,
                        outcome_name=outcome_name,
                        home_name=home_name,
                        away_name=away_name,
                    )
                    if role in roles:
                        raise ValueError("Market repeats an outcome role")
                    roles.add(role)
                    outcome_participant_id: str | None
                    if role == "home":
                        outcome_participant_id = home.source_participant_id
                    elif role == "away":
                        outcome_participant_id = away.source_participant_id
                    else:
                        outcome_participant_id = None

                    points: Decimal | None = None
                    if spec.provider_market_key == "spreads":
                        if "point" not in outcome:
                            raise ValueError("Spread outcome is missing its point value")
                        points = Decimal(str(outcome["point"]))
                        if abs(points) > MAX_BASKETBALL_SPREAD:
                            raise ValueError("Basketball spread is outside the approved range")
                        spread_points.append(points)

                    try:
                        decimal_odds = _validate_price(outcome.get("price"))
                    except InvalidOddsPriceError as exc:
                        warnings.append(
                            f"Skipped invalid price market for event {source_event_id}, "
                            f"bookmaker {book_key}, market {spec.provider_market_key}: {exc}"
                        )
                        invalid_price = True
                        break

                    market_quotes.append(
                        MultiSportQuote(
                            sport=sport,
                            provider_sport_key=provider_sport_key,
                            source_event_id=source_event_id,
                            bookmaker_key=book_key,
                            bookmaker_title=book_title,
                            market_key=spec.internal_market_key,
                            provider_market_key=spec.provider_market_key,
                            period=spec.period,
                            settlement_rule=PENDING_SETTLEMENT_RULE,
                            home_participant_id=home.source_participant_id,
                            away_participant_id=away.source_participant_id,
                            outcome_participant_id=outcome_participant_id,
                            outcome_name=outcome_name,
                            outcome_role=role,
                            points=points,
                            decimal_odds=decimal_odds,
                            source_updated_at=effective_update,
                            collected_at=collected,
                            status="open",
                        )
                    )

                if invalid_price:
                    continue
                required_roles = {"home", "away", "draw"} if spec.allows_draw else {"home", "away"}
                if roles != required_roles:
                    raise ValueError("Market outcomes do not match the required outcome roles")
                if spec.provider_market_key == "spreads":
                    if len(spread_points) != 2 or spread_points[0] != -spread_points[1]:
                        raise ValueError("Spread market does not contain opposite point values")
                quotes.extend(market_quotes)

    return MultiSportCollectionBatch(
        provider_key=PROVIDER_KEY,
        sport=sport,
        provider_sport_key=provider_sport_key,
        collected_at=collected,
        raw_payload=payload,
        quotes=tuple(quotes),
        warnings=tuple(warnings),
    )


class MultiSportOddsCollector:
    """Quota-aware, read-only Phase 2 collector for the controlled new sports."""

    provider_key = PROVIDER_KEY

    def __init__(
        self,
        *,
        api_key: str,
        transport: HttpTransport,
        bookmakers: tuple[str, ...] = (),
        regions: tuple[str, ...] = ("us",),
    ):
        if not api_key.strip():
            raise ValueError("A non-empty Odds API key is required")
        if not bookmakers and not regions:
            raise ValueError("At least one bookmaker or region must be configured")
        self._api_key = api_key
        self._transport = transport
        self._bookmakers = tuple(dict.fromkeys(bookmakers))
        self._regions = tuple(dict.fromkeys(regions))

    @staticmethod
    def _metadata(headers: Mapping[str, str], **extra: Any) -> dict[str, Any]:
        return {
            "requests_remaining": _header_int(headers, "x-requests-remaining"),
            "requests_used": _header_int(headers, "x-requests-used"),
            "request_cost": _header_int(headers, "x-requests-last"),
            **extra,
        }

    def collect_sports_catalog(self) -> SportsCatalogBatch:
        response = self._transport.get(
            f"{BASE_URL}/sports/",
            params={"apiKey": self._api_key},
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Sports catalog response must be a list")
        return SportsCatalogBatch(
            provider_key=PROVIDER_KEY,
            collected_at=response.received_at,
            raw_payload=payload,
            metadata=self._metadata(response.headers, endpoint="sports_catalog"),
        )

    def discover_active_keys(
        self,
        sport: str,
        *,
        catalog: SportsCatalogBatch | None = None,
    ) -> tuple[str, ...]:
        batch = catalog or self.collect_sports_catalog()
        return discover_active_provider_keys(batch.raw_payload, sport)

    def collect_participants(
        self,
        *,
        sport: str,
        provider_sport_key: str,
    ) -> MultiSportCollectionBatch:
        _validate_provider_key(sport, provider_sport_key)
        response = self._transport.get(
            f"{BASE_URL}/sports/{provider_sport_key}/participants",
            params={"apiKey": self._api_key},
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Participants response must be a list")
        participants = parse_participants(
            payload,
            sport=sport,
            provider_sport_key=provider_sport_key,
        )
        return MultiSportCollectionBatch(
            provider_key=PROVIDER_KEY,
            sport=sport,
            provider_sport_key=provider_sport_key,
            collected_at=response.received_at,
            raw_payload=payload,
            participants=participants,
            metadata=self._metadata(response.headers, endpoint="participants"),
        )

    def collect_events(
        self,
        *,
        sport: str,
        provider_sport_key: str,
        participants: ParticipantDirectory,
    ) -> MultiSportCollectionBatch:
        _validate_provider_key(sport, provider_sport_key)
        response = self._transport.get(
            f"{BASE_URL}/sports/{provider_sport_key}/events",
            params={"apiKey": self._api_key, "dateFormat": "iso"},
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Events response must be a list")
        events = parse_events(
            payload,
            sport=sport,
            provider_sport_key=provider_sport_key,
            participants=participants,
        )
        return MultiSportCollectionBatch(
            provider_key=PROVIDER_KEY,
            sport=sport,
            provider_sport_key=provider_sport_key,
            collected_at=response.received_at,
            raw_payload=payload,
            events=events,
            metadata=self._metadata(response.headers, endpoint="events"),
        )

    def collect_odds(
        self,
        *,
        sport: str,
        provider_sport_key: str,
        participants: ParticipantDirectory,
    ) -> MultiSportCollectionBatch:
        spec = get_sport_market(sport)
        _validate_provider_key(sport, provider_sport_key)
        params = {
            "apiKey": self._api_key,
            "markets": spec.provider_market_key,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        if self._bookmakers:
            params["bookmakers"] = ",".join(self._bookmakers)
        else:
            params["regions"] = ",".join(self._regions)
        response = self._transport.get(
            f"{BASE_URL}/sports/{provider_sport_key}/odds/",
            params=params,
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Odds response must be a list")
        batch = parse_current_odds(
            payload,
            sport=sport,
            provider_sport_key=provider_sport_key,
            participants=participants,
            collected_at=response.received_at,
        )
        return MultiSportCollectionBatch(
            provider_key=batch.provider_key,
            sport=batch.sport,
            provider_sport_key=batch.provider_sport_key,
            collected_at=batch.collected_at,
            raw_payload=batch.raw_payload,
            quotes=batch.quotes,
            warnings=batch.warnings,
            metadata=self._metadata(response.headers, endpoint="odds"),
        )

    def collect_sport(self, sport: str) -> SportCollectionBundle:
        """Collect active keys, participant whitelists, schedules, and first-market odds."""
        catalog = self.collect_sports_catalog()
        active_keys = self.discover_active_keys(sport, catalog=catalog)
        if not active_keys:
            return SportCollectionBundle(
                sport=sport,
                active_provider_keys=(),
                participant_batches=(),
                event_batches=(),
                odds_batches=(),
                warnings=("No active provider sport keys were available",),
            )

        participant_batches: list[MultiSportCollectionBatch] = []
        event_batches: list[MultiSportCollectionBatch] = []
        odds_batches: list[MultiSportCollectionBatch] = []
        for provider_sport_key in active_keys:
            participant_batch = self.collect_participants(
                sport=sport,
                provider_sport_key=provider_sport_key,
            )
            directory = ParticipantDirectory(participant_batch.participants)
            event_batch = self.collect_events(
                sport=sport,
                provider_sport_key=provider_sport_key,
                participants=directory,
            )
            odds_batch = self.collect_odds(
                sport=sport,
                provider_sport_key=provider_sport_key,
                participants=directory,
            )
            participant_batches.append(participant_batch)
            event_batches.append(event_batch)
            odds_batches.append(odds_batch)

        return SportCollectionBundle(
            sport=sport,
            active_provider_keys=active_keys,
            participant_batches=tuple(participant_batches),
            event_batches=tuple(event_batches),
            odds_batches=tuple(odds_batches),
        )
