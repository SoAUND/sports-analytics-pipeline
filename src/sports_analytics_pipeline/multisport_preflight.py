"""Truthful readiness checks for our six test-only multi-sport markets."""

from __future__ import annotations

import sqlite3

from .config import Settings
from .multisport_history import history_status
from .multisport_modeling import latest_model
from .multisport_research import verify_source_coverage
from .multisport_runtime import ensure_runtime_schema
from .sports_registry import get_sport_market


SPORTS = ("nba", "wnba", "epl", "mma", "atp", "wta")
MIN_TRAINING_EXAMPLES = 50


def preflight_report(
    connection: sqlite3.Connection,
    *,
    settings: Settings,
) -> dict[str, object]:
    """Separate collection readiness from model/decision readiness.

    Historical examples and a trained model are required before a sport can produce
    model-based paper decisions. Research source contracts must exist, but event-specific
    research evidence is checked again at decision time. A pending controlled-market
    settlement rule blocks that market rather than allowing the program to guess.
    """
    ensure_runtime_schema(connection)
    histories = history_status(connection)
    global_blockers: list[str] = []
    if settings.bot_mode not in {"research", "test_only"}:
        global_blockers.append("Unsupported research mode")
    if not settings.odds_api_key:
        global_blockers.append("THE_ODDS_API_KEY is not configured")
    if not settings.sportsbook_lists_approved():
        global_blockers.append("Accessible and consensus sportsbook lists are not configured")

    sports: dict[str, object] = {}
    for sport in SPORTS:
        spec = get_sport_market(sport)
        model = latest_model(connection, sport)
        settlement = connection.execute(
            """
            SELECT rule_status, rule_key, source_url, notes
            FROM multisport_settlement_rules
            WHERE sport=? AND market_key=? AND period=?
            ORDER BY CASE WHEN rule_status='verified' THEN 0 ELSE 1 END,
                     verified_at DESC
            LIMIT 1
            """,
            (sport, spec.internal_market_key, spec.period),
        ).fetchone()
        coverage_ok, missing_categories = verify_source_coverage(sport)
        blockers: list[str] = list(global_blockers)
        history_count = int(histories[sport])
        if history_count < MIN_TRAINING_EXAMPLES:
            blockers.append(
                f"Historical training examples {history_count}/{MIN_TRAINING_EXAMPLES}"
            )
        if model is None:
            blockers.append("No trained test-only model")
        if not coverage_ok:
            blockers.append(
                "Research source directory missing categories: " + ", ".join(missing_categories)
            )
        settlement_status = str(settlement["rule_status"]) if settlement else "missing"
        if settlement_status != "verified":
            blockers.append(f"Controlled market settlement rule is {settlement_status}")

        sports[sport] = {
            "market": spec.internal_market_key,
            "period": spec.period,
            "historical_training_examples": history_count,
            "training_minimum": MIN_TRAINING_EXAMPLES,
            "training_ready": history_count >= MIN_TRAINING_EXAMPLES,
            "model_version": str(model["model_version"]) if model else None,
            "model_status": str(model["status"]) if model else "not_trained",
            "research_source_contract_complete": coverage_ok,
            "settlement_status": settlement_status,
            "settlement_rule": str(settlement["rule_key"]) if settlement else None,
            "collection_ready": not any(
                item in blockers
                for item in (
                    "THE_ODDS_API_KEY is not configured",
                    "Accessible and consensus sportsbook lists are not configured",
                    "Unsupported research mode",
                )
            ),
            "decision_ready": not blockers,
            "blockers": tuple(blockers),
        }

    return {
        "global": {
            "bot_mode": settings.bot_mode,
            "odds_api_key_configured": bool(settings.odds_api_key),
            "sportsbook_lists_configured": settings.sportsbook_lists_approved(),
            "odds_api_min_remaining_credits": settings.odds_api_min_remaining_credits,
            "odds_horizon_minutes": settings.multisport_odds_horizon_minutes,
            "automatic_wagering": False,
        },
        "sports": sports,
        "all_collection_ready": all(bool(row["collection_ready"]) for row in sports.values()),
        "all_decision_ready": all(bool(row["decision_ready"]) for row in sports.values()),
    }

