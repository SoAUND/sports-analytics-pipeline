"""Prediction-time MLB research-context builder for live paper tracking.

The builder uses only evidence that existed at or before ``as_of``. It does not
upgrade page reachability into player facts. Confirmed lineups/starters come from
structured MLB schedule data; recent form comes from official final MLB results;
transactions/IL moves come from the structured MLB transactions collector.

If any material item is missing, stale, conflicting, or future-dated, the stored
evaluation context remains incomplete so the evaluator/live-readiness layer must
pass rather than force a selection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import uuid
from typing import Any

REQUIRED_PAGE_CHECKS = frozenset({"injuries", "lineups", "transactions", "probable_pitchers"})


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a time zone")
    return value.astimezone(timezone.utc).isoformat()


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored timestamp is missing a time zone")
    return parsed.astimezone(timezone.utc)


def _latest_game_context(db: sqlite3.Connection, game_id: str, as_of: datetime):
    return db.execute(
        """
        SELECT * FROM mlb_game_context
        WHERE game_id=? AND collected_at IS NOT NULL AND collected_at <= ?
        ORDER BY collected_at DESC
        LIMIT 1
        """,
        (game_id, _iso(as_of)),
    ).fetchone()


def _latest_page_checks(db: sqlite3.Connection, game_id: str, as_of: datetime, freshness_minutes: int) -> dict[str, sqlite3.Row]:
    rows = db.execute(
        """
        SELECT * FROM source_checks
        WHERE game_id=? AND source_key LIKE 'mlb_%' AND checked_at <= ?
        ORDER BY checked_at DESC, source_check_id DESC
        """,
        (game_id, _iso(as_of)),
    ).fetchall()
    latest: dict[str, sqlite3.Row] = {}
    cutoff = as_of.astimezone(timezone.utc) - timedelta(minutes=freshness_minutes)
    for row in rows:
        check_type = str(row["check_type"])
        if check_type not in REQUIRED_PAGE_CHECKS:
            continue
        if _dt(str(row["checked_at"])) < cutoff:
            continue
        latest.setdefault(check_type, row)
    return latest


def _latest_transactions_run(db: sqlite3.Connection, as_of: datetime, freshness_hours: int):
    row = db.execute(
        """
        SELECT * FROM collection_runs
        WHERE source_key='mlb_transactions'
          AND status IN ('succeeded','partial')
          AND finished_at IS NOT NULL AND finished_at <= ?
        ORDER BY finished_at DESC, started_at DESC
        LIMIT 1
        """,
        (_iso(as_of),),
    ).fetchone()
    if row is None:
        return None
    finished = _dt(str(row["finished_at"]))
    if as_of.astimezone(timezone.utc) - finished > timedelta(hours=freshness_hours):
        return None
    return row


def _recent_results_collection_fresh(
    db: sqlite3.Connection, as_of: datetime, freshness_hours: int
) -> bool:
    row = db.execute(
        """
        SELECT collected_at FROM raw_ingests
        WHERE source_key='mlb_schedule' AND endpoint_name LIKE 'recent_results_%'
          AND collected_at <= ?
        ORDER BY collected_at DESC, ingest_id DESC
        LIMIT 1
        """,
        (_iso(as_of),),
    ).fetchone()
    if row is None:
        return False
    return as_of.astimezone(timezone.utc) - _dt(str(row["collected_at"])) <= timedelta(hours=freshness_hours)


def _fresh_market_pair(db: sqlite3.Connection, game_id: str, as_of: datetime, stale_after_minutes: int) -> bool:
    rows = db.execute(
        """
        SELECT sportsbook_key, outcome_team_id, source_updated_at, settlement_rule,
               market_status, is_accessible, is_consensus_eligible
        FROM odds_snapshots
        WHERE game_id=? AND market_key='moneylines' AND period='full_game'
          AND source_updated_at <= ? AND collected_at <= ?
          AND source_updated_at >= ?
        ORDER BY source_updated_at DESC, odds_snapshot_id DESC
        """,
        (
            game_id,
            _iso(as_of),
            _iso(as_of),
            _iso(as_of - timedelta(minutes=stale_after_minutes)),
        ),
    ).fetchall()
    grouped: dict[tuple[str, str, str], set[str]] = {}
    for row in rows:
        if row["market_status"] != "open" or not (
            row["is_accessible"] or row["is_consensus_eligible"]
        ):
            continue
        key = (str(row["sportsbook_key"]), str(row["source_updated_at"]), str(row["settlement_rule"]))
        grouped.setdefault(key, set()).add(str(row["outcome_team_id"]))
    return any(len(sides) >= 2 for sides in grouped.values())


def _team_games(db: sqlite3.Connection, team_id: str, *, before: datetime) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT source_game_id, kickoff_time, home_team_id, away_team_id, home_score, away_score
        FROM historical_mlb_games
        WHERE kickoff_time < ? AND (home_team_id=? OR away_team_id=?)
        ORDER BY kickoff_time DESC, source_game_id DESC
        LIMIT 20
        """,
        (_iso(before), team_id, team_id),
    ).fetchall()


