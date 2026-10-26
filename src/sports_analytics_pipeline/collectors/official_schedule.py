"""Bundled, reviewed official NFL preseason schedule data."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files
import json
import sqlite3
from typing import Any

from .nflverse import ScheduleGame


@dataclass(frozen=True)
class OfficialScheduleBatch:
    season: int
    collected_at: datetime
    raw_payload: dict[str, Any]
    records: tuple[ScheduleGame, ...]
    warnings: tuple[str, ...] = ()


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Official schedule kickoff must include a time zone")
    return parsed.astimezone(timezone.utc)


def _roof_status(roof_type: str) -> str:
    return {
        "open": "open",
        "fixed": "fixed",
        "retractable": "unknown",
        "canopy_open_sides": "unknown",
    }[roof_type]


def load_bundled_preseason_schedule(
    season: int,
    connection: sqlite3.Connection,
    *,
    collected_at: datetime | None = None,
) -> OfficialScheduleBatch:
    """Load and strictly validate the complete reviewed preseason schedule."""
    if season != 2026:
        raise ValueError("The bundled official preseason schedule is available only for 2026")
    collected = collected_at or datetime.now(timezone.utc)
    if collected.tzinfo is None or collected.utcoffset() is None:
        raise ValueError("collected_at must include a time zone")
    collected = collected.astimezone(timezone.utc)

    payload = json.loads(
        files("sports_analytics_pipeline.data")
        .joinpath("preseason_schedule_2026.json")
        .read_text(encoding="utf-8")
    )
    games = payload.get("games")
    if not isinstance(games, list):
        raise ValueError("Bundled preseason schedule is missing its games list")

    teams = {
        row["team_id"]: row["primary_stadium_id"]
        for row in connection.execute("SELECT team_id, primary_stadium_id FROM teams")
    }
    stadiums = {
        row["stadium_id"]: row["roof_type"]
        for row in connection.execute("SELECT stadium_id, roof_type FROM stadiums")
    }
    records: list[ScheduleGame] = []
    for game in games:
        home = str(game["home_team_id"])
        away = str(game["away_team_id"])
        if home not in teams or away not in teams:
            raise ValueError(f"Unknown team in official schedule: {away} at {home}")
        if home == away:
            raise ValueError("Official schedule game cannot use one team twice")
        stadium_id = str(game.get("stadium_id") or teams[home])
        if stadium_id not in stadiums:
            raise ValueError(f"Unknown stadium in official schedule: {stadium_id}")
        records.append(
            ScheduleGame(
                source_game_id=str(game["game_id"]),
                season=season,
                season_type="preseason",
                week=int(game["week"]),
                home_team_id=home,
                away_team_id=away,
                kickoff_time=_timestamp(str(game["kickoff_time"])),
                stadium_id=stadium_id,
                surface_override=None,
                roof_status=_roof_status(stadiums[stadium_id]),
                neutral_site=bool(game.get("neutral_site", False)),
                status="scheduled",
                home_score=None,
                away_score=None,
            )
        )

    ids = [record.source_game_id for record in records]
    if len(records) != 49 or len(set(ids)) != 49:
        raise ValueError("The 2026 preseason schedule must contain 49 unique games")
    week_counts = Counter(record.week for record in records)
    if week_counts != Counter({0: 1, 1: 16, 2: 16, 3: 16}):
        raise ValueError(f"Unexpected preseason week counts: {dict(week_counts)}")
    expected_teams = set(teams)
    for week in (1, 2, 3):
        appearances = [
            team
            for record in records
            if record.week == week
            for team in (record.home_team_id, record.away_team_id)
        ]
        if len(appearances) != 32 or set(appearances) != expected_teams:
            raise ValueError(f"Preseason week {week} must schedule every NFL team once")

    return OfficialScheduleBatch(
        season=season,
        collected_at=collected,
        raw_payload=payload,
        records=tuple(records),
    )
