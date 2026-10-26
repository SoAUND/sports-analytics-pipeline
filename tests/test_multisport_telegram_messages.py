import unittest
from datetime import datetime, timedelta, timezone

from scripts.build_multisport_telegram_messages import (
    build_messages,
    build_multisport_summary,
    parse_sports,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 30, 13, 30, tzinfo=UTC)


class MultiSportTelegramMessageTests(unittest.TestCase):
    def sample_status(self):
        return {
            "automatic_wagering": False,
            "sports": {
                "nba": {"model_status": "not_trained", "historical_training_examples": 12, "training_minimum": 50, "completed_oos": 0, "validation_target": 100},
                "wnba": {"model_status": "not_trained", "historical_training_examples": 18, "training_minimum": 50, "completed_oos": 0, "validation_target": 100},
                "epl": {"model_status": "test_only", "historical_training_examples": 50, "training_minimum": 50, "completed_oos": 8, "validation_target": 100},
                "mma": {"model_status": "not_trained", "historical_training_examples": 4, "training_minimum": 50, "completed_oos": 0, "validation_target": 100},
                "atp": {"model_status": "not_trained", "historical_training_examples": 20, "training_minimum": 50, "completed_oos": 0, "validation_target": 100},
                "wta": {"model_status": "not_trained", "historical_training_examples": 22, "training_minimum": 50, "completed_oos": 0, "validation_target": 100},
            },
        }

    def test_each_supported_sport_gets_its_own_clean_body_without_duplicate_title(self):
        status = self.sample_status()
        sports = parse_sports("nba,wnba,epl,mma,atp,wta")
        messages = build_messages(status, sports)
        self.assertEqual(set(messages), {"nba", "wnba", "epl", "mma", "atp", "wta"})
        self.assertNotIn("🏀 NBA Test Result", messages["nba"])
        self.assertNotIn("⚽ EPL / Soccer Test Result", messages["epl"])
        self.assertIn("Status: COLLECTING", messages["nba"])
        self.assertIn("Training: 12/50 verified examples", messages["nba"])
        self.assertIn("No real-money action is taken.", messages["wta"])
        self.assertNotIn('{', messages["nba"])
        self.assertNotIn('"model_status"', messages["nba"])

    def test_qualifying_selection_uses_readable_match_and_start(self):
        status = {
            "sports": {
                "epl": {
                    "model_status": "test_only",
                    "historical_training_examples": 64,
                    "training_minimum": 50,
                    "completed_oos": 14,
                    "validation_target": 100,
                }
            },
            "best_current_paper_selection_display": {
                "decision_type": "test_only_selection",
                "sport": "epl",
                "event_id": "epl:arsenal-brighton",
                "market_key": "three_way_moneyline",
                "period": "full_game",
                "selection_name": "Arsenal",
                "outcome_role": "home",
                "points": None,
                "sportsbook": "hardrockbet",
                "american_odds": -145,
                "check_time": (NOW - timedelta(minutes=5)).isoformat(),
                "event_commence_time": "2026-08-30T14:00:00+00:00",
                "event_status": "scheduled",
                "odds_collected_at": (NOW - timedelta(minutes=5)).isoformat(),
                "odds_market_status": "open",
                "selection_settled": False,
                "model_probability_percent": 64.2,
                "conservative_probability_percent": 59.2,
                "edge_percentage_points": 5.0,
                "expected_value_percent": 7.4,
                "pick_strength_score": 81,
                "letter_grade": "B-",
                "pass_reason": None,
            },
        }

        def lookup(event_id):
            self.assertEqual(event_id, "epl:arsenal-brighton")
            return {
                "home_name": "Arsenal",
                "away_name": "Brighton",
                "commence_time": "2026-08-30T14:00:00+00:00",
            }

        message = build_messages(
            status,
            parse_sports("epl"),
            event_lookup=lookup,
            as_of=NOW,
        )["epl"]
        self.assertIn("Status: READY", message)
        self.assertIn("🚨 BOT LEAN: ARSENAL ML", message)
        self.assertIn("Market: 3-Way Moneyline (ML)", message)
        self.assertIn("Team: Arsenal", message)
        self.assertIn("Matchup Position: HOME", message)
        self.assertIn("Match: Arsenal vs Brighton", message)
        self.assertIn("Start: Sun Aug 30, 10:00 AM ET", message)
        self.assertNotIn("Event: epl:arsenal-brighton", message)
        self.assertNotIn("Pick: HOME", message)
        self.assertIn("Odds: -145", message)
        self.assertIn("Conservative Edge: 5.00 pp", message)
        self.assertIn("Expected Value: 7.40%", message)
        self.assertIn("Pick Strength: 81/100", message)
        self.assertIn("Grade: B-", message)
        self.assertIn("Result: TEST ONLY SELECTION", message)

    def test_mma_moneyline_names_the_fighter_instead_of_only_away(self):
        status = {
            "sports": {"mma": {"model_status": "test_only"}},
            "best_current_paper_selection_display": {
                "decision_type": "test_only_selection",
                "sport": "mma",
                "event_id": "mma:sanchez-mccracken",
                "market_key": "moneylines",
                "period": "full_game",
                "selection_name": "Liam Mccracken",
                "outcome_role": "away",
                "points": None,
                "sportsbook": "betonlineag",
                "american_odds": -110,
                "check_time": (NOW - timedelta(minutes=5)).isoformat(),
                "event_commence_time": (NOW + timedelta(minutes=30)).isoformat(),
                "event_status": "scheduled",
                "odds_collected_at": (NOW - timedelta(minutes=5)).isoformat(),
                "odds_market_status": "open",
                "selection_settled": False,
                "confidence_label": "Unqualified Test Observation",
            },
        }

        message = build_messages(status, parse_sports("mma"), as_of=NOW)["mma"]
        self.assertIn("🚨 BOT LEAN: LIAM MCCRACKEN ML", message)
        self.assertIn("Market: Fight Moneyline (ML)", message)
        self.assertIn("Fighter: Liam Mccracken", message)
        self.assertIn("Matchup Position: AWAY", message)
        self.assertNotIn("Pick: AWAY", message)

    def test_tennis_moneyline_names_the_player_instead_of_only_away(self):
        status = {
            "sports": {"atp": {"model_status": "test_only"}},
            "best_current_paper_selection_display": {
                "decision_type": "test_only_selection",
                "sport": "atp",
                "event_id": "atp:choinski-zandschulp",
                "market_key": "match_moneyline",
                "period": "full_game",
                "selection_name": "Botic van de Zandschulp",
                "outcome_role": "away",
                "points": None,
                "sportsbook": "pinnacle",
                "american_odds": -357,
                "check_time": (NOW - timedelta(minutes=5)).isoformat(),
                "event_commence_time": (NOW + timedelta(minutes=30)).isoformat(),
                "event_status": "scheduled",
                "odds_collected_at": (NOW - timedelta(minutes=5)).isoformat(),
                "odds_market_status": "open",
                "selection_settled": False,
                "confidence_label": "Unqualified Test Observation",
            },
        }

        message = build_messages(status, parse_sports("atp"), as_of=NOW)["atp"]
        self.assertIn("🚨 BOT LEAN: BOTIC VAN DE ZANDSCHULP ML", message)
        self.assertIn("Market: Match Moneyline (ML)", message)
        self.assertIn("Player: Botic van de Zandschulp", message)
        self.assertIn("Matchup Position: AWAY", message)
        self.assertNotIn("Pick: AWAY", message)

    def test_spread_names_the_team_and_exact_signed_line(self):
        status = {
            "sports": {"nba": {"model_status": "test_only"}},
            "best_current_paper_selection_display": {
                "decision_type": "test_only_selection",
                "sport": "nba",
                "event_id": "nba:boston-new-york",
                "market_key": "spreads",
                "period": "full_game",
                "selection_name": "Boston Celtics",
                "outcome_role": "home",
                "points": -3.5,
                "sportsbook": "pinnacle",
                "american_odds": -110,
                "check_time": (NOW - timedelta(minutes=5)).isoformat(),
                "event_commence_time": (NOW + timedelta(minutes=30)).isoformat(),
                "event_status": "scheduled",
                "odds_collected_at": (NOW - timedelta(minutes=5)).isoformat(),
                "odds_market_status": "open",
                "selection_settled": False,
            },
        }

        message = build_messages(status, parse_sports("nba"), as_of=NOW)["nba"]
        self.assertIn("🚨 BOT LEAN: BOSTON CELTICS -3.5", message)
        self.assertIn("Market: Full-Game Spread", message)
        self.assertIn("Team: Boston Celtics", message)
        self.assertIn("Line: -3.5", message)
        self.assertIn("Matchup Position: HOME", message)

    def test_future_event_outside_horizon_does_not_report_stale_research_blocker(self):
        now = datetime(2026, 8, 29, 22, 39, tzinfo=UTC)
        status = {
            "sports": {
                "epl": {
                    "model_status": "test_only",
                    "historical_training_examples": 50,
                    "training_minimum": 50,
                    "completed_oos": 1,
                    "validation_target": 100,
                    "next_event_live_readiness": {
                        "event_id": "epl:next",
                        "ready": False,
                        "blockers": ["official_status:missing", "current_odds:missing"],
                    },
                }
            }
        }

        def lookup(_event_id):
            return {
                "home_name": "Chelsea",
                "away_name": "Fulham",
                "commence_time": (now + timedelta(hours=10)).isoformat(),
            }

        message = build_messages(
            status,
            parse_sports("epl"),
            event_lookup=lookup,
            horizon_minutes=90,
            as_of=now,
        )["epl"]
        self.assertIn("Status: NO ELIGIBLE MATCHES", message)
        self.assertIn("Match: Chelsea vs Fulham", message)
        self.assertIn("No EPL event is inside the 90-minute evaluation window.", message)
        self.assertNotIn("official_status:missing", message)
        self.assertNotIn("Official match status could not be verified.", message)

    def test_in_horizon_blocker_is_translated_to_plain_language(self):
        now = datetime(2026, 8, 29, 22, 39, tzinfo=UTC)
        status = {
            "sports": {
                "epl": {
                    "model_status": "test_only",
                    "next_event_live_readiness": {
                        "event_id": "epl:next",
                        "ready": False,
                        "blockers": ["official_status:missing"],
                    },
                }
            }
        }

        def lookup(_event_id):
            return {
                "home_name": "Chelsea",
                "away_name": "Fulham",
                "commence_time": (now + timedelta(minutes=45)).isoformat(),
            }

        message = build_messages(status, parse_sports("epl"), event_lookup=lookup, as_of=now)["epl"]
        self.assertIn("Status: PASS", message)
        self.assertIn("Why No Pick: Official match status could not be verified.", message)
        self.assertNotIn("official_status:missing", message)

    def test_multisport_summary_is_clean_dashboard_body_not_raw_json(self):
        status = self.sample_status()
        status["best_current_paper_selection_display"] = {
            "decision_type": "test_only_selection",
            "sport": "epl",
            "event_id": "epl:arsenal-brighton",
            "market_key": "three_way_moneyline",
            "period": "full_game",
            "selection_name": "Arsenal",
            "outcome_role": "home",
            "points": None,
            "sportsbook": "hardrockbet",
            "american_odds": -145,
            "check_time": (NOW - timedelta(minutes=5)).isoformat(),
            "event_commence_time": (NOW + timedelta(minutes=30)).isoformat(),
            "event_status": "scheduled",
            "odds_collected_at": (NOW - timedelta(minutes=5)).isoformat(),
            "odds_market_status": "open",
            "selection_settled": False,
            "edge_percentage_points": 5.0,
            "expected_value_percent": 7.4,
            "pick_strength_score": 81,
            "letter_grade": "B-",
        }
        message = build_multisport_summary(
            status,
            workflow_status="success",
            as_of=NOW,
        )
        self.assertNotIn("🏟️ Multi-Sport Test Result", message)
        self.assertIn("Sports Status:", message)
        self.assertIn("⚽ EPL — READY", message)
        self.assertIn("🏀 NBA — COLLECTING", message)
        self.assertIn("Best Current Selection:", message)
        self.assertIn("🚨 Bot Lean: ARSENAL ML", message)
        self.assertIn("Market: 3-Way Moneyline (ML)", message)
        self.assertIn("Team: Arsenal", message)
        self.assertNotIn("Pick: HOME", message)
        self.assertIn("Odds: -145", message)
        self.assertIn("Training / Validation:", message)
        self.assertIn("NBA: 12/50 training | 0/100 OOS", message)
        self.assertIn("Automatic wagering: OFF", message)
        self.assertNotIn('{', message)
        self.assertNotIn('"sports"', message)

    def test_multisport_summary_handles_no_selection(self):
        message = build_multisport_summary(self.sample_status(), workflow_status="success")
        self.assertIn("Best Current Selection:\nNone", message)
        self.assertIn("Test-only. No real-money action is taken.", message)

    def test_selected_sports_only_are_built(self):
        messages = build_messages({"sports": {"epl": {"model_status": "test_only"}}}, parse_sports("epl"))
        self.assertEqual(tuple(messages), ("epl",))

    def test_unknown_sport_fails_closed(self):
        with self.assertRaises(SystemExit):
            parse_sports("epl,nfl")


if __name__ == "__main__":
    unittest.main()
