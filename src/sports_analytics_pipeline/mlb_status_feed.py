"""Structured status-feed reporting for the test-only MLB paper workflow.

The GitHub workflow publishes these payloads to one permanent issue so an
external monitor can read a known location without discovering Actions run IDs.
This module is reporting-only: it never creates a real-money action, stake, or model
approval.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from .paper_money import paper_unit_dollars, units_to_dollars
from .pick_grades import format_pick_strength, pick_strength_grade

FEED_VERSION = 1
STATUS_MARKER = "our-mlb-status-feed:v1"
TARGET_DECISIONS = 100


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bool(value: bool | str) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("status-feed timestamps must include a time zone")
    return parsed.astimezone(timezone.utc)


def _round(value: Any, places: int = 4) -> float | None:
    return None if value is None else round(float(value), places)


def decimal_to_american(decimal_odds: float | None) -> int | None:
    if decimal_odds is None:
        return None
    odds = float(decimal_odds)
    if odds <= 1.0:
        return None
    raw = (odds - 1.0) * 100.0 if odds >= 2.0 else -100.0 / (odds - 1.0)
    return int(round(raw))


def hypothetical_payouts(decimal_odds: float | None) -> dict[str, dict[str, float]]:
    """Return informational $2-$5 profit/return examples for a recorded paper price."""
    if decimal_odds is None or float(decimal_odds) <= 1.0:
        return {}
    odds = float(decimal_odds)
    return {
        f"${stake}": {
            "profit_if_win": round(stake * (odds - 1.0), 2),
            "total_return_if_win": round(stake * odds, 2),
        }
        for stake in range(2, 6)
    }


def build_gate_payload(
    *,
    workflow_run_id: str,
    workflow_run_attempt: str | int,
    operation_date: str,
    automated: bool | str,
    run_mode: str,
    checked_at: str | None = None,
) -> dict[str, Any]:
    return {
        "feed_version": FEED_VERSION,
        "event_type": "paper_run_gate_due",
        "workflow_run_id": str(workflow_run_id),
        "workflow_run_attempt": int(workflow_run_attempt),
        "recorded_at": checked_at or _utc_now(),
        "operation_date": operation_date,
        "automated": _bool(automated),
        "run_mode": run_mode,
        "full_paper_workflow_due": True,
        "mode": "test_only",
        "sport": "mlb",
        "automatic_wagering": False,
        "notice": "A test-only MLB paper workflow is due. No real-money action is taken.",
    }


def _latest_current_operation(
    db: sqlite3.Connection, *, operation_date: str, checked_at: str
) -> sqlite3.Row | None:
    row = db.execute(
        """
        SELECT run_id, operation_date, mode, status, started_at, completed_at,
               failed_step, summary_json
        FROM mlb_operations_runs
        WHERE operation_date=?
        ORDER BY started_at DESC, rowid DESC
        LIMIT 1
        """,
        (operation_date,),
    ).fetchone()
    if row is None:
        return None
    start = _parse_time(row["started_at"])
    gate = _parse_time(checked_at)
    if start is None or gate is None or start < gate:
        return None
    return row


def _latest_live_report(
    db: sqlite3.Connection,
    *,
    operation_date: str,
    operation_started_at: str,
) -> dict[str, Any] | None:
    rows = db.execute(
        """
        SELECT live_run_id, operations_run_id, status, scheduled_games,
               ready_games, blocked_games, paper_decisions, report_json, recorded_at
        FROM mlb_live_paper_runs
        WHERE operation_date=?
        ORDER BY recorded_at DESC, rowid DESC
        """,
        (operation_date,),
    ).fetchall()
    start = _parse_time(operation_started_at)
    for row in rows:
        recorded = _parse_time(row["recorded_at"])
        if start is not None and recorded is not None and recorded < start:
            continue
        report = json.loads(row["report_json"] or "{}")
        games = report.get("games") or []
        blocked_details = []
        first_blocker = None
        for game in games:
            blockers = list(game.get("blockers") or [])
            if blockers and first_blocker is None:
                first_blocker = blockers[0]
            if blockers:
                blocked_details.append(
                    {
                        "game_id": game.get("game_id"),
                        "kickoff_time": game.get("kickoff_time"),
                        "blockers": blockers,
                    }
                )
        return {
            "live_run_id": row["live_run_id"],
            "operations_run_id": row["operations_run_id"],
            "status": row["status"],
            "scheduled_games": int(row["scheduled_games"]),
            "ready_games": int(row["ready_games"]),
            "blocked_games": int(row["blocked_games"]),
            "model_ready_price_verification_required_games": int(
                report.get("model_ready_price_verification_required_games") or 0
            ),
            "paper_decisions_seen": int(row["paper_decisions"]),
            "test_only_selections_seen": int(report.get("test_only_selections") or 0),
            "passes_seen": int(report.get("passes") or 0),
            "first_blocker": first_blocker,
            "blocked_details": blocked_details,
            "recorded_at": row["recorded_at"],
        }
    return None


def _latest_odds_diagnostics(
    db: sqlite3.Connection, *, started_at: str, completed_at: str | None
) -> dict[str, Any] | None:
    params: list[Any] = [started_at]
    end_clause = ""
    if completed_at:
        end_clause = " AND started_at <= ?"
        params.append(completed_at)
    run = db.execute(
        f"""
        SELECT source_key, endpoint_name, started_at, finished_at, status,
               records_received, records_stored, warnings_json,
               requests_remaining, requests_used, request_cost
        FROM collection_runs
        WHERE source_key='the_odds_api' AND endpoint_name='current_mlb_odds'
          AND started_at >= ? {end_clause}
        ORDER BY started_at DESC, rowid DESC LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    if run is None:
        return None

    raw_params: list[Any] = [started_at]
    raw_end_clause = ""
    if completed_at:
        raw_end_clause = " AND collected_at <= ?"
        raw_params.append(completed_at)
    raw = db.execute(
        f"""
        SELECT payload_json, collected_at
        FROM raw_ingests
        WHERE source_key='the_odds_api' AND endpoint_name='current_mlb_odds'
          AND collected_at >= ? {raw_end_clause}
        ORDER BY collected_at DESC, ingest_id DESC LIMIT 1
        """,
        tuple(raw_params),
    ).fetchone()

    returned: set[str] = set()
    if raw is not None:
        try:
            payload = json.loads(raw["payload_json"] or "[]")
            returned = {
                str(book["key"])
                for event in payload
                if isinstance(event, dict)
                for book in (event.get("bookmakers") or [])
                if isinstance(book, dict) and book.get("key")
            }
        except (TypeError, KeyError, json.JSONDecodeError):
            returned = set()
    try:
        warnings = [str(value) for value in json.loads(run["warnings_json"] or "[]")]
    except (TypeError, json.JSONDecodeError):
        warnings = ["Collection warning data was unreadable."]
    missing: set[str] = set()
    prefix = "The Odds API did not return explicitly requested bookmaker key(s): "
    for warning in warnings:
        if warning.startswith(prefix):
            missing.update(
                value.strip()
                for value in warning[len(prefix) :].split(",")
                if value.strip()
            )
    return {
        "provider": "the_odds_api",
        "endpoint": "current_mlb_odds",
        "status": run["status"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "quotes_received": int(run["records_received"] or 0),
        "quotes_stored": int(run["records_stored"] or 0),
        "requested_bookmakers": sorted(returned | missing),
        "returned_bookmakers": sorted(returned),
        "missing_requested_bookmakers": sorted(missing),
        "warnings": warnings,
        "quota": {
            "requests_remaining": run["requests_remaining"],
            "requests_used": run["requests_used"],
            "request_cost": run["request_cost"],
        },
    }


def _market_pairs_at_cutoff(
    db: sqlite3.Connection,
    *,
    game_id: str,
    cutoff: datetime,
    stale_after_minutes: int = 60,
) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    rows = db.execute(
        """
        SELECT * FROM odds_snapshots
        WHERE game_id=? AND market_key='moneylines' AND period='full_game'
          AND market_status='open' AND source_updated_at <= ? AND collected_at <= ?
          AND source_updated_at >= ?
        ORDER BY source_updated_at DESC, odds_snapshot_id DESC
        """,
        (
            game_id,
            cutoff.isoformat(),
            cutoff.isoformat(),
            (cutoff - timedelta(minutes=stale_after_minutes)).isoformat(),
        ),
    ).fetchall()
    grouped: dict[tuple[str, str, str], dict[str, sqlite3.Row]] = {}
    for row in rows:
        key = (
            str(row["sportsbook_key"]),
            str(row["source_updated_at"]),
            str(row["settlement_rule"]),
        )
        grouped.setdefault(key, {}).setdefault(str(row["outcome_team_id"]), row)
    game = db.execute(
        "SELECT home_team_id, away_team_id FROM games WHERE game_id=?", (game_id,)
    ).fetchone()
    if game is None:
        return []
    pairs = []
    for sides in grouped.values():
        home = sides.get(str(game["home_team_id"]))
        away = sides.get(str(game["away_team_id"]))
        if home is not None and away is not None:
            pairs.append((home, away))
    return pairs


def _run_model_screens(
    db: sqlite3.Connection, *, started_at: str, completed_at: str | None
) -> dict[str, Any]:
    params: list[Any] = [started_at]
    end_clause = ""
    if completed_at:
        end_clause = " AND mr.created_at <= ?"
        params.append(completed_at)
    rows = db.execute(
        f"""
        SELECT mr.*, g.kickoff_time, g.home_team_id, g.away_team_id,
               home.official_name AS home_team_name,
               away.official_name AS away_team_name
        FROM model_runs mr
        JOIN games g ON g.game_id=mr.game_id
        JOIN teams home ON home.team_id=g.home_team_id
        JOIN teams away ON away.team_id=g.away_team_id
        WHERE mr.sport='mlb' AND mr.market_key='moneylines'
          AND mr.created_at >= ? {end_clause}
        ORDER BY g.kickoff_time, mr.created_at
        """,
        tuple(params),
    ).fetchall()
    screens: list[dict[str, Any]] = []
    for row in rows:
        cutoff = _parse_time(row["prediction_time"])
        if cutoff is None:
            continue
        pairs = _market_pairs_at_cutoff(
            db, game_id=str(row["game_id"]), cutoff=cutoff
        )
        accessible_books = sorted(
            {
                str(home["sportsbook_key"])
                for home, away in pairs
                if home["is_accessible"] and away["is_accessible"]
            }
        )
        trustworthy_books = sorted(
            {
                str(home["sportsbook_key"])
                for home, away in pairs
                if (home["is_accessible"] or home["is_consensus_eligible"])
                and (away["is_accessible"] or away["is_consensus_eligible"])
            }
        )
        sides = []
        for index, (team_id, team_name, probability) in enumerate(
            (
                (row["home_team_id"], row["home_team_name"], row["home_cover_probability"]),
                (row["away_team_id"], row["away_team_name"], row["away_cover_probability"]),
            )
        ):
            indicative = [
                pair[index]
                for pair in pairs
                if (pair[0]["is_accessible"] or pair[0]["is_consensus_eligible"])
                and (pair[1]["is_accessible"] or pair[1]["is_consensus_eligible"])
            ]
            best = max(indicative, key=lambda quote: float(quote["decimal_odds"]), default=None)
            model_probability = float(probability)
            conservative = max(0.0, model_probability - float(row["uncertainty_margin"]))
            decimal_odds = None if best is None else float(best["decimal_odds"])
            break_even = None if decimal_odds is None else 1.0 / decimal_odds
            edge = None if break_even is None else conservative - break_even
            ev = None if decimal_odds is None else conservative * decimal_odds - 1.0
            sides.append(
                {
                    "team_id": team_id,
                    "team": team_name,
                    "model_probability": _round(model_probability, 6),
                    "conservative_probability": _round(conservative, 6),
                    "indicative_consensus_sportsbook": None if best is None else best["sportsbook_key"],
                    "indicative_decimal_odds": _round(decimal_odds, 6),
                    "indicative_american_odds": decimal_to_american(decimal_odds),
                    "indicative_break_even_probability": _round(break_even, 6),
                    "indicative_probability_edge": _round(edge, 6),
                    "indicative_ev_per_dollar": _round(ev, 6),
                    "meets_five_point_edge_indicatively": bool(edge is not None and edge >= 0.05),
                }
            )
        screens.append(
            {
                "game_id": row["game_id"],
                "matchup": f"{row['away_team_name']} @ {row['home_team_name']}",
                "kickoff_time": row["kickoff_time"],
                "model_run_id": row["model_run_id"],
                "prediction_time": row["prediction_time"],
                    "model_version": row["model_version"],
                    "accessible_bookmakers": accessible_books,
                    "trustworthy_bookmakers": trustworthy_books,
                    "accessible_price_verification_required": False,
                    "bookmaker_coverage_warnings": (
                        ["No configured accessible bookmaker returned a complete fresh pair."]
                        if not accessible_books and trustworthy_books
                        else []
                    ),
                    "status": (
                        "screen_only_not_a_selection"
                        if trustworthy_books
                        else "DATA_LIMITED"
                    ),
                "sides": sides,
            }
        )
    return {
        "model_runs": len(rows),
        "screens": screens,
        "notice": (
            "Every complete fresh trustworthy two-sided market may supply the test-only "
            "selection price. Missing individual bookmakers are coverage warnings; zero "
            "trustworthy pairs is DATA_LIMITED."
        ),
    }


def _exact_market_no_vig_probability(
    db: sqlite3.Connection, *, selected_snapshot_id: int | None
) -> float | None:
    if selected_snapshot_id is None:
        return None
    selected = db.execute(
        """
        SELECT game_id, sportsbook_key, market_key, period, settlement_rule,
               outcome_team_id, opponent_team_id, decimal_odds,
               source_updated_at, collected_at
        FROM odds_snapshots WHERE odds_snapshot_id=?
        """,
        (selected_snapshot_id,),
    ).fetchone()
    if selected is None:
        return None
    opponent = db.execute(
        """
        SELECT decimal_odds FROM odds_snapshots
        WHERE game_id=? AND sportsbook_key=? AND market_key=? AND period=?
          AND settlement_rule=? AND outcome_team_id=?
          AND source_updated_at=? AND collected_at=?
        ORDER BY odds_snapshot_id DESC LIMIT 1
        """,
        (
            selected["game_id"],
            selected["sportsbook_key"],
            selected["market_key"],
            selected["period"],
            selected["settlement_rule"],
            selected["opponent_team_id"],
            selected["source_updated_at"],
            selected["collected_at"],
        ),
    ).fetchone()
    if opponent is None:
        return None
    left = 1.0 / float(selected["decimal_odds"])
    right = 1.0 / float(opponent["decimal_odds"])
    total = left + right
    return None if total <= 0 else left / total


def _run_decisions(
    db: sqlite3.Connection, *, started_at: str, completed_at: str | None
) -> dict[str, Any]:
    params: list[Any] = [started_at]
    end_clause = ""
    if completed_at:
        end_clause = " AND pd.decision_time <= ?"
        params.append(completed_at)
    rows = db.execute(
        f"""
        SELECT pd.*, g.kickoff_time, g.home_team_id, g.away_team_id,
               home.official_name AS home_team_name,
               away.official_name AS away_team_name,
               selected.official_name AS selected_team_name,
               pr.cover_result, pr.paper_profit_loss
        FROM paper_decisions pd
        JOIN games g ON g.game_id=pd.game_id
        JOIN teams home ON home.team_id=g.home_team_id
        JOIN teams away ON away.team_id=g.away_team_id
        LEFT JOIN teams selected ON selected.team_id=pd.selected_team_id
        LEFT JOIN paper_results pr ON pr.paper_decision_id=pd.paper_decision_id
        WHERE g.sport='mlb' AND pd.decision_time >= ? {end_clause}
        ORDER BY pd.decision_time, pd.paper_decision_id
        """,
        tuple(params),
    ).fetchall()
    selections: list[dict[str, Any]] = []
    evaluations: list[dict[str, Any]] = []
    pass_reasons: list[str] = []
    for row in rows:
        score = (
            int(row["pick_strength_score"])
            if row["pick_strength_score"] is not None
            else None
        )
        evaluation = {
            "paper_decision_id": row["paper_decision_id"],
            "game_id": row["game_id"],
            "matchup": f"{row['away_team_name']} @ {row['home_team_name']}",
            "kickoff_time": row["kickoff_time"],
            "market": row["market_key"],
            "decision": row["decision_type"],
            "selected_team": row["selected_team_name"],
            "pick_strength_score": score,
            "pick_strength_grade": pick_strength_grade(score),
            "pick_strength_display": format_pick_strength(score),
            "confidence": row["confidence_label"],
            "pass_reason": row["pass_reason"],
        }
        evaluations.append(evaluation)
        if row["decision_type"] == "pass":
            if row["pass_reason"]:
                pass_reasons.append(str(row["pass_reason"]))
            continue
        decimal_odds = _round(row["decimal_odds"], 6)
        minimum_decimal = _round(row["minimum_acceptable_decimal_odds"], 6)
        selections.append(
            {
                "paper_decision_id": row["paper_decision_id"],
                "game_id": row["game_id"],
                "matchup": f"{row['away_team_name']} @ {row['home_team_name']}",
                "kickoff_time": row["kickoff_time"],
                "market": row["market_key"],
                "selected_team": row["selected_team_name"],
                "sportsbook": row["sportsbook_key"],
                "decimal_odds": decimal_odds,
                "american_odds": decimal_to_american(decimal_odds),
                "model_probability": _round(row["model_probability"], 6),
                "conservative_probability": _round(row["conservative_probability"], 6),
                "market_no_vig_probability": _round(
                    _exact_market_no_vig_probability(
                        db,
                        selected_snapshot_id=row["selected_odds_snapshot_id"],
                    ),
                    6,
                ),
                "recorded_probability_edge": _round(row["probability_edge"], 6),
                "ev_per_dollar": _round(row["ev_per_dollar"], 6),
                "minimum_acceptable_decimal_odds": minimum_decimal,
                "minimum_acceptable_american_odds": decimal_to_american(minimum_decimal),
                "pick_strength_score": score,
                "pick_strength_grade": pick_strength_grade(score),
                "pick_strength_display": format_pick_strength(score),
                "confidence": row["confidence_label"],
                "result": row["cover_result"] or "pending",
                "paper_unit_dollars": paper_unit_dollars(),
                "paper_profit_units": _round(row["paper_profit_loss"], 6),
                "paper_profit_dollars": units_to_dollars(row["paper_profit_loss"]),
                "hypothetical_payouts": hypothetical_payouts(decimal_odds),
            }
        )
    return {
        "new_decisions": len(rows),
        "passes": sum(row["decision_type"] == "pass" for row in rows),
        "test_only_selections": len(selections),
        "pass_reasons": pass_reasons,
        "evaluations": evaluations,
        "selections": selections,
    }


def _progress(db: sqlite3.Connection) -> dict[str, Any]:
    row = db.execute(
        """
        SELECT
          COUNT(*) AS decisions,
          SUM(CASE WHEN pd.decision_type='pass' THEN 1 ELSE 0 END) AS passes,
          SUM(CASE WHEN pd.decision_type='test_only_selection' THEN 1 ELSE 0 END) AS selections
        FROM paper_decisions pd
        JOIN games g ON g.game_id=pd.game_id
        WHERE g.sport='mlb' AND pd.countable_oos=1
        """
    ).fetchone()
    record = db.execute(
        """
        SELECT
          SUM(CASE WHEN pr.cover_result='win' THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN pr.cover_result='loss' THEN 1 ELSE 0 END) AS losses,
          SUM(CASE WHEN pr.cover_result='push' THEN 1 ELSE 0 END) AS pushes
        FROM paper_decisions pd
        JOIN games g ON g.game_id=pd.game_id
        LEFT JOIN paper_results pr ON pr.paper_decision_id=pd.paper_decision_id
        WHERE g.sport='mlb' AND pd.countable_oos=1
          AND pd.decision_type='test_only_selection'
        """
    ).fetchone()
    decisions = int(row["decisions"] or 0)
    profit_row = db.execute(
        """
        SELECT COALESCE(SUM(pr.paper_profit_loss), 0) AS profit_units
        FROM paper_decisions pd
        JOIN games g ON g.game_id=pd.game_id
        JOIN paper_results pr ON pr.paper_decision_id=pd.paper_decision_id
        WHERE g.sport='mlb' AND pd.countable_oos=1
          AND pd.decision_type='test_only_selection'
          AND pr.cover_result IN ('win','loss','push')
        """
    ).fetchone()
    profit_units = _round(profit_row["profit_units"], 6)
    unit_value = paper_unit_dollars()
    return {
        "target_review_checkpoint": TARGET_DECISIONS,
        "recorded_regular_season_decisions": decisions,
        "remaining_to_review": max(0, TARGET_DECISIONS - decisions),
        "passes": int(row["passes"] or 0),
        "test_only_selections": int(row["selections"] or 0),
        "record": {
            "wins": int(record["wins"] or 0),
            "losses": int(record["losses"] or 0),
            "pushes": int(record["pushes"] or 0),
        },
        "paper_unit_dollars": unit_value,
        "paper_profit_units": profit_units,
        "paper_profit_dollars": units_to_dollars(profit_units, unit_value=unit_value),
        "automatic_approval": False,
    }


def build_result_payload(
    *,
    database_path: str | Path,
    workflow_run_id: str,
    workflow_run_attempt: str | int,
    operation_date: str,
    automated: bool | str,
    checked_at: str,
    job_status: str,
    canonical_outcome: str,
) -> dict[str, Any]:
    canonical_saved = str(canonical_outcome).casefold() == "success"
    payload: dict[str, Any] = {
        "feed_version": FEED_VERSION,
        "event_type": "paper_run_completed",
        "workflow_run_id": str(workflow_run_id),
        "workflow_run_attempt": int(workflow_run_attempt),
        "recorded_at": _utc_now(),
        "operation_date": operation_date,
        "automated": _bool(automated),
        "mode": "test_only",
        "sport": "mlb",
        "workflow_status": job_status,
        "canonical_database_saved": canonical_saved,
        "automatic_wagering": False,
        "database_available": False,
        "operations": None,
        "odds_diagnostics": None,
        "live_readiness": None,
        "model_screening": {"model_runs": 0, "screens": []},
        "run_decisions": {"new_decisions": 0, "passes": 0, "test_only_selections": 0, "pass_reasons": [], "evaluations": [], "selections": []},
        "progress": None,
        "first_failure": None,
        "errors": [],
    }
    path = Path(database_path)
    if path.is_file():
        payload["database_available"] = True
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        try:
            operation = _latest_current_operation(
                db, operation_date=operation_date, checked_at=checked_at
            )
            if operation is not None:
                payload["operations"] = {
                    "run_id": operation["run_id"],
                    "status": operation["status"],
                    "started_at": operation["started_at"],
                    "completed_at": operation["completed_at"],
                    "failed_step": operation["failed_step"],
                }
                payload["live_readiness"] = _latest_live_report(
                    db,
                    operation_date=operation_date,
                    operation_started_at=operation["started_at"],
                )
                payload["run_decisions"] = _run_decisions(
                    db,
                    started_at=operation["started_at"],
                    completed_at=operation["completed_at"],
                )
                payload["odds_diagnostics"] = _latest_odds_diagnostics(
                    db,
                    started_at=operation["started_at"],
                    completed_at=operation["completed_at"],
                )
                payload["model_screening"] = _run_model_screens(
                    db,
                    started_at=operation["started_at"],
                    completed_at=operation["completed_at"],
                )
                if operation["failed_step"]:
                    payload["first_failure"] = str(operation["failed_step"])
            payload["progress"] = _progress(db)
        except (sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
            payload["errors"].append(f"status_database_read_failed:{exc}")
        finally:
            db.close()
    if str(job_status).casefold() != "success" or not canonical_saved:
        payload["event_type"] = "paper_run_failed"
        if payload["first_failure"] is None:
            payload["first_failure"] = "workflow_failed_before_or_after_operations"
    elif payload["operations"] is None:
        payload["event_type"] = "paper_run_failed"
        payload["first_failure"] = "no_current_operations_record"
    elif payload["operations"]["status"] != "succeeded":
        payload["event_type"] = "paper_run_failed"
        payload["first_failure"] = payload["operations"].get("failed_step") or "operations_failed"
    payload["notice"] = (
        "Test-only MLB paper reporting. Hypothetical $2-$5 amounts are examples based on recorded paper prices; no real-money action is taken."
    )
    return payload


def render_status_comment(payload: dict[str, Any]) -> str:
    label = {
        "paper_run_gate_due": "Our MLB Paper Gate — Due",
        "paper_run_completed": "Our MLB Paper Run — Completed",
        "paper_run_failed": "Our MLB Paper Run — Failed",
    }.get(str(payload.get("event_type")), "Our MLB Paper Status")
    body = json.dumps(payload, indent=2, sort_keys=True)
    return (
        f"<!-- {STATUS_MARKER} -->\n"
        f"### {label}\n\n"
        "```json\n"
        f"{body}\n"
        "```\n\n"
        "Test-only status feed. No real-money action is taken.\n"
    )
