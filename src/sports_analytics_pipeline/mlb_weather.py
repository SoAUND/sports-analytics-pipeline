"""MLB game-window weather collection for live paper readiness.

Only weather-exposed U.S. venues are queried through the National Weather
Service.  Indoor/non-exposed venues are explicitly skipped.  Any unavailable
or failed required weather remains visible to the research gate and therefore
blocks readiness instead of being guessed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Callable, Iterable, Any

from .collectors.nws import NwsCollector


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a time zone")
    return value.astimezone(timezone.utc).isoformat()


def collect_required_mlb_weather(
    db: sqlite3.Connection,
    *,
    repo: Any,
    transport: Any,
    user_agent: str,
    game_ids: Iterable[str],
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    collector_factory: Callable[..., Any] = NwsCollector,
) -> list[dict[str, Any]]:
    """Collect NWS game-window forecasts for weather-exposed U.S. MLB games.

    Failures are recorded per game and returned to the caller.  They do not get
    converted into synthetic weather; the later research/readiness gate sees
    the missing snapshot and blocks the game.
    """
    results: list[dict[str, Any]] = []
    for game_id in tuple(dict.fromkeys(str(value) for value in game_ids)):
        row = db.execute(
            """
            SELECT g.game_id, g.kickoff_time, g.roof_status,
                   s.latitude, s.longitude, s.weather_exposed,
                   s.country_code, s.roof_type
            FROM games g JOIN stadiums s ON s.stadium_id=g.stadium_id
            WHERE g.game_id=? AND g.sport='mlb'
            """,
            (game_id,),
        ).fetchone()
        if row is None:
            results.append({"game_id": game_id, "status": "failed", "reason": "unknown_mlb_game"})
            continue

        run_id = repo.begin_run("nws", f"mlb_forecast_{game_id}", now_fn())
        if not row["weather_exposed"]:
            repo.finish_run(
                run_id,
                finished_at=now_fn(),
                status="skipped",
                warnings=("Outdoor weather is not required for this MLB venue",),
            )
            results.append({
                "game_id": game_id,
                "status": "skipped",
                "reason": "weather_not_required_for_non_exposed_venue",
            })
            continue
        if row["country_code"] != "US":
            repo.finish_run(
                run_id,
                finished_at=now_fn(),
                status="skipped",
                warnings=("NWS does not cover this weather-exposed non-U.S. MLB venue",),
            )
            results.append({
                "game_id": game_id,
                "status": "blocked",
                "reason": "approved_weather_provider_required_for_non_us_venue",
            })
            continue

        try:
            collector = collector_factory(transport=transport, user_agent=user_agent)
            kickoff = datetime.fromisoformat(str(row["kickoff_time"]).replace("Z", "+00:00"))
            batch = collector.collect_forecast(
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                kickoff_time=kickoff,
            )
            stored = repo.persist_weather(game_id, batch, row["roof_status"] or row["roof_type"] or "unknown")
            repo.finish_run(
                run_id,
                finished_at=now_fn(),
                status="succeeded",
                received=1,
                stored=stored,
            )
            results.append({"game_id": game_id, "status": "succeeded", "stored": stored})
        except Exception as exc:
            repo.finish_run(
                run_id,
                finished_at=now_fn(),
                status="failed",
                error_message=str(exc),
            )
            results.append({"game_id": game_id, "status": "failed", "reason": str(exc)})
    return results
