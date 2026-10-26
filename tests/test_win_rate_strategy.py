from decimal import Decimal

from sports_analytics_pipeline.win_rate_strategy import (
    MIN_TICKET_DECIMAL_ODDS,
    WinRateCandidate,
    apply_win_rate_first_strategy,
    build_win_rate_parlays,
    decimal_to_american,
)

D = Decimal


def _candidate(key: str, odds: str, probability: str, edge: str = "0.01") -> WinRateCandidate:
    decimal_odds = D(odds)
    conservative = D(probability)
    return WinRateCandidate(
        key=key,
        market="moneyline",
        decimal_odds=decimal_odds,
        conservative_probability=conservative,
        break_even_probability=D("1") / decimal_odds,
        ev=conservative * decimal_odds - D("1"),
        probability_edge=D(edge),
        information_quality="complete_current",
        stability_status="stable",
        source={"game_id": key},
    )


def test_minus_400_is_allowed_but_more_expensive_is_not():
    at_limit = _candidate("a", "1.25", "0.82")
    too_expensive = _candidate("b", "1.20", "0.86")
    assert at_limit.odds_eligible is True
    assert decimal_to_american(at_limit.decimal_odds) == -400
    assert too_expensive.odds_eligible is False


def test_plus_300_is_allowed_but_longer_price_is_not():
    at_limit = _candidate("a", "4.00", "0.55")
    too_long = _candidate("b", "4.10", "0.55")
    assert at_limit.odds_eligible is True
    assert decimal_to_american(at_limit.decimal_odds) == 300
    assert too_long.odds_eligible is False


def test_high_probability_expensive_favorite_remains_visible_as_candidate():
    candidate = _candidate("botic-like", "1.28", "0.7373", edge="-0.0439")
    assert candidate.candidate_eligible is True
    assert candidate.probability_band == "premium"
    assert candidate.price_quality == "expensive"
    assert candidate.value_positive is False


def test_low_probability_candidate_is_rejected_even_at_plus_money():
    assert _candidate("dog", "2.50", "0.49").candidate_eligible is False


def test_builder_uses_fewest_legs_that_reach_minimum_4x():
    candidates = [
        _candidate("a", "1.35", "0.76"),
        _candidate("b", "1.45", "0.72"),
        _candidate("c", "1.60", "0.66"),
        _candidate("d", "1.80", "0.61"),
    ]
    cards = build_win_rate_parlays(candidates)
    assert len(cards) == 1
    assert cards[0]["leg_count"] == 4
    assert D(cards[0]["decimal_odds"]) >= MIN_TICKET_DECIMAL_ODDS
    assert cards[0]["meets_minimum_4x"] is True


def test_two_leg_ticket_is_preferred_when_it_already_reaches_4x():
    candidates = [
        _candidate("a", "2.10", "0.60"),
        _candidate("b", "2.00", "0.60"),
        _candidate("c", "1.60", "0.66"),
    ]
    cards = build_win_rate_parlays(candidates)
    assert len(cards) == 1
    assert cards[0]["leg_count"] == 2
    assert D(cards[0]["decimal_odds"]) >= D("4.00")


def test_no_4x_ticket_is_returned_instead_of_forcing_one():
    candidates = [
        _candidate("a", "1.25", "0.82"),
        _candidate("b", "1.30", "0.80"),
        _candidate("c", "1.35", "0.76"),
    ]
    assert build_win_rate_parlays(candidates) == []


def test_same_event_parlay_is_not_built_without_joint_model():
    a = _candidate("a", "2.10", "0.60")
    b = WinRateCandidate(
        key="b", market="total", decimal_odds=D("2.00"), conservative_probability=D("0.60"),
        break_even_probability=D("0.50"), ev=D("0.20"), probability_edge=D("0.10"),
        information_quality="complete_current", stability_status="stable", source={"game_id": "a"},
    )
    assert build_win_rate_parlays([a, b]) == []


def test_strategy_keeps_win_probability_and_price_quality_separate():
    evaluation = {
        "test_only_selections": [{
            "selection_id": "favorite", "market": "moneyline", "decimal_odds": 1.28,
            "conservative_probability": 0.7373, "break_even_probability": 0.78125,
            "probability_edge": -0.04395, "ev": -0.0562,
            "information_quality": "complete_current", "stability_status": "stable",
        }]
    }
    result = apply_win_rate_first_strategy(evaluation)
    row = result["ranked_candidates"][0]
    assert result["mode"] == "test_only"
    assert result["minimum_ticket_decimal_odds"] == "4.00"
    assert result["ticket_status"] == "no_4x_ticket"
    assert row["probability_band"] == "premium"
    assert row["price_quality"] == "expensive"
    assert row["status"] == "win_rate_candidate"
    assert row["value_positive"] is False
