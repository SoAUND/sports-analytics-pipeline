"""MLB Phase 8 live paper-run readiness and audit reporting.

This module does not place wagers.  It inspects the current MLB paper workflow
and records whether each scheduled game has the minimum evidence needed for a
paper decision.  Missing or stale information is reported explicitly and never
silently upgraded into a selection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import uuid
from typing import Any
from zoneinfo import ZoneInfo

REQUIRED_OFFICIAL_CHECKS = frozenset({"injuries", "lineups", "transactions", "probable_pitchers"})
MLB_OPERATION_TIME_ZONE = ZoneInfo("America/New_York")


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a time zone")
    return value.astimezone(timezone.utc).isoformat()


def _date_bounds(operation_date: str) -> tuple[str, str]:
    try:
        local_start = datetime.strptime(operation_date, "%Y-%m-%d").replace(
            tzinfo=MLB_OPERATION_TIME_ZONE
        )
    except ValueError as exc:
        raise ValueError("operation_date must use YYYY-MM-DD") from exc
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc).isoformat(),
        local_end.astimezone(timezone.utc).isoformat(),
    )


def _latest_open_moneyline_markets(
    db: sqlite3.Connection,
    game_id: str,
    as_of: datetime,
    stale_after_minutes: int,
) -> dict[str, list[str]]:
    cutoff = _iso(as_of)
    stale_cutoff = _iso(as_of - timedelta(minutes=stale_after_minutes))
    rows = db.execute(
        """
        SELECT sportsbook_key, outcome_team_id, source_updated_at, market_status,
               is_accessible, is_consensus_eligible
        FROM odds_snapshots
        WHERE game_id=? AND market_key='moneylines' AND period='full_game'
          AND source_updated_at <= ? AND collected_at <= ?
          AND source_updated_at >= ?
        ORDER BY source_updated_at DESC, odds_snapshot_id DESC
        """,
        (game_id, cutoff, cutoff, stale_cutoff),
    ).fetchall()
    # A usable market requires two distinct teams from the same book/update.
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if row["market_status"] != "open":
            continue
        group = grouped.setdefault(
            (row["sportsbook_key"], row["source_updated_at"]),
            {
                "sides": set(),
                "accessible": bool(row["is_accessible"]),
                "consensus": bool(row["is_consensus_eligible"]),
            },
        )
        group["sides"].add(row["outcome_team_id"])
    paired = [
        (book, state)
        for (book, _source_updated_at), state in grouped.items()
        if len(state["sides"]) >= 2
    ]
    return {
        "accessible": sorted({book for book, state in paired if state["accessible"]}),
        "consensus": sorted({book for book, state in paired if state["consensus"]}),
        "trustworthy": sorted(
            {
                book
                for book, state in paired
                if state["accessible"] or state["consensus"]
            }
        ),
    }


def _official_checks(db: sqlite3.Connection, game_id: str, as_of: datetime) -> set[str]:
    rows = db.execute(
        """
        SELECT check_type
        FROM source_checks
        WHERE game_id=? AND checked_at <= ? AND access_status='confirmed'
          AND source_key LIKE 'mlb_%'
        """,
        (game_id, _iso(as_of)),
    ).fetchall()
    return {str(row["check_type"]) for row in rows}


def _context_state(db: sqlite3.Connection, game_id: str, as_of: datetime) -> dict[str, bool]:
    row = db.execute(
        "SELECT * FROM mlb_game_context WHERE game_id=? AND collected_at <= ?",
        (game_id, _iso(as_of)),
    ).fetchone()
    if row is None:
        return {"context_present": False, "starters_confirmed": False, "lineups_confirmed": False}
    starters = bool(row["confirmed_home_pitcher_id"] and row["confirmed_away_pitcher_id"])
    lineups = (
        str(row["home_lineup_status"] or "").casefold() == "confirmed"
        and str(row["away_lineup_status"] or "").casefold() == "confirmed"
    )
    return {"context_present": True, "starters_confirmed": starters, "lineups_confirmed": lineups}


def _reviewed_context(db: sqlite3.Connection, game_id: str, as_of: datetime) -> dict[str, Any]:
    row = db.execute(
        """
        SELECT research_complete, market_match_confirmed,
               material_information_complete, conflict_status
        FROM evaluation_contexts
        WHERE game_id=? AND as_of <= ? AND created_at <= ?
        ORDER BY as_of DESC, created_at DESC, rowid DESC LIMIT 1
        """,
        (game_id, _iso(as_of), _iso(as_of)),
    ).fetchone()
    if row is None:
        return {
            "present": False,
            "complete": False,
            "material_information_complete": False,
            "market_match_confirmed": False,
            "conflict_clear": False,
        }
    conflict_clear = str(row["conflict_status"]).casefold() in {
        "none",
        "clear",
        "no_conflict",
    }
    return {
        "present": True,
        "complete": bool(
            row["research_complete"]
            and row["material_information_complete"]
            and conflict_clear
        ),
        "material_information_complete": bool(row["material_information_complete"]),
        "market_match_confirmed": bool(row["market_match_confirmed"]),
        "conflict_clear": conflict_clear,
    }


def build_live_paper_readiness(
    db: sqlite3.Connection,
    *,
    operation_date: str,
    as_of: datetime,
    stale_after_minutes: int = 20,
) -> dict[str, Any]:
    if stale_after_minutes <= 0:
        raise ValueError("stale_after_minutes must be positive")
    start, end = _date_bounds(operation_date)
    games = db.execute(
        """
        SELECT game_id, kickoff_time, home_team_id, away_team_id, status
        FROM games
        WHERE sport='mlb' AND season_type='regular'
          AND kickoff_time >= ? AND kickoff_time < ?
        ORDER BY kickoff_time, game_id
        """,
        (start, end),
    ).fetchall()

    game_reports: list[dict[str, Any]] = []
    ready = model_ready = blocked = decision_count = selection_count = pass_count = 0
    for game in games:
        game_id = str(game["game_id"])
        context = _context_state(db, game_id, as_of)
        checks = _official_checks(db, game_id, as_of)
        missing_checks = sorted(REQUIRED_OFFICIAL_CHECKS - checks)
        markets = _latest_open_moneyline_markets(
            db, game_id, as_of, stale_after_minutes
        )
        accessible_books = markets["accessible"]
        consensus_books = markets["consensus"]
        trustworthy_books = markets["trustworthy"]
        reviewed = _reviewed_context(db, game_id, as_of)
        decisions = db.execute(
            "SELECT decision_type FROM paper_decisions WHERE game_id=? ORDER BY created_at",
            (game_id,),
        ).fetchall()
        decision_count += len(decisions)
        selection_count += sum(row["decision_type"] == "test_only_selection" for row in decisions)
        pass_count += sum(row["decision_type"] == "pass" for row in decisions)

        blockers: list[str] = []
        if game["status"] not in {"scheduled", "final"}:
            blockers.append(f"game_status_{game['status']}")
        if not trustworthy_books:
            blockers.append("no_fresh_trustworthy_two_sided_moneyline")
        if missing_checks:
            blockers.append("missing_official_checks:" + ",".join(missing_checks))
        if not context["starters_confirmed"]:
            blockers.append("starting_pitchers_unconfirmed")
        if not context["lineups_confirmed"]:
            blockers.append("lineups_unconfirmed")
        if not reviewed["complete"]:
            blockers.append("reviewed_research_context_incomplete")
        if len(decisions) > 1:
            blockers.append("duplicate_paper_decisions")

        if not blockers:
            status = "ready"
        else:
            status = "blocked"
        if status == "ready":
            ready += 1
        else:
            blocked += 1
        game_reports.append({
            "game_id": game_id,
            "kickoff_time": game["kickoff_time"],
            "home_team_id": game["home_team_id"],
            "away_team_id": game["away_team_id"],
            "status": status,
            "fresh_accessible_two_sided_markets": len(accessible_books),
            "fresh_accessible_bookmakers": accessible_books,
            "fresh_consensus_two_sided_markets": len(consensus_books),
            "fresh_consensus_bookmakers": consensus_books,
            "fresh_trustworthy_two_sided_markets": len(trustworthy_books),
            "fresh_trustworthy_bookmakers": trustworthy_books,
            "accessible_price_verification_required": False,
            "bookmaker_coverage_warnings": (
                ["No configured accessible bookmaker returned a complete fresh pair."]
                if not accessible_books and trustworthy_books
                else []
            ),
            "official_checks_confirmed": sorted(checks & REQUIRED_OFFICIAL_CHECKS),
            "missing_official_checks": missing_checks,
            **context,
            "reviewed_research_context_complete": reviewed["complete"],
            "material_research_complete_without_price": reviewed[
                "material_information_complete"
            ] and reviewed["conflict_clear"],
            "paper_decisions": len(decisions),
            "blockers": blockers,
        })

    return {
        "mode": "test_only",
        "sport": "mlb",
        "phase": 8,
        "operation_date": operation_date,
        "as_of": _iso(as_of),
        "scheduled_regular_season_games": len(games),
        "ready_games": ready,
        "model_ready_price_verification_required_games": model_ready,
        "blocked_games": blocked,
        "paper_decisions": decision_count,
        "test_only_selections": selection_count,
        "passes": pass_count,
        "games": game_reports,
        "automatic_wagering": False,
        "official_pick_enabled": False,
        "notice": (
            "Phase 8 is a live paper-run audit only. A missing individual bookmaker is a coverage warning, not a strategy failure. Missing research, lineup, starter, source, or every fresh trustworthy two-sided market blocks the game; no wager or official pick is created."
        ),
    }


def store_live_paper_run(
    db: sqlite3.Connection,
    *,
    report: dict[str, Any],
    operations_run_id: str | None = None,
    recorded_at: datetime | None = None,
) -> str:
    recorded_at = recorded_at or datetime.now(timezone.utc)
    run_id = str(uuid.uuid4())
    status = "ready" if report["scheduled_regular_season_games"] > 0 and report["blocked_games"] == 0 else "blocked"
    if report["scheduled_regular_season_games"] == 0:
        status = "no_games"
    with db:
        db.execute(
            """
            INSERT INTO mlb_live_paper_runs(
                live_run_id, operation_date, operations_run_id, mode, status,
                scheduled_games, ready_games, blocked_games, paper_decisions,
                report_json, recorded_at
            ) VALUES (?, ?, ?, 'test_only', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                report["operation_date"],
                operations_run_id,
                status,
                int(report["scheduled_regular_season_games"]),
                int(report["ready_games"]),
                int(report["blocked_games"]),
                int(report["paper_decisions"]),
                json.dumps(report, sort_keys=True),
                _iso(recorded_at),
            ),
        )
    return run_id


