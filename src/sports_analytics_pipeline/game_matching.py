"""Strict sport-aware game identity helpers."""

from __future__ import annotations

import sqlite3


class GameMatchError(ValueError):
    pass


def strict_game_identity_match(
    db: sqlite3.Connection,
    *,
    sport: str,
    season: int,
    home_team_id: str,
    away_team_id: str,
    kickoff_time: str,
    stadium_id: str,
) -> sqlite3.Row | None:
    """Return one exact game or None; multiple matches are an integrity error."""
    rows = db.execute(
        """
        SELECT * FROM games
        WHERE sport = ? AND season = ? AND home_team_id = ? AND away_team_id = ?
          AND kickoff_time = ? AND stadium_id = ?
        """,
        (sport, season, home_team_id, away_team_id, kickoff_time, stadium_id),
    ).fetchall()
    if len(rows) > 1:
        raise GameMatchError("Ambiguous game identity")
    return rows[0] if rows else None