def _won(row: sqlite3.Row, team_id: str) -> bool:
    if row["home_team_id"] == team_id:
        return int(row["home_score"]) > int(row["away_score"])
    return int(row["away_score"]) > int(row["home_score"])


def _recent_form(rows: list[sqlite3.Row], team_id: str, count: int) -> float | None:
    sample = rows[:count]
    if len(sample) < count:
        return None
    return sum(_won(row, team_id) for row in sample) / count


def _venue_form(rows: list[sqlite3.Row], team_id: str, *, home: bool, count: int = 10) -> tuple[int, int]:
    filtered = [
        row for row in rows
        if (row["home_team_id"] == team_id) == home
    ][:count]
    return sum(_won(row, team_id) for row in filtered), len(filtered)


def _head_to_head(db: sqlite3.Connection, home_id: str, away_id: str, before: datetime) -> tuple[int, int, int]:
    rows = db.execute(
        """
        SELECT home_team_id, away_team_id, home_score, away_score
        FROM historical_mlb_games
        WHERE kickoff_time < ?
          AND ((home_team_id=? AND away_team_id=?) OR (home_team_id=? AND away_team_id=?))
        ORDER BY kickoff_time DESC, source_game_id DESC
        LIMIT 10
        """,
        (_iso(before), home_id, away_id, away_id, home_id),
    ).fetchall()
    return len(rows), sum(_won(row, home_id) for row in rows), sum(_won(row, away_id) for row in rows)


def _rest_days(rows: list[sqlite3.Row], kickoff: datetime) -> int | None:
    if not rows:
        return None
    last = _dt(str(rows[0]["kickoff_time"]))
    days = (kickoff.date() - last.date()).days - 1
    return max(0, days)


def _late_material_changes(
    db: sqlite3.Connection,
    *,
    home_team_id: str,
    away_team_id: str,
    context_collected_at: datetime,
    as_of: datetime,
) -> list[str]:
    rows = db.execute(
        """
        SELECT player_name, transaction_type, description, source_updated_at
        FROM transaction_snapshots
        WHERE source_key='mlb_transactions'
          AND source_updated_at > ? AND source_updated_at <= ?
          AND (from_team_id IN (?, ?) OR to_team_id IN (?, ?))
        ORDER BY source_updated_at, transaction_snapshot_id
        """,
        (
            _iso(context_collected_at), _iso(as_of),
            home_team_id, away_team_id, home_team_id, away_team_id,
        ),
    ).fetchall()
    return [
        f"Late official transaction after lineup collection: {row['player_name']} - {row['description']}"
        for row in rows
    ]


