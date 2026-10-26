"""Result ingestion, paper settlement, CLV, and validation metrics.

No result is guessed. A result must come from a named source. Pass decisions do not
count as completed forward decisions until their event has actually concluded, and
uncertain sportsbook settlement states remain unsettled until their rule is verified.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from statistics import mean
from typing import Any

from .multisport_evaluation import current_market_views
from .multisport_runtime import ensure_runtime_schema
from .sports_registry import validation_bucket


UTC = timezone.utc


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a time zone")
    return value.astimezone(UTC).isoformat()


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def record_result(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    result_status: str,
    source_key: str,
    source_updated_at: datetime,
    collected_at: datetime,
    home_score: float | None = None,
    away_score: float | None = None,
    winner_role: str | None = None,
    raw_result: dict[str, Any] | None = None,
) -> int:
    ensure_runtime_schema(connection)
    event = connection.execute("SELECT * FROM multisport_events WHERE event_id=?", (event_id,)).fetchone()
    if event is None:
        raise ValueError("Unknown event")
    if result_status not in {"final", "cancelled", "postponed", "no_contest", "walkover", "retirement"}:
        raise ValueError("Unsupported result status")
    if winner_role not in {None, "home", "away", "draw", "void"}:
        raise ValueError("Unsupported winner role")
    if source_updated_at > collected_at:
        raise ValueError("Result source update cannot follow collection time")
    if result_status == "final" and winner_role is None:
        if home_score is None or away_score is None:
            raise ValueError("Final result requires a winner role or both scores")
        winner_role = "home" if home_score > away_score else "away" if away_score > home_score else "draw"
    connection.execute(
        """
        INSERT INTO multisport_results (
            event_id, sport, home_score, away_score, winner_role, result_status,
            source_key, source_updated_at, collected_at, raw_result_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO UPDATE SET
            home_score=excluded.home_score,
            away_score=excluded.away_score,
            winner_role=excluded.winner_role,
            result_status=excluded.result_status,
            source_key=excluded.source_key,
            source_updated_at=excluded.source_updated_at,
            collected_at=excluded.collected_at,
            raw_result_json=excluded.raw_result_json
        """,
        (
            event_id, event["sport"], home_score, away_score, winner_role, result_status,
            source_key, _iso(source_updated_at), _iso(collected_at),
            json.dumps(raw_result or {}, sort_keys=True),
        ),
    )
    event_status = "final" if result_status in {"final", "no_contest", "walkover", "retirement"} else result_status
    connection.execute("UPDATE multisport_events SET status=? WHERE event_id=?", (event_status, event_id))
    connection.commit()
    row = connection.execute("SELECT result_id FROM multisport_results WHERE event_id=?", (event_id,)).fetchone()
    return int(row[0])


def _rule_row(connection: sqlite3.Connection, decision: sqlite3.Row) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM multisport_settlement_rules
        WHERE sportsbook_key=? AND sport=? AND market_key=? AND period=?
        """,
        (decision["sportsbook_key"], decision["sport"], decision["market_key"], decision["period"]),
    ).fetchone()


def _grade_spread(decision: sqlite3.Row, result: sqlite3.Row) -> str:
    if result["home_score"] is None or result["away_score"] is None:
        return "unsettled"
    role = str(decision["outcome_role"])
    points = float(decision["points"])
    home_score = float(result["home_score"])
    away_score = float(result["away_score"])
    adjusted = home_score - away_score + points if role == "home" else away_score - home_score + points
    if adjusted > 0:
        return "win"
    if adjusted < 0:
        return "loss"
    return "push"


