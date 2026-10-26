"""Phase 10 MLB final out-of-sample review.

This module turns the Phase 6 evidence report into a stricter final review packet.
It can recommend revise, retire, or eligibility for manual approval review, but it
never approves wagering, changes BOT_MODE, or activates a model automatically.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import sqlite3
import uuid
from typing import Any

from .mlb_validation import TARGET_DECISIONS, build_mlb_validation_report


def _safe_probability(value: float) -> float:
    return max(1e-9, min(1.0 - 1e-9, float(value)))


def _score_binary(rows: list[sqlite3.Row], field: str) -> dict[str, Any]:
    usable = [r for r in rows if r[field] is not None and r["cover_result"] in {"win", "loss"}]
    if not usable:
        return {"samples": 0, "brier_score": None, "log_loss": None}
    probabilities = [_safe_probability(r[field]) for r in usable]
    outcomes = [1.0 if r["cover_result"] == "win" else 0.0 for r in usable]
    brier = sum((p - y) ** 2 for p, y in zip(probabilities, outcomes)) / len(usable)
    log_loss = -sum(y * math.log(p) + (1.0 - y) * math.log(1.0 - p) for p, y in zip(probabilities, outcomes)) / len(usable)
    return {
        "samples": len(usable),
        "brier_score": round(brier, 6),
        "log_loss": round(log_loss, 6),
    }


def _integrity_audit(db: sqlite3.Connection) -> dict[str, int]:
    future_prices = db.execute(
        """
        SELECT COUNT(*)
        FROM paper_decisions pd
        JOIN games g ON g.game_id = pd.game_id
        LEFT JOIN odds_snapshots o ON o.odds_snapshot_id = pd.selected_odds_snapshot_id
        WHERE g.sport = 'mlb' AND pd.countable_oos = 1
          AND pd.decision_type = 'test_only_selection'
          AND (
              (pd.price_time IS NOT NULL AND pd.price_time > pd.decision_time)
              OR (o.source_updated_at IS NOT NULL AND o.source_updated_at > pd.decision_time)
              OR (o.collected_at IS NOT NULL AND o.collected_at > pd.decision_time)
          )
        """
    ).fetchone()[0]
    source_game_mismatches = db.execute(
        """
        SELECT COUNT(*)
        FROM paper_decisions pd
        JOIN games g ON g.game_id = pd.game_id
        JOIN decisions d ON d.decision_id = pd.source_decision_id
        WHERE g.sport = 'mlb' AND pd.countable_oos = 1 AND d.game_id <> pd.game_id
        """
    ).fetchone()[0]
    missing_selection_prices = db.execute(
        """
        SELECT COUNT(*)
        FROM paper_decisions pd JOIN games g ON g.game_id=pd.game_id
        WHERE g.sport='mlb' AND pd.countable_oos=1
          AND pd.decision_type='test_only_selection'
          AND (pd.selected_odds_snapshot_id IS NULL OR pd.decimal_odds IS NULL OR pd.selected_team_id IS NULL)
        """
    ).fetchone()[0]
    return {
        "future_price_leaks": int(future_prices),
        "source_game_mismatches": int(source_game_mismatches),
        "selection_price_identity_failures": int(missing_selection_prices),
    }


def _selection_rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT pd.*, pr.cover_result, pr.paper_profit_loss,
               pr.exact_line_price_clv_probability, pr.close_comparison,
               d.no_vig_probability
        FROM paper_decisions pd
        JOIN games g ON g.game_id = pd.game_id
        LEFT JOIN paper_results pr ON pr.paper_decision_id = pd.paper_decision_id
        LEFT JOIN decisions d ON d.decision_id = pd.source_decision_id
        WHERE g.sport='mlb' AND pd.countable_oos=1
          AND pd.decision_type='test_only_selection'
        ORDER BY pd.decision_time, pd.paper_decision_id
        """
    ).fetchall()


def _concentration(rows: list[sqlite3.Row]) -> dict[str, Any]:
    settled = [r for r in rows if r["cover_result"] in {"win", "loss", "push"}]
    profits = [float(r["paper_profit_loss"] or 0.0) for r in settled]
    total_positive = sum(max(0.0, x) for x in profits)
    best_window = 0.0
    window_size = min(20, len(profits))
    if window_size:
        for start in range(0, len(profits) - window_size + 1):
            best_window = max(best_window, sum(max(0.0, x) for x in profits[start:start + window_size]))
    share = None if total_positive <= 0 else best_window / total_positive
    return {
        "settled_selections": len(settled),
        "window_size": window_size,
        "largest_window_share_of_positive_profit": None if share is None else round(share, 6),
        "concentration_warning": bool(share is not None and len(settled) >= 40 and share > 0.75),
    }


