from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.paper import paper_report, record_paper_decision, settle_paper_decisions


UTC = timezone.utc


class PaperTrackingTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        initialize_schema(self.db)
        seed_reference_data(self.db)
        self.decision_time = datetime(2026, 9, 10, 16, tzinfo=UTC)
        self.kickoff = self.decision_time + timedelta(hours=4)
        self.db.execute(
            """
            INSERT INTO games (
                game_id, season, season_type, week, home_team_id, away_team_id,
                kickoff_time, stadium_id, status, created_at, updated_at
            ) VALUES ('game-1', 2026, 'regular', 1, 'BUF', 'MIA', ?,
                      'highmark_stadium_2026', 'scheduled', ?, ?)
            """,
            (self.kickoff.isoformat(), self.decision_time.isoformat(), self.decision_time.isoformat()),
        )
        self.entry_id = self._odds("BUF", "MIA", -3, 1.91, self.decision_time)
        self._odds("MIA", "BUF", 3, 1.91, self.decision_time)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _odds(self, team, opponent, spread, price, when):
        cursor = self.db.execute(
            """
            INSERT INTO odds_snapshots (
                game_id, sportsbook_key, market_key, period, settlement_rule,
                outcome_team_id, opponent_team_id, spread_points, decimal_odds,
                source_updated_at, collected_at, market_status,
                is_accessible, is_consensus_eligible
            ) VALUES ('game-1', 'draftkings', 'spreads', 'full_game', 'standard',
                      ?, ?, ?, ?, ?, ?, 'open', 1, 1)
            """,
            (team, opponent, spread, price, when.isoformat(), when.isoformat()),
        )
        return cursor.lastrowid

    def _game(self):
        return self.db.execute("SELECT * FROM games WHERE game_id = 'game-1'").fetchone()

    def _selection(self):
        return {
            "decision": "test_only_selection",
            "team_id": "BUF",
            "spread_points": "-3",
            "sportsbook": "draftkings",
            "odds_snapshot_id": self.entry_id,
            "decimal_odds": "1.91",
            "checked_at": self.decision_time.isoformat(),
            "model_probability": "0.58",
            "conservative_probability": "0.56",
            "push_probability": "0.03",
            "break_even_probability": "0.52356",
            "probability_edge": "0.03644",
            "ev_per_dollar": "0.04",
            "minimum_acceptable_decimal_odds": "1.90",
            "pick_strength_score": 80,
            "confidence": "medium_test_only",
            "model_version": "test-model",
            "feature_version": "test-features",
            "data_version": "test-data",
            "model_rule_version": "test-rule",
            "gates": {"research": True},
        }

    def test_first_decision_is_immutable_and_regular_counts(self):
        first = record_paper_decision(
            self.db, game=self._game(), outcome=self._selection(), decision_time=self.decision_time
        )
        second = record_paper_decision(
            self.db,
            game=self._game(),
            outcome={"decision": "pass", "pass_reason": "Later information"},
            decision_time=self.decision_time + timedelta(minutes=10),
        )
        self.assertEqual(first["status"], "recorded")
        self.assertEqual(second["status"], "already_recorded")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM paper_decisions").fetchone()[0], 1)
        self.assertEqual(paper_report(self.db)["recorded_regular_season_decisions"], 1)

    def test_preseason_is_excluded_from_target(self):
        self.db.execute("UPDATE games SET season_type = 'preseason' WHERE game_id = 'game-1'")
        record_paper_decision(
            self.db, game=self._game(), outcome={"decision": "pass", "pass_reason": "test"},
            decision_time=self.decision_time,
        )
        report = paper_report(self.db)
        self.assertEqual(report["recorded_regular_season_decisions"], 0)
        self.assertEqual(report["remaining"], 100)

    def test_settlement_records_result_profit_clv_and_report(self):
        record_paper_decision(
            self.db, game=self._game(), outcome=self._selection(), decision_time=self.decision_time
        )
        close_time = self.kickoff - timedelta(minutes=5)
        self._odds("BUF", "MIA", -4, 1.91, close_time)
        self._odds("MIA", "BUF", 4, 1.91, close_time)
        self.db.execute(
            "UPDATE games SET status = 'final', home_score = 24, away_score = 20 WHERE game_id = 'game-1'"
        )
        output = settle_paper_decisions(self.db, as_of=self.kickoff + timedelta(hours=4))
        self.assertEqual(output["newly_settled"], 1)
        result = self.db.execute("SELECT * FROM paper_results").fetchone()
        self.assertEqual(result["cover_result"], "win")
        self.assertAlmostEqual(result["paper_profit_loss"], 0.91)
        self.assertAlmostEqual(result["line_clv_points"], 1.0)
        self.assertEqual(result["close_comparison"], "better")
        report = paper_report(self.db)
        self.assertEqual(report["record"]["wins"], 1)
        self.assertAlmostEqual(report["paper_profit_units"], 0.91)
        self.assertEqual(report["paper_unit_dollars"], 5.0)
        self.assertEqual(report["paper_profit_dollars"], 4.55)
        self.assertEqual(report["maximum_drawdown_dollars"], 0.0)
        self.assertEqual(report["calibration"]["scored_decisions"], 1)


if __name__ == "__main__":
    unittest.main()
