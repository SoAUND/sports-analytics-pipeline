from decimal import Decimal

from sports_analytics_pipeline.market_expression import (
    candidates_from_rows,
    market_expression_definition,
    scan_market_expressions,
)

D = Decimal


def _row(
    key: str,
    market_key: str,
    odds: str,
    probability: str,
    *,
    sport: str = "ncaaf",
    event_id: str = "event-1",
    thesis_key: str = "event-1:favorite",
    edge: str = "0.02",
    ev: str = "0.04",
    information_quality: str = "complete_current",
    stability_status: str = "stable",
):
    return {
        "selection_id": key,
        "sport": sport,
        "event_id": event_id,
        "thesis_key": thesis_key,
        "market_key": market_key,
        "decimal_odds": odds,
        "conservative_probability": probability,
        "probability_edge": edge,
        "ev": ev,
        "information_quality": information_quality,
        "stability_status": stability_status,
        "selection_name": "Favorite",
    }


def test_market_expression_labels_cover_multiple_sport_market_families():
    assert market_expression_definition("spreads") == ("full_event", "Full-Event Spread")
    assert market_expression_definition("alternate_spreads") == ("full_event", "Alternate Spread")
    assert market_expression_definition("first_half_spread") == ("first_half", "First-Half Spread")
    assert market_expression_definition("first_quarter_spread") == ("first_quarter", "First-Quarter Spread")
    assert market_expression_definition("first_five_total") == ("first_five", "First-Five Total")
    assert market_expression_definition("round_total") == ("rounds", "Round Total")
    assert market_expression_definition("set_spread") == ("set", "Set Spread")
    assert market_expression_definition("map_total") == ("map", "Map Total")
    assert market_expression_definition("player_prop") == ("full_event", "Player Prop")


def test_scanner_compares_multiple_expressions_of_same_thesis():
    result = scan_market_expressions([
        _row("ml", "moneyline", "1.35", "0.77", edge="0.01", ev="0.0395"),
        _row("fg", "spreads", "1.55", "0.69", edge="0.045", ev="0.0695"),
        _row("h1", "first_half_spread", "1.91", "0.64", edge="0.116", ev="0.2224"),
        _row("q1", "first_quarter_spread", "1.91", "0.58", edge="0.056", ev="0.1078"),
    ])
    assert result["scope"] == "sport_agnostic"
    assert result["candidate_count"] == 4
    assert result["eligible_count"] == 4
    assert result["best_by_thesis"] == ["h1"]
    assert result["ranked"][0]["label"] == "First-Half Spread"


def test_scanner_accepts_different_sports_without_sport_specific_strategy_code():
    result = scan_market_expressions([
        _row("mlb", "first_five_total", "1.80", "0.62", sport="mlb", event_id="mlb-1", thesis_key="mlb-1:under"),
        _row("mma", "round_total", "1.70", "0.66", sport="mma", event_id="mma-1", thesis_key="mma-1:over"),
        _row("tennis", "set_spread", "1.85", "0.61", sport="atp", event_id="atp-1", thesis_key="atp-1:favorite"),
        _row("nba", "player_points", "1.90", "0.60", sport="nba", event_id="nba-1", thesis_key="nba-1:player"),
    ])
    assert result["eligible_count"] == 4
    assert set(result["sports_seen"]) == {"mlb", "mma", "atp", "nba"}


def test_scanner_rejects_unresearched_derivative():
    candidate = candidates_from_rows([
        _row(
            "h1",
            "first_half_spread",
            "1.91",
            "0.70",
            information_quality="missing",
        )
    ])[0]
    assert candidate.eligible is False
    assert scan_market_expressions([candidate.source])["eligible_count"] == 0


def test_scanner_enforces_win_rate_price_range():
    too_expensive = candidates_from_rows([_row("a", "spreads", "1.20", "0.86")])[0]
    too_long = candidates_from_rows([_row("b", "spreads", "4.10", "0.60")])[0]
    assert too_expensive.eligible is False
    assert too_long.eligible is False


def test_scanner_does_not_invent_probability_when_missing():
    result = scan_market_expressions([
        {
            "selection_id": "missing-probability",
            "sport": "ncaaf",
            "event_id": "game-1",
            "market_key": "first_quarter_spread",
            "decimal_odds": "1.91",
            "information_quality": "complete_current",
            "stability_status": "stable",
        }
    ])
    assert result["candidate_count"] == 1
    assert result["eligible_count"] == 0
    assert result["ranked"] == []


def test_same_event_parlay_stays_locked_without_joint_probability():
    result = scan_market_expressions([_row("a", "spreads", "1.91", "0.62")])
    assert result["same_event_parlay_rule"] == "blocked_without_explicit_joint_probability"