def _tennis_result_payload(result: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(str(result["raw_result_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _tennis_first_set_completed(result: sqlite3.Row) -> bool | None:
    payload = _tennis_result_payload(result)
    value = payload.get("first_set_completed")
    if isinstance(value, bool):
        return value
    completed_sets = payload.get("completed_sets")
    if isinstance(completed_sets, int) and not isinstance(completed_sets, bool) and completed_sets >= 0:
        return completed_sets >= 1
    return None


def _tennis_point_completed(result: sqlite3.Row) -> bool | None:
    payload = _tennis_result_payload(result)
    value = payload.get("at_least_one_point_completed")
    if isinstance(value, bool):
        return value
    completed_points = payload.get("completed_points")
    if isinstance(completed_points, int) and not isinstance(completed_points, bool) and completed_points >= 0:
        return completed_points >= 1
    return None


def _grade_moneyline(decision: sqlite3.Row, result: sqlite3.Row, rule: sqlite3.Row | None) -> str:
    status = str(result["result_status"])
    sport = str(decision["sport"])
    verified = rule is not None and rule["rule_status"] == "verified"
    rule_key = str(rule["rule_key"]) if rule is not None else ""
    if status in {"cancelled", "postponed"}:
        return "void"
    if sport in {"atp", "wta"} and status == "walkover":
        return "void" if verified else "unsettled"
    if sport in {"atp", "wta"} and status == "retirement":
        if not verified or result["winner_role"] not in {"home", "away"}:
            return "unsettled"
        winner = str(result["winner_role"])
        selected = str(decision["outcome_role"])
        if rule_key == "book_tennis_first_set_retirement_action":
            first_set = _tennis_first_set_completed(result)
            if first_set is None:
                return "unsettled"
            if not first_set:
                return "void"
            return "win" if winner == selected else "loss"
        if rule_key == "draftkings_tennis_match_moneyline_interrupted_advancer":
            point = _tennis_point_completed(result)
            if point is None:
                return "unsettled"
            if not point:
                return "void"
            return "win" if winner == selected else "void"
        return "unsettled"
    if sport == "mma" and status == "no_contest":
        return "void" if verified else "unsettled"
    winner = result["winner_role"]
    if winner is None:
        return "unsettled"
    if sport == "mma" and winner == "draw":
        return "push" if verified else "unsettled"
    return "win" if str(winner) == str(decision["outcome_role"]) else "loss"


def _closing_probability(
    connection: sqlite3.Connection,
    decision: sqlite3.Row,
) -> tuple[float | None, float | None]:
    event = connection.execute("SELECT * FROM multisport_events WHERE event_id=?", (decision["event_id"],)).fetchone()
    if event is None:
        return None, None
    start = _dt(str(event["commence_time"]))
    views = current_market_views(
        connection,
        event_id=str(decision["event_id"]),
        sport=str(decision["sport"]),
        as_of=start - timedelta(microseconds=1),
        stale_after_minutes=24 * 60,
    )
    for view in views:
        if view.sportsbook_key != decision["sportsbook_key"]:
            continue
        role = str(decision["outcome_role"])
        row = next((item for item in view.rows if item["outcome_role"] == role), None)
        if row is None:
            continue
        if decision["points"] is not None:
            if row["points"] is None or abs(float(row["points"]) - float(decision["points"])) > 1e-9:
                continue
        return float(row["decimal_odds"]), float(view.probabilities[role])
    return None, None


def settle_decision(
    connection: sqlite3.Connection,
    *,
    paper_decision_id: str,
    settled_at: datetime,
) -> str:
    ensure_runtime_schema(connection)
    decision = connection.execute(
        "SELECT * FROM multisport_paper_decisions WHERE paper_decision_id=?",
        (paper_decision_id,),
    ).fetchone()
    if decision is None:
        raise ValueError("Unknown paper decision")
    existing = connection.execute(
        "SELECT grading_result FROM multisport_paper_results WHERE paper_decision_id=?",
        (paper_decision_id,),
    ).fetchone()
    if existing:
        return str(existing[0])

    result = connection.execute(
        "SELECT * FROM multisport_results WHERE event_id=?",
        (decision["event_id"],),
    ).fetchone()
    # A pass is still a forward model decision. It becomes completed only after the
    # event has a terminal result record, preventing future games from inflating 100/OOS.
    if result is None:
        return "unsettled"

    if decision["decision_type"] == "pass":
        grade = "pass"
        profit = 0.0
        closing_odds = None
        closing_prob = None
    else:
        rule = _rule_row(connection, decision)
        if decision["market_key"] == "spreads":
            if rule is None or rule["rule_status"] != "verified":
                return "unsettled"
            if result["result_status"] in {"cancelled", "postponed"}:
                grade = "void"
            else:
                grade = _grade_spread(decision, result)
        else:
            grade = _grade_moneyline(decision, result, rule)
        if grade == "unsettled":
            return grade
        profit = float(decision["decimal_odds"]) - 1.0 if grade == "win" else -1.0 if grade == "loss" else 0.0
        closing_odds, closing_prob = _closing_probability(connection, decision)

    entry_prob = float(decision["break_even_probability"]) if decision["break_even_probability"] is not None else None
    probability_clv = None
    close_comparison = "unavailable"
    if entry_prob is not None and closing_prob is not None:
        probability_clv = closing_prob - entry_prob
        close_comparison = "better" if probability_clv > 1e-9 else "worse" if probability_clv < -1e-9 else "equal"
    connection.execute(
        """
        INSERT INTO multisport_paper_results (
            paper_decision_id, grading_result, paper_profit_loss,
            closing_decimal_odds, closing_probability, entry_probability,
            probability_clv, close_comparison, settled_at, settlement_notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            paper_decision_id, grade, profit, closing_odds, closing_prob, entry_prob,
            probability_clv, close_comparison, _iso(settled_at),
            "Settled from a named result source and stored sportsbook rule.",
        ),
    )
    connection.commit()
    refresh_validation_metrics(connection, str(decision["sport"]), updated_at=settled_at)
    return grade


def settle_ready_decisions(connection: sqlite3.Connection, *, settled_at: datetime) -> dict[str, int]:
    ensure_runtime_schema(connection)
    rows = connection.execute(
        """
        SELECT d.paper_decision_id
        FROM multisport_paper_decisions d
        LEFT JOIN multisport_paper_results r ON r.paper_decision_id=d.paper_decision_id
        WHERE r.paper_decision_id IS NULL
        ORDER BY d.check_time
        """
    ).fetchall()
    counts = {"settled": 0, "unsettled": 0}
    for row in rows:
        grade = settle_decision(connection, paper_decision_id=str(row[0]), settled_at=settled_at)
        counts["unsettled" if grade == "unsettled" else "settled"] += 1
    return counts



def pending_result_notifications(
    connection: sqlite3.Connection,
    *,
    channel: str = "telegram",
    paper_unit_dollars: float = 10.0,
) -> tuple[dict[str, Any], ...]:
    """Return newly settled selections not already announced on the channel."""
    ensure_runtime_schema(connection)
    if not channel.strip():
        raise ValueError("Notification channel is required")
    if paper_unit_dollars <= 0:
        raise ValueError("Paper unit value must be positive")
    rows = connection.execute(
        """
        SELECT
            d.paper_decision_id, d.sport, d.market_key, d.period, d.outcome_role,
            d.points, d.decimal_odds, d.sportsbook_key, d.countable_oos,
            d.confidence_label, d.check_time,
            pr.grading_result, pr.paper_profit_loss, pr.settled_at,
            e.commence_time,
            home.full_name AS home_name,
            away.full_name AS away_name,
            selected.full_name AS selection_name,
            r.home_score, r.away_score, r.winner_role, r.result_status,
            r.source_key, r.source_updated_at
        FROM multisport_paper_decisions d
        JOIN multisport_paper_results pr
          ON pr.paper_decision_id=d.paper_decision_id
        JOIN multisport_events e ON e.event_id=d.event_id
        JOIN multisport_participants home
          ON home.participant_id=e.home_participant_id
        JOIN multisport_participants away
          ON away.participant_id=e.away_participant_id
        LEFT JOIN multisport_participants selected
          ON selected.participant_id=d.outcome_participant_id
        JOIN multisport_results r ON r.event_id=d.event_id
        LEFT JOIN multisport_result_notifications n
          ON n.paper_decision_id=d.paper_decision_id AND n.channel=?
        WHERE n.paper_decision_id IS NULL
          AND d.decision_type!='pass'
          AND pr.grading_result IN ('win','loss','push','void')
        ORDER BY pr.settled_at, d.paper_decision_id
        """,
        (channel,),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        record_rows = connection.execute(
            """
            SELECT pr.grading_result, COUNT(*) AS total
            FROM multisport_paper_decisions d
            JOIN multisport_paper_results pr
              ON pr.paper_decision_id=d.paper_decision_id
            WHERE d.sport=? AND d.decision_type!='pass'
              AND pr.grading_result IN ('win','loss','push','void')
              AND (pr.settled_at<? OR (pr.settled_at=? AND d.paper_decision_id<=?))
            GROUP BY pr.grading_result
            """,
            (row["sport"], row["settled_at"], row["settled_at"], row["paper_decision_id"]),
        ).fetchall()
        record = {"win": 0, "loss": 0, "push": 0, "void": 0}
        for item in record_rows:
            record[str(item["grading_result"])] = int(item["total"])
        payload = dict(row)
        payload["paper_profit_loss_dollars"] = round(
            float(row["paper_profit_loss"]) * paper_unit_dollars, 2
        )
        payload["record"] = record
        output.append(payload)
    return tuple(output)


def mark_result_notified(
    connection: sqlite3.Connection,
    *,
    paper_decision_id: str,
    channel: str,
    notified_at: datetime,
    source_workflow_run_id: str,
) -> bool:
    """Record a successful delivery. Repeated calls are safe and return False."""
    ensure_runtime_schema(connection)
    if not channel.strip() or not source_workflow_run_id.strip():
        raise ValueError("Channel and source workflow run ID are required")
    settled = connection.execute(
        """
        SELECT 1
        FROM multisport_paper_decisions d
        JOIN multisport_paper_results pr
          ON pr.paper_decision_id=d.paper_decision_id
        WHERE d.paper_decision_id=? AND d.decision_type!='pass'
          AND pr.grading_result IN ('win','loss','push','void')
        """,
        (paper_decision_id,),
    ).fetchone()
    if settled is None:
        raise ValueError("Only a settled non-pass selection can be marked as notified")
    before = connection.total_changes
    connection.execute(
        """
        INSERT OR IGNORE INTO multisport_result_notifications (
            paper_decision_id, channel, notified_at, source_workflow_run_id
        ) VALUES (?, ?, ?, ?)
        """,
        (
            paper_decision_id,
            channel,
            _iso(notified_at),
            source_workflow_run_id,
        ),
    )
    inserted = connection.total_changes > before
    connection.commit()
    return inserted

def _max_drawdown(profits: list[float]) -> float:
    cumulative = peak = drawdown = 0.0
    for profit in profits:
        cumulative += profit
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return drawdown


def _calibration_error(rows: list[sqlite3.Row]) -> float | None:
    usable = [row for row in rows if row["model_probability"] is not None and row["grading_result"] in {"win", "loss"}]
    if not usable:
        return None
    bins: list[list[tuple[float, int]]] = [[] for _ in range(10)]
    for row in usable:
        p = float(row["model_probability"])
        bins[min(9, max(0, int(p * 10)))].append((p, 1 if row["grading_result"] == "win" else 0))
    error = 0.0
    for bucket in bins:
        if bucket:
            error += len(bucket) / len(usable) * abs(
                mean(item[0] for item in bucket) - mean(item[1] for item in bucket)
            )
    return error


def refresh_validation_metrics(
    connection: sqlite3.Connection,
    sport: str,
    *,
    updated_at: datetime,
) -> dict[str, float | int | None | str]:
    """Refresh independent forward metrics for one sport/market validation bucket.

    ``completed_count`` counts every terminal, countable OOS event decision including
    passes, matching the project's one-final-decision-per-event standard. ROI/CLV and
    selection calibration are computed only from actually selected wagers.
    """
    ensure_runtime_schema(connection)
    rows = connection.execute(
        """
        SELECT d.*, r.*
        FROM multisport_paper_decisions d
        JOIN multisport_paper_results r ON r.paper_decision_id=d.paper_decision_id
        WHERE d.sport=? AND d.countable_oos=1
          AND r.grading_result IN ('win','loss','push','void','pass')
        ORDER BY d.check_time
        """,
        (sport,),
    ).fetchall()
    completed_rows = [row for row in rows if row["grading_result"] != "void"]
    selected = [row for row in completed_rows if row["grading_result"] in {"win", "loss", "push"}]
    wins = sum(1 for row in selected if row["grading_result"] == "win")
    losses = sum(1 for row in selected if row["grading_result"] == "loss")
    pushes = sum(1 for row in selected if row["grading_result"] == "push")
    voids = sum(1 for row in rows if row["grading_result"] == "void")
    profits = [float(row["paper_profit_loss"]) for row in selected]
    roi = sum(profits) / len(selected) if selected else None
    clvs = [float(row["probability_clv"]) for row in selected if row["probability_clv"] is not None]
    avg_clv = mean(clvs) if clvs else None
    binary = [row for row in selected if row["grading_result"] in {"win", "loss"} and row["model_probability"] is not None]
    brier = mean(
        (float(row["model_probability"]) - (1.0 if row["grading_result"] == "win" else 0.0)) ** 2
        for row in binary
    ) if binary else None
    calibration_error = _calibration_error(selected)
    existing = connection.execute(
        "SELECT * FROM multisport_validation_metrics WHERE validation_bucket=?",
        (validation_bucket(sport),),
    ).fetchone()
    calibration_status = str(existing["calibration_status"]) if existing else "untested"
    clv_status = str(existing["clv_status"]) if existing else "unknown"
    review_status = str(existing["review_status"]) if existing else "collecting"
    if len(completed_rows) >= 100 and review_status == "collecting":
        review_status = "ready_for_review"
    connection.execute(
        """
        INSERT INTO multisport_validation_metrics (
            validation_bucket, completed_count, wins, losses, pushes, voids,
            roi, average_clv, brier_score, calibration_error, max_drawdown,
            calibration_status, clv_status, review_status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(validation_bucket) DO UPDATE SET
            completed_count=excluded.completed_count,
            wins=excluded.wins,
            losses=excluded.losses,
            pushes=excluded.pushes,
            voids=excluded.voids,
            roi=excluded.roi,
            average_clv=excluded.average_clv,
            brier_score=excluded.brier_score,
            calibration_error=excluded.calibration_error,
            max_drawdown=excluded.max_drawdown,
            review_status=excluded.review_status,
            updated_at=excluded.updated_at
        """,
        (
            validation_bucket(sport), len(completed_rows), wins, losses, pushes, voids,
            roi, avg_clv, brier, calibration_error, _max_drawdown(profits),
            calibration_status, clv_status, review_status, _iso(updated_at),
        ),
    )
    connection.execute(
        """
        UPDATE multisport_validation_state
        SET completed_decisions=?, updated_at=?
        WHERE validation_bucket=?
        """,
        (len(completed_rows), _iso(updated_at), validation_bucket(sport)),
    )
    connection.commit()
    return {
        "completed_count": len(completed_rows),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "voids": voids,
        "roi": roi,
        "average_clv": avg_clv,
        "brier_score": brier,
        "calibration_error": calibration_error,
        "max_drawdown": _max_drawdown(profits),
        "review_status": review_status,
    }


def record_manual_validation_review(
    connection: sqlite3.Connection,
    *,
    sport: str,
    calibration_status: str,
    clv_status: str,
    passed: bool,
    reviewed_at: datetime,
) -> None:
    ensure_runtime_schema(connection)
    if calibration_status not in {"acceptable", "unacceptable"}:
        raise ValueError("Calibration review must be acceptable or unacceptable")
    if clv_status not in {"stable", "favorable", "unfavorable"}:
        raise ValueError("CLV review must be stable, favorable, or unfavorable")
    row = connection.execute(
        "SELECT completed_count FROM multisport_validation_metrics WHERE validation_bucket=?",
        (validation_bucket(sport),),
    ).fetchone()
    if row is None or int(row[0]) < 100:
        raise ValueError("Validation review cannot be finalized before 100 completed OOS decisions")
    connection.execute(
        """
        UPDATE multisport_validation_metrics
        SET calibration_status=?, clv_status=?, review_status=?, updated_at=?
        WHERE validation_bucket=?
        """,
        (
            calibration_status, clv_status,
            "reviewed_pass" if passed else "reviewed_fail",
            _iso(reviewed_at), validation_bucket(sport),
        ),
    )
    connection.commit()
