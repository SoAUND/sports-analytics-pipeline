"""Immutable Phase 5 paper tracking, settlement, and reporting."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
import sqlite3
import uuid
from typing import Any

from .paper_money import paper_unit_dollars, units_to_dollars


TARGET_DECISIONS = 100


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Paper-tracking timestamps must include a time zone")
    return value.astimezone(timezone.utc).isoformat()


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def record_paper_decision(
    db: sqlite3.Connection,
    *,
    game: sqlite3.Row,
    outcome: dict[str, Any],
    decision_time: datetime,
) -> dict[str, Any]:
    """Lock the first final full-game decision for a game and market; never overwrite it."""

    market_key = str(outcome.get("market_key") or "spreads")
    if market_key not in {"spreads", "moneylines"}:
        raise ValueError("Unsupported paper market")
    existing = db.execute(
        """SELECT paper_decision_id, decision_type FROM paper_decisions
           WHERE game_id = ? AND market_key = ? AND period = 'full_game'""",
        (game["game_id"], market_key),
    ).fetchone()
    if existing:
        return {
            "status": "already_recorded",
            "paper_decision_id": existing["paper_decision_id"],
            "decision": existing["decision_type"],
        }

    paper_id = str(uuid.uuid4())
    is_selection = outcome.get("decision") == "test_only_selection"
    countable = int(game["season_type"] == "regular")
    with db:
        db.execute(
            """
            INSERT INTO paper_decisions (
                paper_decision_id, game_id, model_run_id, season_type, market_key,
                decision_type, selected_team_id, spread_points, sportsbook_key,
                selected_odds_snapshot_id, decimal_odds, price_time,
                model_probability, conservative_probability, push_probability,
                break_even_probability, probability_edge, ev_per_dollar,
                minimum_acceptable_decimal_odds, pick_strength_score,
                confidence_label, model_version, feature_version, data_version,
                rule_version, gate_results_json, reasons_json, loss_reasons_json,
                source_notes_json, pass_reason, countable_oos, decision_time,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper_id,
                game["game_id"],
                outcome.get("model_run_id"),
                game["season_type"],
                market_key,
                outcome.get("decision", "pass"),
                outcome.get("team_id") if is_selection else None,
                _number(outcome.get("spread_points")),
                outcome.get("sportsbook"),
                outcome.get("odds_snapshot_id") if is_selection else None,
                _number(outcome.get("decimal_odds")),
                outcome.get("checked_at"),
                _number(outcome.get("model_probability")),
                _number(outcome.get("conservative_probability")),
                _number(outcome.get("push_probability")),
                _number(outcome.get("break_even_probability")),
                _number(outcome.get("probability_edge")),
                _number(outcome.get("ev_per_dollar")),
                _number(outcome.get("minimum_acceptable_decimal_odds")),
                outcome.get("pick_strength_score"),
                outcome.get("confidence"),
                outcome.get("model_version"),
                outcome.get("feature_version"),
                outcome.get("data_version"),
                outcome.get("model_rule_version", "our-pick-evaluation-v1.1"),
                json.dumps(outcome.get("gates", {}), sort_keys=True),
                json.dumps(outcome.get("supporting_reasons", [])),
                json.dumps(outcome.get("edge_loss_reasons", [])),
                json.dumps(outcome.get("source_notes", [])),
                outcome.get("pass_reason"),
                countable,
                _iso(decision_time),
                _iso(decision_time),
            ),
        )
    return {
        "status": "recorded",
        "paper_decision_id": paper_id,
        "decision": outcome.get("decision", "pass"),
        "countable_oos": bool(countable),
    }


def attach_source_decision(
    db: sqlite3.Connection, paper_decision_id: str, source_decision_id: str
) -> None:
    with db:
        db.execute(
            "UPDATE paper_decisions SET source_decision_id = ? WHERE paper_decision_id = ?",
            (source_decision_id, paper_decision_id),
        )