def _weather_gate(db: sqlite3.Connection, game: sqlite3.Row, as_of: datetime) -> tuple[bool, str]:
    stadium = db.execute(
        "SELECT weather_exposed, roof_type FROM stadiums WHERE stadium_id=?",
        (game["stadium_id"],),
    ).fetchone()
    if stadium is None:
        return False, "stadium_weather_metadata_missing"
    if not stadium["weather_exposed"]:
        return True, "weather_not_required_for_non_exposed_venue"
    kickoff = _dt(str(game["kickoff_time"]))
    row = db.execute(
        """
        SELECT forecast_for, source_updated_at, collected_at
        FROM weather_snapshots
        WHERE game_id=? AND source_updated_at <= ? AND collected_at <= ?
        ORDER BY source_updated_at DESC, weather_snapshot_id DESC
        LIMIT 1
        """,
        (game["game_id"], _iso(as_of), _iso(as_of)),
    ).fetchone()
    if row is None:
        return False, "outdoor_weather_missing"
    if as_of.astimezone(timezone.utc) - _dt(str(row["collected_at"])) > timedelta(hours=6):
        return False, "outdoor_weather_stale"
    forecast_for = _dt(str(row["forecast_for"]))
    if abs(forecast_for - kickoff) > timedelta(hours=4):
        return False, "weather_not_tied_to_game_window"
    return True, "outdoor_weather_confirmed"


