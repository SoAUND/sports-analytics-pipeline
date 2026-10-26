"""Read-only official MLB collection for schedule/probable pitchers and required page checks.

Structured schedule data is accepted only from MLB's official stats service.  Required
injury, lineup, transaction, and probable-pitcher pages are checked separately.  We do
not infer player facts from unstable HTML markup; if a structured fact is unavailable,
the evaluation layer must remain conditional/test-only or pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
from typing import Any, Protocol

from .base import TeamResolver
from .http import HttpTransport
from .the_odds_api import parse_timestamp
from ..venue_identity import normalize_venue_name


STATS_BASE_URL = "https://statsapi.mlb.com/api/v1"
OFFICIAL_PAGES = {
    "injuries": ("mlb_injuries", "https://www.mlb.com/injury-report", "injur"),
    "lineups": ("mlb_lineups", "https://www.mlb.com/starting-lineups", "lineup"),
    "transactions": ("mlb_transactions", "https://www.mlb.com/transactions", "transaction"),
    "probable_pitchers": ("mlb_probable_pitchers", "https://www.mlb.com/probable-pitchers", "pitcher"),
}


class StadiumResolver(Protocol):
    def resolve(self, source_name: str, source_id: str | int | None = None) -> str:
        ...


class MappingStadiumResolver:
    def __init__(
        self,
        aliases: dict[str, str],
        *,
        provider_ids: dict[str | int, str] | None = None,
    ):
        self._aliases = {normalize_venue_name(name): stadium_id for name, stadium_id in aliases.items()}
        self._provider_ids = {
            str(provider_id).strip(): stadium_id
            for provider_id, stadium_id in (provider_ids or {}).items()
        }

    def resolve(self, source_name: str, source_id: str | int | None = None) -> str:
        if source_id is not None:
            stadium_id = self._provider_ids.get(str(source_id).strip())
            if stadium_id is not None:
                return stadium_id
        try:
            return self._aliases[normalize_venue_name(source_name)]
        except KeyError as exc:
            identity = f"id={source_id!r}, name={source_name!r}" if source_id is not None else repr(source_name)
            raise ValueError(f"Unknown MLB stadium alias: {identity}") from exc

    def resolve_optional(self, source_name: str, source_id: str | int | None = None) -> str | None:
        try:
            return self.resolve(source_name, source_id)
        except ValueError:
            return None


@dataclass(frozen=True)
class MlbProbablePitcher:
    player_id: str
    player_name: str
    team_id: str


@dataclass(frozen=True)
class MlbLineupPlayer:
    player_id: str
    player_name: str
    position: str | None


@dataclass(frozen=True)
class MlbScheduleGame:
    source_game_id: str
    season: int
    home_team_id: str
    away_team_id: str
    game_time: datetime
    stadium_id: str
    status: str
    home_score: int | None
    away_score: int | None
    probable_home_pitcher: MlbProbablePitcher | None
    probable_away_pitcher: MlbProbablePitcher | None
    home_lineup: tuple[MlbLineupPlayer, ...] = ()
    away_lineup: tuple[MlbLineupPlayer, ...] = ()


@dataclass(frozen=True)
class MlbScheduleBatch:
    provider_key: str
    collected_at: datetime
    schedule_date: str
    raw_payload: dict[str, Any]
    games: tuple[MlbScheduleGame, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class OfficialMlbPageCheck:
    check_type: str
    source_key: str
    source_url: str
    checked_at: datetime
    http_status: int
    content_sha256: str
    access_status: str
    warning: str | None = None


def _game_status(game: dict[str, Any]) -> str:
    status = game.get("status") or {}
    abstract = str(status.get("abstractGameState", "")).casefold()
    detailed = str(status.get("detailedState", "")).casefold()
    if "postpon" in detailed:
        return "postponed"
    if "cancel" in detailed:
        return "cancelled"
    if abstract == "final" or detailed == "final":
        return "final"
    return "scheduled"


def _probable(side: dict[str, Any], resolver: TeamResolver) -> MlbProbablePitcher | None:
    pitcher = side.get("probablePitcher")
    if not isinstance(pitcher, dict) or pitcher.get("id") is None or not pitcher.get("fullName"):
        return None
    team_id = resolver.resolve(str((side.get("team") or {})["name"]))
    return MlbProbablePitcher(
        player_id=f"mlb_person_{int(pitcher['id'])}",
        player_name=str(pitcher["fullName"]),
        team_id=team_id,
    )


def _lineup_players(game: dict[str, Any], side: str) -> tuple[MlbLineupPlayer, ...]:
    lineups = game.get("lineups") or {}
    raw = lineups.get(f"{side}Players")
    if not isinstance(raw, list):
        return ()
    players: list[MlbLineupPlayer] = []
    seen: set[int] = set()
    for item in raw:
        if not isinstance(item, dict) or item.get("id") is None or not item.get("fullName"):
            continue
        person_id = int(item["id"])
        if person_id in seen:
            continue
        seen.add(person_id)
        position = item.get("primaryPosition") or {}
        players.append(
            MlbLineupPlayer(
                player_id=f"mlb_person_{person_id}",
                player_name=str(item["fullName"]),
                position=str(position.get("abbreviation")) if position.get("abbreviation") else None,
            )
        )
    return tuple(players)


def parse_mlb_schedule(
    payload: dict[str, Any],
    *,
    team_resolver: TeamResolver,
    stadium_resolver: StadiumResolver,
    collected_at: datetime,
    expected_date: str,
) -> MlbScheduleBatch:
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must include a time zone")
    collected = collected_at.astimezone(timezone.utc)
    games: list[MlbScheduleGame] = []
    warnings: list[str] = []

    dates = payload.get("dates", [])
    if not isinstance(dates, list):
        raise ValueError("MLB schedule response dates must be a list")
    for date_block in dates:
        block_date = str(date_block.get("date", ""))
        if block_date and block_date != expected_date:
            warnings.append(f"Skipped MLB schedule block for unexpected date {block_date}")
            continue
        for game in date_block.get("games", []):
            game_pk = game.get("gamePk")
            game_date = game.get("gameDate")
            teams = game.get("teams") or {}
            venue = game.get("venue") or {}
            if game_pk is None or not game_date:
                warnings.append("Skipped MLB schedule game missing identity or time")
                continue
            venue_name = str(venue.get("name") or "").strip()
            venue_source_id = venue.get("id")
            if not venue_name and venue_source_id is None:
                warnings.append(f"Skipped MLB game {game_pk} missing venue identity")
                continue
            home = teams.get("home") or {}
            away = teams.get("away") or {}
            home_team_id = team_resolver.resolve(str((home.get("team") or {})["name"]))
            away_team_id = team_resolver.resolve(str((away.get("team") or {})["name"]))
            if home_team_id == away_team_id:
                raise ValueError("MLB schedule resolves both sides to one team")
            try:
                stadium_id = stadium_resolver.resolve(venue_name, venue_source_id)
            except TypeError:
                # Compatibility for a small custom resolver that predates provider IDs.
                try:
                    stadium_id = stadium_resolver.resolve(venue_name)
                except ValueError as exc:
                    warnings.append(f"Skipped MLB game {game_pk}: {exc}")
                    continue
            except ValueError as exc:
                warnings.append(f"Skipped MLB game {game_pk}: {exc}")
                continue
            game_time = parse_timestamp(str(game_date))
            if game_time < collected - timedelta(days=2):
                warnings.append(f"MLB game {game_pk} is already materially in the past")
            games.append(
                MlbScheduleGame(
                    source_game_id=str(game_pk),
                    season=int(game.get("season") or game_time.year),
                    home_team_id=home_team_id,
                    away_team_id=away_team_id,
                    game_time=game_time,
                    stadium_id=stadium_id,
                    status=_game_status(game),
                    home_score=int(home["score"]) if home.get("score") is not None else None,
                    away_score=int(away["score"]) if away.get("score") is not None else None,
                    probable_home_pitcher=_probable(home, team_resolver),
                    probable_away_pitcher=_probable(away, team_resolver),
                    home_lineup=_lineup_players(game, "home"),
                    away_lineup=_lineup_players(game, "away"),
                )
            )
    return MlbScheduleBatch(
        provider_key="mlb_schedule",
        collected_at=collected,
        schedule_date=expected_date,
        raw_payload=payload,
        games=tuple(games),
        warnings=tuple(warnings),
    )


class MlbScheduleCollector:
    provider_key = "mlb_schedule"

    def __init__(self, *, transport: HttpTransport, team_resolver: TeamResolver, stadium_resolver: StadiumResolver):
        self._transport = transport
        self._teams = team_resolver
        self._stadiums = stadium_resolver

    def collect_date(self, schedule_date: date | str) -> MlbScheduleBatch:
        requested = schedule_date.isoformat() if isinstance(schedule_date, date) else str(schedule_date)
        response = self._transport.get(
            f"{STATS_BASE_URL}/schedule",
            params={
                "sportId": "1",
                "date": requested,
                "hydrate": "probablePitcher,lineups,venue",
            },
            headers={"Accept": "application/json", "User-Agent": "OurSportsBot/0.3"},
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("MLB schedule response must be an object")
        return parse_mlb_schedule(
            payload,
            team_resolver=self._teams,
            stadium_resolver=self._stadiums,
            collected_at=response.received_at,
            expected_date=requested,
        )


@dataclass(frozen=True)
class MlbRecentResult:
    source_game_id: str
    season: int
    game_time: datetime
    home_team_id: str
    away_team_id: str
    home_score: int
    away_score: int


@dataclass(frozen=True)
class MlbRecentResultsBatch:
    provider_key: str
    collected_at: datetime
    start_date: str
    end_date: str
    raw_payload: dict[str, Any]
    games: tuple[MlbRecentResult, ...]
    warnings: tuple[str, ...] = ()


def parse_mlb_recent_results(
    payload: dict[str, Any],
    *,
    team_resolver: TeamResolver,
    collected_at: datetime,
    start_date: str,
    end_date: str,
) -> MlbRecentResultsBatch:
    """Parse only completed MLB regular-season games known by collection time."""
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must include a time zone")
    collected = collected_at.astimezone(timezone.utc)
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("recent-result dates must use YYYY-MM-DD") from exc
    if end < start:
        raise ValueError("recent-result end date cannot precede start date")

    dates = payload.get("dates", [])
    if not isinstance(dates, list):
        raise ValueError("MLB recent-results response dates must be a list")
    games: list[MlbRecentResult] = []
    warnings: list[str] = []
    for block in dates:
        block_date = str(block.get("date") or "")
        if block_date:
            try:
                block_day = date.fromisoformat(block_date)
            except ValueError:
                warnings.append(f"Skipped invalid MLB result date block {block_date!r}")
                continue
            if block_day < start or block_day > end:
                warnings.append(f"Skipped MLB result block outside requested window: {block_date}")
                continue
        for game in block.get("games", []):
            if str(game.get("gameType") or "R").upper() != "R":
                continue
            if _game_status(game) != "final":
                continue
            game_pk = game.get("gamePk")
            game_date = game.get("gameDate")
            teams = game.get("teams") or {}
            home = teams.get("home") or {}
            away = teams.get("away") or {}
            if game_pk is None or not game_date:
                warnings.append("Skipped final MLB result missing game identity/time")
                continue
            if home.get("score") is None or away.get("score") is None:
                warnings.append(f"Skipped final MLB game {game_pk} without both scores")
                continue
            home_team = home.get("team") or {}
            away_team = away.get("team") or {}
            if not home_team.get("name") or not away_team.get("name"):
                warnings.append(f"Skipped final MLB game {game_pk} without both teams")
                continue
            game_time = parse_timestamp(str(game_date))
            if game_time > collected:
                raise ValueError("MLB final result cannot have a future game time")
            home_id = team_resolver.resolve(str(home_team["name"]))
            away_id = team_resolver.resolve(str(away_team["name"]))
            if home_id == away_id:
                raise ValueError("MLB recent result resolves both sides to one team")
            home_score = int(home["score"])
            away_score = int(away["score"])
            if home_score == away_score:
                warnings.append(f"Skipped tied MLB result {game_pk}; regular MLB finals should not tie")
                continue
            games.append(
                MlbRecentResult(
                    source_game_id=str(game_pk),
                    season=int(game.get("season") or game_time.year),
                    game_time=game_time,
                    home_team_id=home_id,
                    away_team_id=away_id,
                    home_score=home_score,
                    away_score=away_score,
                )
            )
    games.sort(key=lambda item: (item.game_time, item.source_game_id))
    return MlbRecentResultsBatch(
        provider_key="mlb_schedule",
        collected_at=collected,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        raw_payload=payload,
        games=tuple(games),
        warnings=tuple(warnings),
    )


class MlbRecentResultsCollector:
    provider_key = "mlb_schedule"

    def __init__(self, *, transport: HttpTransport, team_resolver: TeamResolver):
        self._transport = transport
        self._teams = team_resolver

    def collect_before(
        self, operation_date: date | str, *, lookback_days: int = 21
    ) -> MlbRecentResultsBatch:
        if lookback_days < 10 or lookback_days > 45:
            raise ValueError("lookback_days must be between 10 and 45")
        if isinstance(operation_date, date):
            target = operation_date
        else:
            try:
                target = date.fromisoformat(str(operation_date))
            except ValueError as exc:
                raise ValueError("operation_date must use YYYY-MM-DD") from exc
        ending = target - timedelta(days=1)
        starting = target - timedelta(days=lookback_days)
        response = self._transport.get(
            f"{STATS_BASE_URL}/schedule",
            params={
                "sportId": "1",
                "gameType": "R",
                "startDate": starting.strftime("%m/%d/%Y"),
                "endDate": ending.strftime("%m/%d/%Y"),
            },
            headers={"Accept": "application/json", "User-Agent": "OurSportsBot/0.5"},
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("MLB recent-results response must be an object")
        return parse_mlb_recent_results(
            payload,
            team_resolver=self._teams,
            collected_at=response.received_at,
            start_date=starting.isoformat(),
            end_date=ending.isoformat(),
        )


@dataclass(frozen=True)
class MlbTransactionRecord:
    player_id: str
    player_name: str
    from_team_id: str | None
    to_team_id: str | None
    position: str | None
    transaction_type: str
    transaction_date: str
    description: str
    roster_status: str | None
    injured_list: bool
    source_updated_at: datetime
    source_time_is_collection_fallback: bool


@dataclass(frozen=True)
class MlbTransactionsBatch:
    provider_key: str
    collected_at: datetime
    start_date: str
    end_date: str
    raw_payload: dict[str, Any]
    records: tuple[MlbTransactionRecord, ...]
    warnings: tuple[str, ...] = ()


def _optional_team_id(team: Any, resolver: TeamResolver) -> str | None:
    if not isinstance(team, dict):
        return None
    name = str(team.get("name") or "").strip()
    if not name:
        return None
    return resolver.resolve(name)


def _transaction_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "T" in text:
        try:
            return parse_timestamp(text).date().isoformat()
        except ValueError:
            return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _transaction_source_time(value: Any, collected_at: datetime) -> tuple[datetime, bool]:
    text = str(value or "").strip()
    if text and "T" in text:
        try:
            parsed = parse_timestamp(text)
            if parsed <= collected_at:
                return parsed, False
        except ValueError:
            pass
    # MLB transaction records commonly expose an effective date without a precise
    # posting timestamp.  First collection time is the safest prediction-time
    # boundary because it never makes the fact available earlier than observed.
    return collected_at, True


def _roster_status(transaction_type: str, description: str) -> str | None:
    text = f"{transaction_type} {description}".casefold()
    injured = "injured list" in text or "disabled list" in text
    if injured and any(word in text for word in ("reinstated", "activated")):
        return "active"
    if injured and any(word in text for word in ("placed", "transferred")):
        return "injured_list"
    if "recalled" in text:
        return "active"
    if "selected" in text and "contract" in text:
        return "active"
    if "activated" in text:
        return "active"
    if "optioned" in text:
        return "optioned"
    if "designated for assignment" in text:
        return "designated_for_assignment"
    if "released" in text:
        return "released"
    if "outright" in text:
        return "minors"
    return None


def parse_mlb_transactions(
    payload: dict[str, Any],
    *,
    team_resolver: TeamResolver,
    collected_at: datetime,
    start_date: str,
    end_date: str,
) -> MlbTransactionsBatch:
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must include a time zone")
    collected = collected_at.astimezone(timezone.utc)
    raw_records = payload.get("transactions", [])
    if not isinstance(raw_records, list):
        raise ValueError("MLB transactions response transactions must be a list")

    records: list[MlbTransactionRecord] = []
    warnings: list[str] = []
    used_collection_fallback = False
    for index, raw in enumerate(raw_records):
        if not isinstance(raw, dict):
            warnings.append(f"Skipped MLB transaction {index}: record was not an object")
            continue
        person = raw.get("person") or {}
        if not isinstance(person, dict) or person.get("id") is None or not person.get("fullName"):
            warnings.append(f"Skipped MLB transaction {index}: player identity was missing")
            continue
        date_value = raw.get("effectiveDate") or raw.get("date")
        tx_date = _transaction_date(date_value)
        if tx_date is None:
            warnings.append(f"Skipped MLB transaction {index}: transaction date was missing or invalid")
            continue

        from_team_id = _optional_team_id(raw.get("fromTeam"), team_resolver)
        to_team_id = _optional_team_id(raw.get("toTeam"), team_resolver)
        transaction_type = str(raw.get("typeDesc") or raw.get("typeCode") or "unknown").strip()
        description = str(raw.get("description") or transaction_type).strip()
        position = person.get("primaryPosition") or {}
        position_text = (
            str(position.get("abbreviation")).strip()
            if isinstance(position, dict) and position.get("abbreviation")
            else None
        )
        source_updated_at, fallback = _transaction_source_time(date_value, collected)
        used_collection_fallback = used_collection_fallback or fallback
        roster_status = _roster_status(transaction_type, description)
        records.append(
            MlbTransactionRecord(
                player_id=f"mlb_person_{int(person['id'])}",
                player_name=str(person["fullName"]),
                from_team_id=from_team_id,
                to_team_id=to_team_id,
                position=position_text,
                transaction_type=transaction_type,
                transaction_date=tx_date,
                description=description,
                roster_status=roster_status,
                injured_list=roster_status == "injured_list",
                source_updated_at=source_updated_at,
                source_time_is_collection_fallback=fallback,
            )
        )

    if used_collection_fallback:
        warnings.append(
            "One or more MLB transaction records had date-level timing only; "
            "first collection time is used as the prediction-time availability boundary."
        )
    return MlbTransactionsBatch(
        provider_key="mlb_transactions",
        collected_at=collected,
        start_date=start_date,
        end_date=end_date,
        raw_payload=payload,
        records=tuple(records),
        warnings=tuple(warnings),
    )


class MlbTransactionsCollector:
    provider_key = "mlb_transactions"

    def __init__(self, *, transport: HttpTransport, team_resolver: TeamResolver):
        self._transport = transport
        self._teams = team_resolver

    def collect_window(self, end_date: date | str, *, lookback_days: int = 7) -> MlbTransactionsBatch:
        if lookback_days < 1 or lookback_days > 30:
            raise ValueError("lookback_days must be between 1 and 30")
        if isinstance(end_date, date):
            ending = end_date
        else:
            try:
                ending = date.fromisoformat(str(end_date))
            except ValueError as exc:
                raise ValueError("end_date must use YYYY-MM-DD") from exc
        starting = ending - timedelta(days=lookback_days)
        response = self._transport.get(
            f"{STATS_BASE_URL}/transactions",
            params={
                "sportId": "1",
                "startDate": starting.strftime("%m/%d/%Y"),
                "endDate": ending.strftime("%m/%d/%Y"),
                "hydrate": "person",
            },
            headers={"Accept": "application/json", "User-Agent": "OurSportsBot/0.4"},
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("MLB transactions response must be an object")
        return parse_mlb_transactions(
            payload,
            team_resolver=self._teams,
            collected_at=response.received_at,
            start_date=starting.isoformat(),
            end_date=ending.isoformat(),
        )


class OfficialMlbPageCollector:
    """Reachability/identity checks only; no unstable HTML player-fact scraping."""

    def __init__(self, *, transport: HttpTransport):
        self._transport = transport

    def check(self, check_type: str) -> OfficialMlbPageCheck:
        try:
            source_key, url, marker = OFFICIAL_PAGES[check_type]
        except KeyError as exc:
            raise ValueError(f"Unsupported official MLB check: {check_type!r}") from exc
        response = self._transport.get(
            url,
            headers={"Accept": "text/html", "User-Agent": "OurSportsBot/0.3"},
        )
        text = response.body.decode("utf-8", errors="replace").casefold()
        identifiable = "mlb" in text and marker in text
        return OfficialMlbPageCheck(
            check_type=check_type,
            source_key=source_key,
            source_url=url,
            checked_at=response.received_at,
            http_status=response.status,
            content_sha256=hashlib.sha256(response.body).hexdigest(),
            access_status="confirmed" if identifiable else "unavailable",
            warning=None if identifiable else "Official MLB page did not contain the expected marker",
        )

    def check_required_pages(self) -> tuple[OfficialMlbPageCheck, ...]:
        return tuple(self.check(name) for name in OFFICIAL_PAGES)
