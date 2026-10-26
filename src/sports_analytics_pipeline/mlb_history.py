"""Historical MLB regular-season results for the Phase 4 model.

The collector uses the read-only MLB Stats API schedule service. It stores only
final regular-season games and preserves collection provenance. Historical
sportsbook prices are intentionally nullable because the free workflow cannot
reconstruct truthful prediction-time prices.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any, Iterable

from .collectors.base import TeamResolver
from .collectors.http import HttpTransport

MLB_HISTORY_URL = "https://statsapi.mlb.com/api/v1/schedule"
DEFAULT_HISTORY_SEASONS = (2020, 2021, 2022, 2023, 2024, 2025, 2026)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Historical MLB timestamps must include a time zone")
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Historical MLB gameDate is missing a time zone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class HistoricalMlbGame:
    source_game_id: str
    season: int
    kickoff_time: datetime
    home_team_id: str
    away_team_id: str
    home_score: int
    away_score: int
    game_number: int


def parse_historical_mlb_schedule(
    payload: dict[str, Any],
    *,
    resolver: TeamResolver,
    seasons: Iterable[int] | None = None,
) -> tuple[tuple[HistoricalMlbGame, ...], tuple[str, ...]]:
    """Normalize final MLB regular-season games and fail closed on bad identity."""

    allowed = None if seasons is None else {int(value) for value in seasons}
    games: list[HistoricalMlbGame] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for date_block in payload.get("dates", []):
        for raw in date_block.get("games", []):
            try:
                season = int(raw.get("season") or str(date_block.get("date", ""))[:4])
            except (TypeError, ValueError):
                warnings.append("Skipped historical MLB game with invalid season.")
                continue
            if allowed is not None and season not in allowed:
                continue
            if str(raw.get("gameType", "")) != "R":
                continue
            status = raw.get("status") or {}
            if str(status.get("abstractGameState", "")).casefold() != "final":
                continue
            try:
                source_game_id = str(raw["gamePk"])
                if source_game_id in seen:
                    continue
                home_raw = raw["teams"]["home"]
                away_raw = raw["teams"]["away"]
                home_name = str(home_raw["team"]["name"])
                away_name = str(away_raw["team"]["name"])
                home_id = resolver.resolve(home_name)
                away_id = resolver.resolve(away_name)
                if home_id == away_id:
                    raise ValueError("home and away resolved to the same team")
                home_score = int(home_raw["score"])
                away_score = int(away_raw["score"])
                if home_score == away_score:
                    raise ValueError("final MLB regular-season game cannot be stored as a tie")
                kickoff = _parse_time(str(raw["gameDate"]))
                game_number = int(raw.get("gameNumber") or 1)
            except (KeyError, TypeError, ValueError) as exc:
                warnings.append(
                    f"Skipped historical MLB game {raw.get('gamePk', 'unknown')}: {exc}"
                )
                continue
            games.append(
                HistoricalMlbGame(
                    source_game_id=source_game_id,
                    season=season,
                    kickoff_time=kickoff,
                    home_team_id=home_id,
                    away_team_id=away_id,
                    home_score=home_score,
                    away_score=away_score,
                    game_number=game_number,
                )
            )
            seen.add(source_game_id)
    games.sort(key=lambda item: (item.kickoff_time, item.source_game_id))
    return tuple(games), tuple(warnings)


def collect_historical_mlb_season(
    db: sqlite3.Connection,
    *,
    transport: HttpTransport,
    resolver: TeamResolver,
    season: int,
) -> dict[str, Any]:
    """Fetch and persist one MLB regular season without inventing old prices."""

    url = f"{MLB_HISTORY_URL}?sportId=1&season={int(season)}&gameType=R"
    response = transport.get(url, headers={"Accept": "application/json"})
    payload = json.loads(response.body.decode("utf-8"))
    games, warnings = parse_historical_mlb_schedule(payload, resolver=resolver, seasons=(season,))
    collected_at = response.received_at.astimezone(timezone.utc)
    checksum = hashlib.sha256(response.body).hexdigest()

    with db:
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO raw_ingests (
                source_key, endpoint_name, source_updated_at, collected_at,
                content_sha256, payload_json, http_status
            ) VALUES ('mlb_schedule', ?, NULL, ?, ?, ?, ?)
            """,
            (
                f"historical_mlb_{season}",
                _iso(collected_at),
                checksum,
                json.dumps({"season": int(season), "sha256": checksum, "source_url": url}),
                response.status,
            ),
        )
        ingest_id = cursor.lastrowid
        for item in games:
            db.execute(
                """
                INSERT INTO historical_mlb_games (
                    source_game_id, season, kickoff_time, home_team_id, away_team_id,
                    home_score, away_score, game_number, source_key, ingest_id,
                    source_updated_at, collected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'mlb_schedule', ?, NULL, ?)
                ON CONFLICT(source_game_id) DO UPDATE SET
                    season=excluded.season,
                    kickoff_time=excluded.kickoff_time,
                    home_team_id=excluded.home_team_id,
                    away_team_id=excluded.away_team_id,
                    home_score=excluded.home_score,
                    away_score=excluded.away_score,
                    game_number=excluded.game_number,
                    ingest_id=COALESCE(excluded.ingest_id, historical_mlb_games.ingest_id),
                    collected_at=excluded.collected_at
                """,
                (
                    item.source_game_id,
                    item.season,
                    _iso(item.kickoff_time),
                    item.home_team_id,
                    item.away_team_id,
                    item.home_score,
                    item.away_score,
                    item.game_number,
                    ingest_id,
                    _iso(collected_at),
                ),
            )

    return {
        "season": int(season),
        "received": len(games),
        "stored_total": db.execute(
            "SELECT COUNT(*) FROM historical_mlb_games WHERE season=?", (int(season),)
        ).fetchone()[0],
        "warnings": list(warnings),
        "historical_prices": "not_collected",
        "notice": (
            "Final results support time-ordered model development. Historical prediction-time "
            "moneyline prices are not reconstructed or invented."
        ),
    }