def build_and_store_mlb_research_context(
    db: sqlite3.Connection,
    *,
    game_id: str,
    as_of: datetime,
    stale_after_minutes: int = 60,
    transaction_freshness_hours: int = 12,
    page_freshness_minutes: int = 90,
    lineup_freshness_minutes: int = 90,
    recent_results_freshness_hours: int = 12,
) -> dict[str, Any]:
    """Create one immutable reviewed MLB context from evidence available by ``as_of``."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include a time zone")
    if stale_after_minutes <= 0:
        raise ValueError("stale_after_minutes must be positive")
    if transaction_freshness_hours <= 0:
        raise ValueError("transaction_freshness_hours must be positive")
    if page_freshness_minutes <= 0 or lineup_freshness_minutes <= 0:
        raise ValueError("research freshness windows must be positive")
    if recent_results_freshness_hours <= 0:
        raise ValueError("recent_results_freshness_hours must be positive")
    as_of = as_of.astimezone(timezone.utc)

    game = db.execute(
        "SELECT * FROM games WHERE game_id=? AND sport='mlb' AND season_type='regular'",
        (game_id,),
    ).fetchone()
    if game is None:
        raise ValueError("game_id must identify one MLB regular-season game")
    kickoff = _dt(str(game["kickoff_time"]))
    if as_of > kickoff:
        raise ValueError("MLB research context must be created no later than first pitch")

    context = _latest_game_context(db, game_id, as_of)
    page_checks = _latest_page_checks(db, game_id, as_of, page_freshness_minutes)
    transaction_run = _latest_transactions_run(db, as_of, transaction_freshness_hours)
    market_ok = _fresh_market_pair(db, game_id, as_of, stale_after_minutes)

    blockers: list[str] = []
    notes: list[str] = [
        "Research context uses prediction-time evidence only; no future information is permitted."
    ]

    if context is None:
        blockers.append("structured_mlb_game_context_missing_by_cutoff")
        starters_confirmed = False
        lineups_confirmed = False
        context_time = None
    else:
        context_time = _dt(str(context["collected_at"]))
        if as_of - context_time > timedelta(minutes=lineup_freshness_minutes):
            blockers.append("structured_lineup_context_stale")
        starters_confirmed = bool(context["confirmed_home_pitcher_id"] and context["confirmed_away_pitcher_id"])
        lineups_confirmed = (
            str(context["home_lineup_status"] or "").casefold() == "confirmed"
            and str(context["away_lineup_status"] or "").casefold() == "confirmed"
        )
        if not starters_confirmed:
            blockers.append("starting_pitchers_unconfirmed")
        if not lineups_confirmed:
            blockers.append("lineups_unconfirmed")

    missing_checks = sorted(
        check for check in REQUIRED_PAGE_CHECKS
        if check not in page_checks or page_checks[check]["access_status"] != "confirmed"
    )
    if missing_checks:
        blockers.append("missing_required_official_page_checks:" + ",".join(missing_checks))
    else:
        notes.append("Official MLB injury, lineup, transaction, and probable-pitcher pages were checked.")
        notes.append("Page reachability is treated only as a source check; structured lineups/starters/transactions provide player-level evidence.")

    if transaction_run is None:
        blockers.append("fresh_structured_transactions_collection_missing")
    else:
        notes.append("Official structured MLB transaction/IL collection is fresh at the research cutoff.")

    if not market_ok:
        blockers.append("fresh_accessible_two_sided_moneyline_missing")
    else:
        notes.append("A fresh exact two-sided accessible MLB moneyline existed by the research cutoff.")

    recent_results_fresh = _recent_results_collection_fresh(
        db, as_of, recent_results_freshness_hours
    )
    if not recent_results_fresh:
        blockers.append("fresh_official_recent_results_collection_missing")
    else:
        notes.append("Official recent-result data was refreshed within the required research window.")

    home_rows = _team_games(db, str(game["home_team_id"]), before=as_of)
    away_rows = _team_games(db, str(game["away_team_id"]), before=as_of)
    home5 = _recent_form(home_rows, str(game["home_team_id"]), 5)
    away5 = _recent_form(away_rows, str(game["away_team_id"]), 5)
    home10 = _recent_form(home_rows, str(game["home_team_id"]), 10)
    away10 = _recent_form(away_rows, str(game["away_team_id"]), 10)
    if None in (home5, away5, home10, away10):
        blockers.append("official_recent_5_and_10_form_incomplete")
    else:
        notes.append(
            f"Official recent-form win rates: home team L5={home5:.3f}, L10={home10:.3f}; "
            f"away team L5={away5:.3f}, L10={away10:.3f}."
        )

    home_home_wins, home_home_n = _venue_form(home_rows, str(game["home_team_id"]), home=True)
    away_away_wins, away_away_n = _venue_form(away_rows, str(game["away_team_id"]), home=False)
    notes.append(
        f"Home/away check: home team {home_home_wins}-{home_home_n-home_home_wins} in its last {home_home_n} stored home games; "
        f"away team {away_away_wins}-{away_away_n-away_away_wins} in its last {away_away_n} stored road games."
    )

    h2h_n, h2h_home_wins, h2h_away_wins = _head_to_head(
        db, str(game["home_team_id"]), str(game["away_team_id"]), as_of
    )
    notes.append(
        f"Opponent-history check: {h2h_n} prior stored meetings; current home team {h2h_home_wins} wins, current away team {h2h_away_wins} wins."
    )

    home_rest = _rest_days(home_rows, kickoff)
    away_rest = _rest_days(away_rows, kickoff)
    if home_rest is None or away_rest is None:
        blockers.append("rest_schedule_history_incomplete")
    else:
        notes.append(f"Rest check: home rest days={home_rest}; away rest days={away_rest}.")

    weather_ok, weather_note = _weather_gate(db, game, as_of)
    if not weather_ok:
        blockers.append(weather_note)
    notes.append("Weather gate: " + weather_note + ".")

    late_changes: list[str] = []
    if context_time is not None:
        late_changes = _late_material_changes(
            db,
            home_team_id=str(game["home_team_id"]),
            away_team_id=str(game["away_team_id"]),
            context_collected_at=context_time,
            as_of=as_of,
        )
        if late_changes:
            blockers.append("official_transaction_after_lineup_collection")
            notes.extend(late_changes)

    conflict_status = "conflict" if late_changes else "clear"
    material_complete = not any(
        blocker for blocker in blockers
        if blocker not in {"fresh_accessible_two_sided_moneyline_missing"}
    )
    research_complete = material_complete and market_ok and conflict_status == "clear"

    if context is not None:
        merged_notes: list[str] = []
        try:
            merged_notes.extend(json.loads(context["source_notes_json"] or "[]"))
        except (TypeError, json.JSONDecodeError):
            merged_notes.append("Existing MLB context source notes were unreadable JSON.")
        merged_notes.extend(notes)
        with db:
            db.execute(
                """
                UPDATE mlb_game_context
                SET home_recent_5=?, away_recent_5=?, home_recent_10=?, away_recent_10=?,
                    home_rest_days=?, away_rest_days=?, source_notes_json=?
                WHERE game_id=?
                """,
                (
                    home5, away5, home10, away10, home_rest, away_rest,
                    json.dumps(merged_notes, sort_keys=True), game_id,
                ),
            )

    context_id = str(uuid.uuid4())
    cutoff = _iso(as_of)
    source_notes = notes + (["Blockers: " + "; ".join(blockers)] if blockers else ["No material research blocker remained at cutoff."])
    information_quality = "full_official" if research_complete else "missing_or_conflicting"
    with db:
        existing = db.execute(
            "SELECT evaluation_context_id FROM evaluation_contexts WHERE game_id=? AND as_of=?",
            (game_id, cutoff),
        ).fetchone()
        if existing is not None:
            raise ValueError("An MLB evaluation context already exists for this exact game/cutoff")
        db.execute(
            """
            INSERT INTO evaluation_contexts (
                evaluation_context_id, game_id, as_of, research_complete,
                market_match_confirmed, material_information_complete,
                information_quality, conflict_status, exposure_status,
                stability_status, manual_approval_status, source_notes_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'clear', 'unknown', 'pending', ?, ?)
            """,
            (
                context_id, game_id, cutoff, int(research_complete), int(market_ok),
                int(material_complete), information_quality, conflict_status,
                json.dumps(source_notes, sort_keys=True), cutoff,
            ),
        )
        db.execute(
            """
            INSERT INTO source_checks (
                game_id, source_key, check_type, access_status, checked_at,
                missing_information, reviewer_notes, source_url
            ) VALUES (?, 'mlb_schedule', 'recent_form', ?, ?, ?, ?, ?)
            """,
            (
                game_id,
                "confirmed" if None not in (home5, away5, home10, away10) else "unavailable",
                cutoff,
                None if None not in (home5, away5, home10, away10) else "At least one team lacked 10 completed pregame results.",
                "Recent 5/10, home/away, opponent history, and rest were derived from official completed MLB results stored before the cutoff.",
                "https://www.mlb.com/schedule",
            ),
        )
        db.execute(
            """
            INSERT INTO audit_log (
                entity_type, entity_id, action, reason, new_value_json,
                responsible_actor, occurred_at
            ) VALUES ('evaluation_context', ?, 'create', ?, ?, 'mlb_research_builder', ?)
            """,
            (
                context_id,
                "Created prediction-time MLB reviewed research context.",
                json.dumps({
                    "game_id": game_id,
                    "research_complete": research_complete,
                    "material_information_complete": material_complete,
                    "market_match_confirmed": market_ok,
                    "conflict_status": conflict_status,
                    "blockers": blockers,
                }, sort_keys=True),
                cutoff,
            ),
        )

    return {
        "evaluation_context_id": context_id,
        "game_id": game_id,
        "as_of": cutoff,
        "research_complete": research_complete,
        "material_information_complete": material_complete,
        "market_match_confirmed": market_ok,
        "conflict_status": conflict_status,
        "information_quality": information_quality,
        "starting_pitchers_confirmed": starters_confirmed,
        "lineups_confirmed": lineups_confirmed,
        "recent_form_complete": None not in (home5, away5, home10, away10),
        "weather_complete": weather_ok,
        "blockers": blockers,
        "automatic_wagering": False,
        "official_pick_enabled": False,
    }


def build_and_store_mlb_research_contexts(
    db: sqlite3.Connection,
    *,
    game_ids: list[str] | tuple[str, ...],
    as_of: datetime,
    stale_after_minutes: int = 60,
    transaction_freshness_hours: int = 12,
    page_freshness_minutes: int = 90,
    lineup_freshness_minutes: int = 90,
    recent_results_freshness_hours: int = 12,
) -> list[dict[str, Any]]:
    return [
        build_and_store_mlb_research_context(
            db,
            game_id=game_id,
            as_of=as_of,
            stale_after_minutes=stale_after_minutes,
            transaction_freshness_hours=transaction_freshness_hours,
            page_freshness_minutes=page_freshness_minutes,
            lineup_freshness_minutes=lineup_freshness_minutes,
            recent_results_freshness_hours=recent_results_freshness_hours,
        )
        for game_id in game_ids
    ]