def _closing_quote(db: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row | None:
    if not row["selected_team_id"] or not row["sportsbook_key"]:
        return None
    return db.execute(
        """
        SELECT * FROM odds_snapshots
        WHERE game_id = ? AND sportsbook_key = ? AND outcome_team_id = ?
          AND market_key = ? AND period = 'full_game'
          AND source_updated_at <= ?
        ORDER BY source_updated_at DESC, collected_at DESC, odds_snapshot_id DESC
        LIMIT 1
        """,
        (
            row["game_id"],
            row["sportsbook_key"],
            row["selected_team_id"],
            row["market_key"],
            row["kickoff_time"],
        ),
    ).fetchone()


def _consensus_close(db: sqlite3.Connection, row: sqlite3.Row) -> float | None:
    if not row["selected_team_id"]:
        return None
    quotes = db.execute(
        """
        SELECT * FROM odds_snapshots
        WHERE game_id = ? AND market_key = ? AND period = 'full_game'
          AND is_consensus_eligible = 1 AND source_updated_at <= ?
        ORDER BY source_updated_at DESC, collected_at DESC, odds_snapshot_id DESC
        """,
        (row["game_id"], row["market_key"], row["kickoff_time"]),
    ).fetchall()
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for quote in quotes:
        latest.setdefault((quote["sportsbook_key"], quote["outcome_team_id"]), quote)
    values: list[float] = []
    for book in {key[0] for key in latest}:
        selected = latest.get((book, row["selected_team_id"]))
        opponent_id = row["away_team_id"] if row["selected_team_id"] == row["home_team_id"] else row["home_team_id"]
        opponent = latest.get((book, opponent_id))
        if not selected or not opponent:
            continue
        if row["market_key"] == "spreads":
            if selected["spread_points"] is None or opponent["spread_points"] is None:
                continue
            if abs(float(selected["spread_points"]) + float(opponent["spread_points"])) > 1e-9:
                continue
        left = 1.0 / float(selected["decimal_odds"])
        right = 1.0 / float(opponent["decimal_odds"])
        values.append(left / (left + right))
    if not values:
        return None
    values.sort()
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def settle_paper_decisions(
    db: sqlite3.Connection, *, as_of: datetime | None = None, sport: str | None = None
) -> dict[str, Any]:
    if sport not in {None, "nfl", "mlb"}:
        raise ValueError("Unsupported sport")
    settled_at = as_of or datetime.now(timezone.utc)
    sport_clause = "" if sport is None else " AND g.sport = ?"
    params: tuple[Any, ...] = () if sport is None else (sport,)
    rows = db.execute(
        f"""
        SELECT pd.*, g.home_team_id, g.away_team_id, g.home_score, g.away_score,
               g.kickoff_time, g.status
        FROM paper_decisions pd JOIN games g ON g.game_id = pd.game_id
        LEFT JOIN paper_results pr ON pr.paper_decision_id = pd.paper_decision_id
        WHERE pr.paper_decision_id IS NULL AND g.status = 'final'
          AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
          {sport_clause}
        ORDER BY g.kickoff_time, pd.paper_decision_id
        """,
        params,
    ).fetchall()
    counts = Counter()
    for row in rows:
        close = _closing_quote(db, row)
        consensus = _consensus_close(db, row)
        entry_spread = _number(row["spread_points"])
        entry_odds = _number(row["decimal_odds"])
        close_spread = _number(close["spread_points"]) if close else None
        close_odds = _number(close["decimal_odds"]) if close else None
        if row["decision_type"] == "pass":
            result, push, profit = "pass", 0, 0.0
        else:
            selected_home = row["selected_team_id"] == row["home_team_id"]
            margin = (
                int(row["home_score"]) - int(row["away_score"])
                if selected_home
                else int(row["away_score"]) - int(row["home_score"])
            )
            if row["market_key"] == "moneylines":
                if margin > 0:
                    result, push, profit = "win", 0, float(entry_odds) - 1.0
                elif margin < 0:
                    result, push, profit = "loss", 0, -1.0
                else:
                    result, push, profit = "push", 1, 0.0
            else:
                cover = margin + float(entry_spread)
                if abs(cover) < 1e-9:
                    result, push, profit = "push", 1, 0.0
                elif cover > 0:
                    result, push, profit = "win", 0, float(entry_odds) - 1.0
                else:
                    result, push, profit = "loss", 0, -1.0
        line_clv = (
            None
            if row["market_key"] != "spreads" or entry_spread is None or close_spread is None
            else entry_spread - close_spread
        )
        exact_price_clv = None
        if close and entry_odds is not None:
            if row["market_key"] == "moneylines":
                exact_price_clv = (1.0 / float(close["decimal_odds"])) - (1.0 / float(entry_odds))
            elif entry_spread is not None and close["spread_points"] is not None and abs(float(close["spread_points"]) - entry_spread) < 1e-9:
                exact_price_clv = (1.0 / float(close["decimal_odds"])) - (1.0 / float(entry_odds))
        if line_clv is None:
            comparison = "unavailable"
        elif line_clv > 1e-9:
            comparison = "better"
        elif line_clv < -1e-9:
            comparison = "worse"
        else:
            comparison = "equal"
        with db:
            db.execute(
                """
                INSERT INTO paper_results (
                    paper_decision_id, home_score, away_score, cover_result,
                    push_status, entry_spread, entry_decimal_odds, closing_spread,
                    closing_decimal_odds, closing_sportsbook_key,
                    closing_snapshot_time, consensus_closing_probability,
                    line_clv_points, exact_line_price_clv_probability,
                    close_comparison, paper_profit_loss, settled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["paper_decision_id"], row["home_score"], row["away_score"],
                    result, push, entry_spread, entry_odds, close_spread, close_odds,
                    close["sportsbook_key"] if close else None,
                    close["source_updated_at"] if close else None,
                    consensus, line_clv, exact_price_clv, comparison, profit,
                    _iso(settled_at),
                ),
            )
        counts[result] += 1
    return {
        "newly_settled": len(rows),
        "sport": sport or "all",
        "results": dict(counts),
        "notice": "Paper results only. No real-money action is taken.",
    }


def _rounded(value: float | None, places: int = 6) -> float | None:
    return None if value is None else round(value, places)


def paper_report(
    db: sqlite3.Connection, *, target: int = TARGET_DECISIONS, sport: str = "nfl"
) -> dict[str, Any]:
    if sport not in {"nfl", "mlb"}:
        raise ValueError("Unsupported sport")
    rows = db.execute(
        """
        SELECT pd.*, pr.cover_result, pr.paper_profit_loss, pr.line_clv_points,
               pr.exact_line_price_clv_probability
        FROM paper_decisions pd
        JOIN games g ON g.game_id = pd.game_id
        LEFT JOIN paper_results pr ON pr.paper_decision_id = pd.paper_decision_id
        WHERE pd.countable_oos = 1 AND g.sport = ?
        ORDER BY pd.decision_time, pd.paper_decision_id
        """,
        (sport,),
    ).fetchall()
    selected = [row for row in rows if row["decision_type"] == "test_only_selection"]
    settled_selected = [row for row in selected if row["cover_result"] in {"win", "loss", "push"}]
    scored = [row for row in settled_selected if row["cover_result"] != "push" and row["model_probability"] is not None]
    brier = None
    log_loss = None
    if scored:
        probs = [max(1e-9, min(1 - 1e-9, float(row["model_probability"]))) for row in scored]
        outcomes = [1.0 if row["cover_result"] == "win" else 0.0 for row in scored]
        brier = sum((p - y) ** 2 for p, y in zip(probs, outcomes)) / len(scored)
        log_loss = -sum(y * math.log(p) + (1 - y) * math.log(1 - p) for p, y in zip(probs, outcomes)) / len(scored)
    profits = [float(row["paper_profit_loss"]) for row in settled_selected]
    cumulative = peak = drawdown = 0.0
    for profit in profits:
        cumulative += profit
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    line_clv = [float(row["line_clv_points"]) for row in settled_selected if row["line_clv_points"] is not None]
    price_clv = [float(row["exact_line_price_clv_probability"]) for row in settled_selected if row["exact_line_price_clv_probability"] is not None]
    pass_reasons = Counter(str(row["pass_reason"]) for row in rows if row["decision_type"] == "pass")
    completed = len(rows)
    profit_units = _rounded(sum(profits))
    drawdown_units = _rounded(drawdown)
    unit_value = paper_unit_dollars()
    return {
        "mode": "test_only",
        "sport": sport,
        "target_regular_season_decisions": target,
        "recorded_regular_season_decisions": completed,
        "remaining": max(0, target - completed),
        "progress_percent": round(min(100.0, completed * 100.0 / target), 1),
        "passes": sum(row["decision_type"] == "pass" for row in rows),
        "selections": len(selected),
        "settled_selections": len(settled_selected),
        "pending_results": sum(row["cover_result"] is None for row in rows),
        "record": {
            "wins": sum(row["cover_result"] == "win" for row in settled_selected),
            "losses": sum(row["cover_result"] == "loss" for row in settled_selected),
            "pushes": sum(row["cover_result"] == "push" for row in settled_selected),
        },
        "paper_unit_dollars": unit_value,
        "paper_profit_units": profit_units,
        "paper_profit_dollars": units_to_dollars(profit_units, unit_value=unit_value),
        "paper_roi_per_settled_selection": _rounded(sum(profits) / len(profits) if profits else None),
        "maximum_drawdown_units": drawdown_units,
        "maximum_drawdown_dollars": units_to_dollars(drawdown_units, unit_value=unit_value),
        "calibration": {"scored_decisions": len(scored), "brier": _rounded(brier), "log_loss": _rounded(log_loss)},
        "clv": {
            "average_line_points": _rounded(sum(line_clv) / len(line_clv) if line_clv else None),
            "line_samples": len(line_clv),
            "average_exact_line_probability": _rounded(sum(price_clv) / len(price_clv) if price_clv else None),
            "exact_line_price_samples": len(price_clv),
        },
        "top_pass_reasons": [{"reason": reason, "count": count} for reason, count in pass_reasons.most_common(5)],
        "approval_status": "review_required" if completed >= target else "collecting",
        "notice": "Reaching 100 decisions does not approve the model. Calibration, CLV, leakage, stability, and source failures still require review. No real-money action is taken.",
    }