def build_mlb_final_review(
    db: sqlite3.Connection,
    *,
    target: int = TARGET_DECISIONS,
    minimum_settled_share: float = 0.80,
    minimum_clv_samples: int = 30,
    minimum_scored_samples: int = 30,
) -> dict[str, Any]:
    if target <= 0:
        raise ValueError("target must be positive")
    if not 0 < minimum_settled_share <= 1:
        raise ValueError("minimum_settled_share must be in (0, 1]")
    if minimum_clv_samples < 0 or minimum_scored_samples < 0:
        raise ValueError("minimum sample requirements cannot be negative")

    evidence = build_mlb_validation_report(db, target=target)
    rows = _selection_rows(db)
    settled = [r for r in rows if r["cover_result"] in {"win", "loss", "push"}]
    graded = [r for r in rows if r["cover_result"] in {"win", "loss"}]
    integrity = _integrity_audit(db)
    model_score = _score_binary(graded, "model_probability")
    market_score = _score_binary(graded, "no_vig_probability")
    concentration = _concentration(rows)

    settled_share = len(settled) / len(rows) if rows else 0.0
    blockers: list[str] = []
    warnings: list[str] = []

    if evidence["recorded_oos_decisions"] < target:
        blockers.append(f"Need {target - evidence['recorded_oos_decisions']} more MLB regular-season OOS decisions")
    if evidence["recorded_oos_decisions"] >= target and rows and settled_share < minimum_settled_share:
        blockers.append(f"Settled selection share is below {minimum_settled_share:.0%}")
    if evidence["recorded_oos_decisions"] >= target and evidence["clv"]["exact_price_samples"] < minimum_clv_samples:
        blockers.append(f"Need at least {minimum_clv_samples} exact-price CLV samples")
    if evidence["recorded_oos_decisions"] >= target and model_score["samples"] < minimum_scored_samples:
        blockers.append(f"Need at least {minimum_scored_samples} settled model-probability samples")
    if any(integrity.values()):
        blockers.append("Material prediction-time or identity integrity failures were detected")
    if concentration["concentration_warning"]:
        warnings.append("A large share of positive paper profit came from one short sequence of selections")
    if market_score["samples"] < minimum_scored_samples and evidence["recorded_oos_decisions"] >= target:
        warnings.append("Market-baseline comparison has limited matched no-vig samples")

    comparison = {
        "matched_samples": min(model_score["samples"], market_score["samples"]),
        "model": model_score,
        "no_vig_market_baseline": market_score,
        "model_brier_improvement": None,
        "model_log_loss_improvement": None,
    }
    if model_score["brier_score"] is not None and market_score["brier_score"] is not None:
        comparison["model_brier_improvement"] = round(market_score["brier_score"] - model_score["brier_score"], 6)
        comparison["model_log_loss_improvement"] = round(market_score["log_loss"] - model_score["log_loss"], 6)

    recorded = evidence["recorded_oos_decisions"]
    avg_clv = evidence["clv"]["average_probability_clv"]
    roi = evidence["roi_percent"]
    severe_negative = (
        recorded >= target
        and not blockers
        and len(settled) >= minimum_scored_samples
        and roi is not None and roi <= -5.0
        and avg_clv is not None and avg_clv < 0.0
        and (
            comparison["model_brier_improvement"] is None
            or comparison["model_brier_improvement"] <= 0.0
        )
    )

    if recorded < target:
        recommendation = "insufficient_sample"
    elif blockers:
        recommendation = "revise"
    elif severe_negative:
        recommendation = "retire"
    else:
        recommendation = "eligible_for_manual_approval_review"

    return {
        "mode": "test_only",
        "sport": "mlb",
        "phase": 10,
        "automatic_approval": False,
        "automatic_wagering": False,
        "target_oos_decisions": target,
        "recommendation": recommendation,
        "manual_decision_required": recommendation != "insufficient_sample",
        "evidence": evidence,
        "settlement": {
            "selections": len(rows),
            "settled": len(settled),
            "settled_share": round(settled_share, 6),
        },
        "integrity_audit": integrity,
        "model_vs_market_baseline": comparison,
        "result_concentration": concentration,
        "review_blockers": blockers,
        "review_warnings": warnings,
        "allowed_manual_dispositions": ["approve_for_continued_test_only_use", "revise", "retire"],
        "notice": (
            "Phase 10 is the final MLB evidence review. A recommendation never takes a real-money action, never changes test_only mode, "
            "and never approves real-money use automatically. Any approval decision remains manual and must be documented."
        ),
    }


def store_mlb_final_review(
    db: sqlite3.Connection,
    *,
    report: dict[str, Any],
    reviewed_at: datetime | None = None,
) -> str:
    reviewed_at = reviewed_at or datetime.now(timezone.utc)
    if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
        raise ValueError("reviewed_at must include a time zone")
    review_id = str(uuid.uuid4())
    with db:
        db.execute(
            """
            INSERT INTO mlb_final_reviews(
                final_review_id, recommendation, recorded_oos_decisions,
                automatic_approval, automatic_wagering, report_json, reviewed_at
            ) VALUES (?, ?, ?, 0, 0, ?, ?)
            """,
            (
                review_id,
                report["recommendation"],
                int(report["evidence"]["recorded_oos_decisions"]),
                json.dumps(report, sort_keys=True),
                reviewed_at.astimezone(timezone.utc).isoformat(),
            ),
        )
    return review_id


def record_manual_mlb_disposition(
    db: sqlite3.Connection,
    *,
    final_review_id: str,
    disposition: str,
    reason: str,
    actor: str,
    decided_at: datetime | None = None,
) -> str:
    allowed = {"approve_for_continued_test_only_use", "revise", "retire"}
    if disposition not in allowed:
        raise ValueError("unsupported manual disposition")
    if not reason.strip() or not actor.strip():
        raise ValueError("manual disposition requires reason and actor")
    decided_at = decided_at or datetime.now(timezone.utc)
    if decided_at.tzinfo is None or decided_at.utcoffset() is None:
        raise ValueError("decided_at must include a time zone")
    review = db.execute("SELECT final_review_id FROM mlb_final_reviews WHERE final_review_id=?", (final_review_id,)).fetchone()
    if review is None:
        raise ValueError("unknown final_review_id")
    disposition_id = str(uuid.uuid4())
    with db:
        db.execute(
            """
            INSERT INTO mlb_manual_dispositions(
                disposition_id, final_review_id, disposition, reason, actor,
                real_money_authorized, automatic_wagering, decided_at
            ) VALUES (?, ?, ?, ?, ?, 0, 0, ?)
            """,
            (disposition_id, final_review_id, disposition, reason.strip(), actor.strip(), decided_at.astimezone(timezone.utc).isoformat()),
        )
    return disposition_id
