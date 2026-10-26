"""Leakage-safe historical results used by the first interpretable model.

The nflverse schedule file labels ``spread_line`` from the home-team point of
view: a positive value means the home team was favored.  The rest of our bot
stores a selected team's spread, so this module stores the home spread with the
opposite sign.  These are closing lines, not reconstructed pick-time prices.
They can train and test a cover-probability model, but they cannot recreate CLV
or the price that was available at an earlier decision time.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import io
import json
import sqlite3
from typing import Mapping
from zoneinfo import ZoneInfo

from .collectors.base import TeamResolver
from .collectors.http import HttpTransport
from .collectors.nflverse import NFLVERSE_TEAM_ALIASES, SCHEDULE_URL


HISTORY_SEASONS = tuple(range(2020, 2026))


@dataclass(frozen=True)
class HistoricalTrainingGame:
    source_game_id: str
    season: int
    week: int
    kickoff_time: datetime
    home_team_id: str
    away_team_id: str
    home_score: int
    away_score: int
    home_spread_points: float
    home_spread_decimal_odds: float | None = None
    away_spread_decimal_odds: float | None = None
    home_market_no_vig_probability: float | None = None
    home_rest_days: int | None = None
    away_rest_days: int | None = None
    home_qb_id: str | None = None
    away_qb_id: str | None = None
    home_coach: str | None = None
    away_coach: str | None = None


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Historical timestamps must include a time zone")
    return value.astimezone(timezone.utc).isoformat()


def _resolve_team(resolver: TeamResolver, value: str) -> str:
    normalized = value.strip().upper()
    return resolver.resolve(NFLVERSE_TEAM_ALIASES.get(normalized, value))


def _kickoff(row: Mapping[str, str]) -> datetime:
    gameday = (row.get("gameday") or "").strip()
    gametime = (row.get("gametime") or "").strip()
    if not gameday or not gametime:
        raise ValueError("missing gameday or gametime")
    local = datetime.fromisoformat(f"{gameday}T{gametime}").replace(
        tzinfo=ZoneInfo("America/New_York")
    )
    return local.astimezone(timezone.utc)


def _optional_text(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _optional_int(value: str | None) -> int | None:
    normalized = (value or "").strip()
    return int(float(normalized)) if normalized else None


def _american_to_decimal(value: str | None) -> float | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    american = float(normalized)
    if american == 0:
        raise ValueError("American odds cannot be zero")
    return 1.0 + (american / 100.0 if american > 0 else 100.0 / abs(american))


def _home_no_vig_probability(
    home_decimal: float | None, away_decimal: float | None
) -> float | None:
    if home_decimal is None or away_decimal is None:
        return None
    home_implied = 1.0 / home_decimal
    away_implied = 1.0 / away_decimal
    return home_implied / (home_implied + away_implied)


def parse_historical_games(
    text: str,
    *,
    resolver: TeamResolver,
    seasons: tuple[int, ...] = HISTORY_SEASONS,
) -> tuple[tuple[HistoricalTrainingGame, ...], tuple[str, ...]]:
    reader = csv.DictReader(io.StringIO(text))
    required = {
        "game_id",
        "season",
        "game_type",
        "week",
        "gameday",
        "gametime",
        "home_team",
        "away_team",
        "home_score",
        "away_score",
        "spread_line",
    }
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        missing = sorted(required.difference(reader.fieldnames or ()))
        raise ValueError(f"Historical schedule source is missing columns: {missing}")

    allowed = set(seasons)
    records: list[HistoricalTrainingGame] = []
    warnings: list[str] = []
    for row_number, row in enumerate(reader, 2):
        try:
            season = int(row["season"])
            if season not in allowed or row["game_type"].strip().upper() != "REG":
                continue
            home_score = int(float(row["home_score"]))
            away_score = int(float(row["away_score"]))
            provider_line = float(row["spread_line"])
            home_decimal = _american_to_decimal(row.get("home_spread_odds"))
            away_decimal = _american_to_decimal(row.get("away_spread_odds"))
            home = _resolve_team(resolver, row["home_team"])
            away = _resolve_team(resolver, row["away_team"])
            if home == away:
                raise ValueError("home and away resolve to the same team")
            records.append(
                HistoricalTrainingGame(
                    source_game_id=row["game_id"].strip(),
                    season=season,
                    week=int(float(row["week"])),
                    kickoff_time=_kickoff(row),
                    home_team_id=home,
                    away_team_id=away,
                    home_score=home_score,
                    away_score=away_score,
                    home_spread_points=-provider_line,
                    home_spread_decimal_odds=home_decimal,
                    away_spread_decimal_odds=away_decimal,
                    home_market_no_vig_probability=_home_no_vig_probability(
                        home_decimal, away_decimal
                    ),
                    home_rest_days=_optional_int(row.get("home_rest")),
                    away_rest_days=_optional_int(row.get("away_rest")),
                    home_qb_id=_optional_text(row.get("home_qb_id")),
                    away_qb_id=_optional_text(row.get("away_qb_id")),
                    home_coach=_optional_text(row.get("home_coach")),
                    away_coach=_optional_text(row.get("away_coach")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            warnings.append(f"Skipped historical row {row_number}: {exc}")
    return tuple(records), tuple(warnings)


def _last_modified(headers: Mapping[str, str]) -> datetime | None:
    value = headers.get("last-modified")
    if not value:
        return None
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def collect_historical_training_games(
    db: sqlite3.Connection,
    *,
    transport: HttpTransport,
    resolver: TeamResolver,
    seasons: tuple[int, ...] = HISTORY_SEASONS,
) -> dict[str, object]:
    """Download once, normalize regular-season results, and preserve provenance."""

    response = transport.get(SCHEDULE_URL, headers={"Accept": "text/csv"})
    text = response.body.decode("utf-8-sig")
    records, warnings = parse_historical_games(
        text, resolver=resolver, seasons=seasons
    )
    collected_at = response.received_at.astimezone(timezone.utc)
    source_updated_at = _last_modified(response.headers)
    checksum = hashlib.sha256(response.body).hexdigest()

    with db:
        db.execute(
            """
            INSERT OR IGNORE INTO raw_ingests (
                source_key, endpoint_name, source_updated_at, collected_at,
                content_sha256, payload_json, http_status
            ) VALUES ('nflverse', 'historical_training_games', ?, ?, ?, ?, ?)
            """,
            (
                _iso(source_updated_at) if source_updated_at else None,
                _iso(collected_at),
                checksum,
                json.dumps({"source_url": SCHEDULE_URL, "sha256": checksum}),
                response.status,
            ),
        )
        for item in records:
            db.execute(
                """
                INSERT INTO historical_training_games (
                    source_game_id, season, week, kickoff_time,
                    home_team_id, away_team_id, home_score, away_score,
                    home_spread_points, home_spread_decimal_odds,
                    away_spread_decimal_odds, home_market_no_vig_probability,
                    home_rest_days, away_rest_days, home_qb_id, away_qb_id,
                    home_coach, away_coach, line_type, source_updated_at, collected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'nflverse_closing', ?, ?)
                ON CONFLICT(source_game_id) DO UPDATE SET
                    season = excluded.season,
                    week = excluded.week,
                    kickoff_time = excluded.kickoff_time,
                    home_team_id = excluded.home_team_id,
                    away_team_id = excluded.away_team_id,
                    home_score = excluded.home_score,
                    away_score = excluded.away_score,
                    home_spread_points = excluded.home_spread_points,
                    home_spread_decimal_odds = excluded.home_spread_decimal_odds,
                    away_spread_decimal_odds = excluded.away_spread_decimal_odds,
                    home_market_no_vig_probability = excluded.home_market_no_vig_probability,
                    home_rest_days = excluded.home_rest_days,
                    away_rest_days = excluded.away_rest_days,
                    home_qb_id = excluded.home_qb_id,
                    away_qb_id = excluded.away_qb_id,
                    home_coach = excluded.home_coach,
                    away_coach = excluded.away_coach,
                    source_updated_at = excluded.source_updated_at,
                    collected_at = excluded.collected_at
                """,
                (
                    item.source_game_id,
                    item.season,
                    item.week,
                    _iso(item.kickoff_time),
                    item.home_team_id,
                    item.away_team_id,
                    item.home_score,
                    item.away_score,
                    item.home_spread_points,
                    item.home_spread_decimal_odds,
                    item.away_spread_decimal_odds,
                    item.home_market_no_vig_probability,
                    item.home_rest_days,
                    item.away_rest_days,
                    item.home_qb_id,
                    item.away_qb_id,
                    item.home_coach,
                    item.away_coach,
                    _iso(source_updated_at) if source_updated_at else None,
                    _iso(collected_at),
                ),
            )

    counts = {
        str(season): sum(item.season == season for item in records)
        for season in seasons
    }
    return {
        "source": "nflverse games.csv",
        "line_type": "closing_line_only",
        "market_baseline": "closing_spread_no_vig",
        "received": len(records),
        "stored": db.execute(
            "SELECT COUNT(*) FROM historical_training_games"
        ).fetchone()[0],
        "seasons": counts,
        "warnings": list(warnings),
        "notice": (
            "Closing lines support model training and calibration. They do not recreate historical pick-time prices or CLV."
        ),
    }
