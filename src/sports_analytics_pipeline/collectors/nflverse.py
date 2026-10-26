"""Validated nflverse schedule, roster, injury, and play-by-play collectors."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import io
from pathlib import Path
from typing import Mapping, Protocol
from zoneinfo import ZoneInfo

from .base import TeamResolver
from .http import HttpTransport
from ..venue_identity import normalize_venue_name


SCHEDULE_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
ROSTER_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/rosters/roster_{season}.csv"
)
INJURY_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{season}.csv"
)
PBP_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/pbp/"
    "play_by_play_{season}.csv.gz"
)


# nflverse uses "LA" for the Rams and "LAC" for the Chargers. Keep this
# provider-specific rule here so the shared resolver does not accept the
# otherwise ambiguous city-only alias "LA" from other data providers.
NFLVERSE_TEAM_ALIASES = {"LA": "LAR"}


class StadiumResolver(Protocol):
    def resolve(self, source_name: str) -> str:
        """Return a controlled stadium ID or raise for an unknown venue."""


class MappingStadiumResolver:
    def __init__(self, aliases: Mapping[str, str]):
        self._aliases = {normalize_venue_name(name): stadium_id for name, stadium_id in aliases.items()}

    def resolve(self, source_name: str) -> str:
        try:
            return self._aliases[normalize_venue_name(source_name)]
        except KeyError as exc:
            raise ValueError(f"Unknown stadium alias: {source_name!r}") from exc


@dataclass(frozen=True)
class ScheduleGame:
    source_game_id: str
    season: int
    season_type: str
    week: int
    home_team_id: str
    away_team_id: str
    kickoff_time: datetime
    stadium_id: str
    surface_override: str | None
    roof_status: str | None
    neutral_site: bool
    status: str
    home_score: int | None
    away_score: int | None


@dataclass(frozen=True)
class RosterRecord:
    player_id: str
    player_name: str
    team_id: str
    position: str | None
    roster_status: str
    starter_status: str | None
    depth_role: str | None
    source_updated_at: datetime


@dataclass(frozen=True)
class InjuryRecord:
    season: int
    season_type: str
    week: int
    player_id: str
    player_name: str
    team_id: str
    position: str | None
    injury: str | None
    practice_status: str | None
    game_status: str | None
    source_updated_at: datetime


@dataclass(frozen=True)
class NflverseBatch:
    dataset_name: str
    season: int
    collected_at: datetime
    raw_text: str
    records: tuple[object, ...]
    warnings: tuple[str, ...] = ()
    source_updated_at: datetime | None = None


@dataclass(frozen=True)
class ArchiveBatch:
    dataset_name: str
    season: int
    collected_at: datetime
    source_url: str
    content: bytes
    source_updated_at: datetime | None


def _rows(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV source is missing a header row")
    return [dict(row) for row in reader]


def _optional(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _optional_int(value: str | None) -> int | None:
    value = _optional(value)
    return None if value is None else int(float(value))


def _timestamp(value: str | None, fallback: datetime) -> datetime:
    value = _optional(value)
    if value is None:
        return fallback
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_team(team_resolver: TeamResolver, source_name: str) -> str:
    normalized = source_name.strip().upper()
    return team_resolver.resolve(NFLVERSE_TEAM_ALIASES.get(normalized, source_name))


def _season_type(value: str) -> str:
    normalized = value.strip().upper()
    mapping = {"PRE": "preseason", "REG": "regular", "POST": "postseason"}
    try:
        return mapping[normalized]
    except KeyError as exc:
        raise ValueError(f"Unknown NFL season type: {value!r}") from exc


def _kickoff(row: Mapping[str, str]) -> datetime:
    date_value = _optional(row.get("gameday"))
    time_value = _optional(row.get("gametime"))
    if not date_value or not time_value:
        raise ValueError("Schedule row is missing gameday or gametime")
    local = datetime.fromisoformat(f"{date_value}T{time_value}").replace(
        tzinfo=ZoneInfo("America/New_York")
    )
    return local.astimezone(timezone.utc)


def parse_schedules_csv(
    text: str,
    *,
    season: int,
    team_resolver: TeamResolver,
    stadium_resolver: StadiumResolver,
    collected_at: datetime,
    source_updated_at: datetime | None = None,
) -> NflverseBatch:
    records: list[ScheduleGame] = []
    warnings: list[str] = []
    for row in _rows(text):
        if int(row["season"]) != season:
            continue
        source_game_id = row.get("game_id") or "<missing>"
        try:
            home = _resolve_team(team_resolver, row["home_team"])
            away = _resolve_team(team_resolver, row["away_team"])
            stadium = stadium_resolver.resolve(row["stadium"])
            if home == away:
                raise ValueError("home and away teams resolve to one team")
            home_score = _optional_int(row.get("home_score"))
            away_score = _optional_int(row.get("away_score"))
            status = "final" if home_score is not None and away_score is not None else "scheduled"
            raw_roof = (_optional(row.get("roof")) or "").casefold()
            roof_status = {
                "closed": "closed",
                "dome": "fixed",
                "outdoors": "open",
                "open": "open",
                "retractable": "unknown",
            }.get(raw_roof)
            records.append(
                ScheduleGame(
                    source_game_id=source_game_id,
                    season=season,
                    season_type=_season_type(row["game_type"]),
                    week=int(float(row["week"])),
                    home_team_id=home,
                    away_team_id=away,
                    kickoff_time=_kickoff(row),
                    stadium_id=stadium,
                    surface_override=_optional(row.get("surface")),
                    roof_status=roof_status,
                    neutral_site=(_optional(row.get("location")) or "Home").casefold() != "home",
                    status=status,
                    home_score=home_score,
                    away_score=away_score,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            warnings.append(f"Skipped schedule game {source_game_id}: {exc}")

    return NflverseBatch(
        dataset_name="schedules",
        season=season,
        collected_at=collected_at,
        raw_text=text,
        records=tuple(records),
        warnings=tuple(warnings),
        source_updated_at=source_updated_at,
    )


def parse_rosters_csv(
    text: str,
    *,
    season: int,
    team_resolver: TeamResolver,
    collected_at: datetime,
    source_updated_at: datetime | None = None,
) -> NflverseBatch:
    records: list[RosterRecord] = []
    warnings: list[str] = []
    updated = source_updated_at or collected_at
    for index, row in enumerate(_rows(text), 2):
        try:
            if int(row.get("season") or season) != season:
                continue
            player_id = _optional(row.get("gsis_id")) or _optional(row.get("esb_id"))
            player_name = _optional(row.get("full_name")) or _optional(row.get("football_name"))
            if not player_id or not player_name:
                raise ValueError("player ID and name are required")
            records.append(
                RosterRecord(
                    player_id=player_id,
                    player_name=player_name,
                    team_id=_resolve_team(team_resolver, row["team"]),
                    position=_optional(row.get("position")),
                    roster_status=_optional(row.get("status")) or "unknown",
                    starter_status=_optional(row.get("starter_status")),
                    depth_role=_optional(row.get("depth_chart_position")),
                    source_updated_at=updated,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            warnings.append(f"Skipped roster row {index}: {exc}")
    return NflverseBatch(
        dataset_name="rosters",
        season=season,
        collected_at=collected_at,
        raw_text=text,
        records=tuple(records),
        warnings=tuple(warnings),
        source_updated_at=updated,
    )


def parse_injuries_csv(
    text: str,
    *,
    season: int,
    team_resolver: TeamResolver,
    collected_at: datetime,
    source_updated_at: datetime | None = None,
) -> NflverseBatch:
    records: list[InjuryRecord] = []
    warnings: list[str] = []
    fallback = source_updated_at or collected_at
    for index, row in enumerate(_rows(text), 2):
        try:
            if int(row.get("season") or season) != season:
                continue
            player_id = _optional(row.get("gsis_id"))
            player_name = _optional(row.get("full_name"))
            if not player_id or not player_name:
                raise ValueError("player ID and name are required")
            records.append(
                InjuryRecord(
                    season=season,
                    season_type=_season_type(row["game_type"]),
                    week=int(float(row["week"])),
                    player_id=player_id,
                    player_name=player_name,
                    team_id=_resolve_team(team_resolver, row["team"]),
                    position=_optional(row.get("position")),
                    injury=_optional(row.get("report_primary_injury"))
                    or _optional(row.get("practice_primary_injury")),
                    practice_status=_optional(row.get("practice_status")),
                    game_status=_optional(row.get("report_status")),
                    source_updated_at=_timestamp(row.get("date_modified"), fallback),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            warnings.append(f"Skipped injury row {index}: {exc}")
    return NflverseBatch(
        dataset_name="injuries",
        season=season,
        collected_at=collected_at,
        raw_text=text,
        records=tuple(records),
        warnings=tuple(warnings),
        source_updated_at=fallback,
    )


def _last_modified(headers: Mapping[str, str]) -> datetime | None:
    value = headers.get("last-modified")
    if not value:
        return None
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class NflverseCollector:
    provider_key = "nflverse"

    def __init__(
        self,
        *,
        transport: HttpTransport,
        team_resolver: TeamResolver,
        stadium_resolver: StadiumResolver,
    ):
        self._transport = transport
        self._teams = team_resolver
        self._stadiums = stadium_resolver

    def collect_schedules(self, season: int) -> NflverseBatch:
        response = self._transport.get(SCHEDULE_URL, headers={"Accept": "text/csv"})
        return parse_schedules_csv(
            response.body.decode("utf-8-sig"),
            season=season,
            team_resolver=self._teams,
            stadium_resolver=self._stadiums,
            collected_at=response.received_at,
            source_updated_at=_last_modified(response.headers),
        )

    def collect_rosters(self, season: int) -> NflverseBatch:
        url = ROSTER_URL.format(season=season)
        response = self._transport.get(url, headers={"Accept": "text/csv"})
        return parse_rosters_csv(
            response.body.decode("utf-8-sig"),
            season=season,
            team_resolver=self._teams,
            collected_at=response.received_at,
            source_updated_at=_last_modified(response.headers),
        )

    def collect_injuries(self, season: int) -> NflverseBatch:
        url = INJURY_URL.format(season=season)
        response = self._transport.get(url, headers={"Accept": "text/csv"})
        return parse_injuries_csv(
            response.body.decode("utf-8-sig"),
            season=season,
            team_resolver=self._teams,
            collected_at=response.received_at,
            source_updated_at=_last_modified(response.headers),
        )

    def collect_play_by_play_archive(self, season: int) -> ArchiveBatch:
        url = PBP_URL.format(season=season)
        response = self._transport.get(url, headers={"Accept": "application/gzip"})
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(response.body)) as archive:
                header = archive.readline().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("Play-by-play response is not a valid CSV gzip archive") from exc
        required = {"game_id", "play_id", "home_team", "away_team"}
        if not required.issubset(set(next(csv.reader([header])))):
            raise ValueError("Play-by-play archive is missing required columns")
        return ArchiveBatch(
            dataset_name="play_by_play",
            season=season,
            collected_at=response.received_at,
            source_url=url,
            content=response.body,
            source_updated_at=_last_modified(response.headers),
        )


def safe_archive_path(base_directory: str | Path, season: int) -> Path:
    if season < 1999 or season > datetime.now(timezone.utc).year + 1:
        raise ValueError("season is outside the supported play-by-play range")
    base = Path(base_directory).resolve()
    return base / f"play_by_play_{season}.csv.gz"
