from sports_analytics_pipeline.win_rate_reporting import build_win_rate_report, classify_selection


def _selection(name, odds, probability_percent, edge_pp, ev_percent, *, event_id, confidence="High-Confidence Test Selection"):
    return {
        "decision_type": "test_only_selection",
        "confidence_label": confidence,
        "sport": "atp",
        "event_id": event_id,
        "market_key": "match_moneyline",
        "selection_name": name,
        "outcome_role": "away",
        "decimal_odds": odds,
        "conservative_probability_percent": probability_percent,
        "edge_percentage_points": edge_pp,
        "expected_value_percent": ev_percent,
    }


def test_botic_like_price_is_visible_but_watch_only_when_safeguard_failed():
    row = _selection("Botic-like Favorite", 1.28, 73.73, -4.39, -5.62, event_id="event-a", confidence="Unqualified Test Observation")
    result = classify_selection(row)
    assert result["american_odds"] == -357
    assert result["probability_band"] == "premium"
    assert result["price_quality"] == "expensive"
    assert result["status"] == "watch_only"
    assert result["research_safeguard_clear"] is False


def test_safeguard_cleared_high_probability_selection_is_test_candidate():
    result = classify_selection(_selection("Favorite A", 1.40, 74.0, 2.57, 3.6, event_id="event-a"))
    assert result["status"] == "test_candidate"
    assert result["probability_band"] == "premium"


def test_report_refuses_sub_4x_two_leg_card():
    status = {
        "sports": {
            "atp": {"historical_training_examples": 20, "training_minimum": 50, "completed_oos": 5, "validation_target": 100, "model_status": "development", "best_current_paper_selection_display": _selection("Favorite A", 1.40, 74.0, 2.57, 3.6, event_id="event-a")},
            "wta": {"historical_training_examples": 18, "training_minimum": 50, "completed_oos": 4, "validation_target": 100, "model_status": "development", "best_current_paper_selection_display": {**_selection("Favorite B", 1.50, 70.0, 3.33, 5.0, event_id="event-b"), "sport": "wta"}},
        }
    }
    report = build_win_rate_report(status)
    assert "Eligible individual price range: -400 through +300" in report
    assert "Minimum ticket return: 4.00x total return (+300 combined odds)" in report
    assert "NO 4X TICKET" in report
    assert "Win-Rate Lane: TEST CANDIDATE" in report
    assert "The existing expected-value qualification lane remains unchanged." in report


def test_report_builds_two_leg_ticket_when_combined_odds_reach_4x():
    status = {
        "sports": {
            "atp": {"best_current_paper_selection_display": _selection("Pick A", 2.10, 60.0, 12.38, 26.0, event_id="a")},
            "wta": {"best_current_paper_selection_display": {**_selection("Pick B", 2.00, 60.0, 10.0, 20.0, event_id="b"), "sport": "wta"}},
        }
    }
    report = build_win_rate_report(status)
    assert "2-Leg:" in report
    assert "Total Return Multiple: 4.20x" in report
    assert "$10 Test Risk → Total Return $42.00 | Profit $32.00" in report


def test_same_event_candidates_do_not_create_parlay_card():
    first = _selection("Favorite A", 2.10, 60.0, 12.38, 26.0, event_id="same-event")
    second = {**_selection("Favorite B", 2.00, 60.0, 10.0, 20.0, event_id="same-event"), "sport": "wta"}
    status = {"sports": {"atp": {"best_current_paper_selection_display": first}, "wta": {"best_current_paper_selection_display": second}}}
    report = build_win_rate_report(status)
    assert "NO 4X TICKET" in report
