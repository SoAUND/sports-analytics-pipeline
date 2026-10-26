"""Phase 6 MLB out-of-sample review and validation reporting.

This module never approves wagering. It summarizes the immutable paper ledger and
forces an explicit review state once the configured OOS target is reached.
"""
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


def _round(value: float | None, places: int = 6) -> float | None:
    return None if value is None else round(float(value), places)


def _pct(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(100.0 * numerator / denominator, 2)


def _edge_band(edge: float | None) -> str:
    if edge is None:
        return "unknown"
    if edge < 0.05:
        return "below_5pct"
    if edge < 0.075:
        return "5_to_7_5pct"
    if edge < 0.10:
        return "7_5_to_10pct"
    return "10pct_plus"


def _probability_band(probability: float | None) -> str:
    if probability is None:
        return "unknown"
    lower = max(0.0, math.floor(probability * 10.0) / 10.0)
    upper = min(1.0, lower + 0.1)
    return f"{lower:.1f}-{upper:.1f}"


def _longest_losing_streak(rows: list[sqlite3.Row]) -> int:
    longest = current = 0
    for row in rows:
        if row["cover_result"] == "loss":
            current += 1
            longest = max(longest, current)
        elif row["cover_result"] in {"win", "push"}:
            current = 0
    return longest


def _maximum_drawdown(rows: list[sqlite3.Row]) -> float:
    cumulative = peak = drawdown = 0.0
    for row in rows:
        profit = float(row["paper_profit_loss"] or 0.0)
        cumulative += profit
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return drawdown


def _group_performance(rows: list[sqlite3.Row], key_fn) -> list[dict[str, Any]]:
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(str(key_fn(row)), []).append(row)
    output: list[dict[str, Any]] = []
    for key in sorted(groups):
        group = groups[key]
        settled = [r for r in group if r["cover_result"] in {"win", "loss", "push"}]
        wins = sum(r["cover_result"] == "win" for r in settled)
        losses = sum(r["cover_result"] == "loss" for r in settled)
        pushes = sum(r["cover_result"] == "push" for r in settled)
        profits = [float(r["paper_profit_loss"] or 0.0) for r in settled]
        denominator = wins + losses
        profit_units = _round(sum(profits))
        output.append({
            "band": key,
            "selections": len(group),
            "settled": len(settled),
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "win_rate_percent": _pct(wins, denominator),
            "profit_units": profit_units,
            "profit_dollars": units_to_dollars(profit_units),
            "roi_percent": None if not settled else round(100.0 * sum(profits) / len(settled), 2),
        })
    return output


def build_mlb_validation_report(
    db: sqlite3.Connection, *, target: int = TARGET_DECISIONS
) -> dict[str, Any]:
    if target <= 0:
        raise ValueError("target must be positive")
    rows = db.execute(
        """
        SELECT pd.*, pr.cover_result, pr.paper_profit_loss, pr.line_clv_points,
               pr.exact_line_price_clv_probability, pr.close_comparison,
               g.kickoff_time
        FROM paper_decisions pd
        JOIN games g ON g.game_id = pd.game_id
        LEFT JOIN paper_results pr ON pr.paper_decision_id = pd.paper_decision_id
        WHERE pd.countable_oos = 1 AND g.sport = 'mlb'
        ORDER BY pd.decision_time, pd.paper_decision_id
        """
    ).fetchall()

    selections = [r for r in rows if r["decision_type"] == "test_only_selection"]
    settled = [r for r in selections if r["cover_result"] in {"win", "loss", "push"}]
    graded = [r for r in settled if r["cover_result"] in {"win", "loss"}]
    wins = sum(r["cover_result"] == "win" for r in graded)
    losses = sum(r["cover_result"] == "loss" for r in graded)
    pushes = sum(r["cover_result"] == "push" for r in settled)
    profits = [float(r["paper_profit_loss"] or 0.0) for r in settled]

    scored = [r for r in graded if r["model_probability"] is not None]
    brier = log_loss = None
    if scored:
        probs = [max(1e-9, min(1 - 1e-9, float(r["model_probability"]))) for r in scored]
        ys = [1.0 if r["cover_result"] == "win" else 0.0 for r in scored]
        brier = sum((p - y) ** 2 for p, y in zip(probs, ys)) / len(scored)
        log_loss = -sum(y * math.log(p) + (1 - y) * math.log(1 - p) for p, y in zip(probs, ys)) / len(scored)

    clv_rows = [r for r in settled if r["exact_line_price_clv_probability"] is not None]
    clv_values = [float(r["exact_line_price_clv_probability"]) for r in clv_rows]
    favorable = sum((r["close_comparison"] == "better") for r in settled)
    worse = sum((r["close_comparison"] == "worse") for r in settled)
    equal = sum((r["close_comparison"] == "equal") for r in settled)

    calibration: list[dict[str, Any]] = []
    buckets: dict[str, list[sqlite3.Row]] = {}
    for row in scored:
        buckets.setdefault(_probability_band(float(row["model_probability"])), []).append(row)
    for band in sorted(buckets):
        bucket = buckets[band]
        expected = sum(float(r["model_probability"]) for r in bucket) / len(bucket)
        actual = sum(r["cover_result"] == "win" for r in bucket) / len(bucket)
        calibration.append({
            "probability_band": band,
            "sample": len(bucket),
            "average_predicted_probability": _round(expected),
            "observed_win_rate": _round(actual),
            "calibration_gap": _round(actual - expected),
        })

    recorded = len(rows)
    settled_share = len(settled) / len(selections) if selections else 0.0
    review_status = "insufficient_sample" if recorded < target else "review_required"
    blockers: list[str] = []
    if recorded < target:
        blockers.append(f"Need {target - recorded} more MLB regular-season OOS decisions before review threshold")
    if recorded >= target and not settled:
        blockers.append("No settled MLB selections are available for performance review")
    if recorded >= target and settled_share < 0.8 and selections:
        blockers.append("Fewer than 80% of MLB selections are settled")
    if recorded >= target and len(clv_rows) < 30:
        blockers.append("Fewer than 30 exact-price CLV samples are available")

    pass_reasons = Counter(str(r["pass_reason"]) for r in rows if r["decision_type"] == "pass")
    profit_units = _round(sum(profits))
    drawdown_units = _round(_maximum_drawdown(settled))
    unit_value = paper_unit_dollars()
    return {
        "mode": "test_only",
        "sport": "mlb",
        "phase": 6,
        "review_status": review_status,
        "automatic_approval": False,
        "target_oos_decisions": target,
        "recorded_oos_decisions": recorded,
        "remaining_to_review_threshold": max(0, target - recorded),
        "passes": sum(r["decision_type"] == "pass" for r in rows),
        "selections": len(selections),
        "settled_selections": len(settled),
        "record": {"wins": wins, "losses": losses, "pushes": pushes},
        "win_rate_percent": _pct(wins, wins + losses),
        "paper_unit_dollars": unit_value,
        "profit_units": profit_units,
        "profit_dollars": units_to_dollars(profit_units, unit_value=unit_value),
        "roi_percent": None if not settled else round(100.0 * sum(profits) / len(settled), 2),
        "maximum_drawdown_units": drawdown_units,
        "maximum_drawdown_dollars": units_to_dollars(drawdown_units, unit_value=unit_value),
        "longest_losing_streak": _longest_losing_streak(settled),
        "calibration": {
            "scored_selections": len(scored),
            "brier_score": _round(brier),
            "log_loss": _round(log_loss),
            "bands": calibration,
        },
        "clv": {
            "exact_price_samples": len(clv_rows),
            "average_probability_clv": _round(sum(clv_values) / len(clv_values) if clv_values else None),
            "better": favorable,
            "equal": equal,
            "worse": worse,
        },
        "edge_band_performance": _group_performance(selections, lambda r: _edge_band(r["probability_edge"])),
        "confidence_performance": _group_performance(selections, lambda r: r["confidence_label"] or "unknown"),
        "top_pass_reasons": [{"reason": reason, "count": count} for reason, count in pass_reasons.most_common(10)],
        "review_blockers": blockers,
        "notice": (
            "Phase 6 is an evidence review only. Reaching 100 MLB OOS decisions does not validate the model, "
            "does not enable wagering, and does not override calibration, CLV, stability, leakage, or source-integrity review."
        ),
    }


def store_mlb_validation_review(
    db: sqlite3.Connection, *, report: dict[str, Any], reviewed_at: datetime | None = None
) -> str:
    reviewed_at = reviewed_at or datetime.now(timezone.utc)
    if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
        raise ValueError("reviewed_at must include a time zone")
    review_id = str(uuid.uuid4())
    with db:
        db.execute(
            """
            INSERT INTO mlb_validation_reviews(
                review_id, review_status, recorded_oos_decisions, settled_selections,
                automatic_approval, report_json, reviewed_at
            ) VALUES (?, ?, ?, ?, 0, ?, ?)
            """,
            (
                review_id,
                report["review_status"],
                int(report["recorded_oos_decisions"]),
                int(report["settled_selections"]),
                json.dumps(report, sort_keys=True),
                reviewed_at.astimezone(timezone.utc).isoformat(),
            ),
        )
    return review_id