def latest_live_paper_run(db: sqlite3.Connection) -> dict[str, Any] | None:
    row = db.execute(
        """
        SELECT * FROM mlb_live_paper_runs
        ORDER BY recorded_at DESC, rowid DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    report = json.loads(row["report_json"])
    report.update({
        "live_run_id": row["live_run_id"],
        "operations_run_id": row["operations_run_id"],
        "status": row["status"],
        "recorded_at": row["recorded_at"],
    })
    return report


def latest_live_paper_run_for_date(
    db: sqlite3.Connection, operation_date: str
) -> dict[str, Any] | None:
    """Return the newest stored readiness audit for one explicit MLB date."""
    _date_bounds(operation_date)
    row = db.execute(
        """
        SELECT * FROM mlb_live_paper_runs
        WHERE operation_date=?
        ORDER BY recorded_at DESC, rowid DESC LIMIT 1
        """,
        (operation_date,),
    ).fetchone()
    if row is None:
        return None
    report = json.loads(row["report_json"])
    report.update({
        "live_run_id": row["live_run_id"],
        "operations_run_id": row["operations_run_id"],
        "status": row["status"],
        "recorded_at": row["recorded_at"],
    })
    return report


def ready_game_ids_from_report(report: dict[str, Any]) -> tuple[str, ...]:
    """Extract only explicitly ready game IDs from a stored readiness report."""
    games = report.get("games") or []
    return tuple(
        str(game["game_id"])
        for game in games
        if game.get("status") == "ready" and game.get("game_id")
    )


def model_eligible_game_ids_from_report(report: dict[str, Any]) -> tuple[str, ...]:
    """Return games with complete research and a trustworthy two-sided market."""
    allowed = {"ready"}
    return tuple(
        str(game["game_id"])
        for game in report.get("games") or []
        if game.get("status") in allowed and game.get("game_id")
    )
