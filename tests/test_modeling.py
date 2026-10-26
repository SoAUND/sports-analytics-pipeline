from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.collectors.base import MappingTeamResolver
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.history import parse_historical_games
from sports_analytics_pipeline.modeling import (
    ModelArtifact,
    build_current_model_runs,
    train_first_model,
)


UTC = timezone.utc


def artifact(**changes):
    values = {
        "model_version": "model-test",
        "feature_version": "features-test",
        "rule_version": "rules-test",
        "trained_at": datetime(2026, 8, 14, 22, tzinfo=UTC),
        "training_cutoff": datetime(2025, 12, 31, tzinfo=UTC),
        "intercept": 2.0,
        "elo_coefficient": 12.0,
        "residual_standard_deviation": 13.5,
        "calibration_intercept": 0.0,
        "calibration_slope": 1.0,
        "validation_ece": 0.02,
        "validation_brier": 0.24,
        "validation_log_loss": 0.68,
        "locked_test_ece": 0.03,
        "locked_test_brier": 0.24,
        "locked_test_log_loss": 0.68,
        "baseline_brier": 0.25,
        "baseline_log_loss": 0.693147,
        "market_baseline_brier": 0.245,
        "market_baseline_log_loss": 0.685,
        "market_baseline_coverage": 1.0,
        "training_count": 800,
        "validation_count": 200,
        "locked_test_count": 200,
        "push_rates": {"3": 0.04, "all_whole": 0.03},
        "data_hash": "data-test",
        "status": "acceptable",
    }
    values.update(changes)
    return ModelArtifact(**values)


class HistoricalTrainingTests(unittest.TestCase):
    def test_parser_reverses_provider_favorite_sign_for_home_spread(self):
        text = (
            "game_id,season,game_type,week,gameday,gametime,home_team,away_team,"
            "home_score,away_score,spread_line\n"
            "g1,2025,REG,1,2025-09-07,13:00:00,BUF,MIA,24,20,3.5\n"
        )
        records, warnings = parse_historical_games(
            text,
            resolver=MappingTeamResolver({"BUF": "BUF", "MIA": "MIA"}),
            seasons=(2025,),
        )
        self.assertEqual(warnings, ())
        self.assertEqual(records[0].home_spread_points, -3.5)

    def test_parser_preserves_closing_prices_and_builds_no_vig_baseline(self):
        text = (
            "game_id,season,game_type,week,gameday,gametime,home_team,away_team,"
            "home_score,away_score,spread_line,home_spread_odds,away_spread_odds,"
            "home_rest,away_rest,home_qb_id,away_qb_id,home_coach,away_coach\n"
            "g1,2025,REG,1,2025-09-07,13:00:00,BUF,MIA,24,20,3.5,"
            "-120,+100,7,8,qb-home,qb-away,Home Coach,Away Coach\n"
        )
        records, warnings = parse_historical_games(
            text,
            resolver=MappingTeamResolver({"BUF": "BUF", "MIA": "MIA"}),
            seasons=(2025,),
        )
        self.assertEqual(warnings, ())
        self.assertAlmostEqual(records[0].home_spread_decimal_odds, 1.833333, places=5)
        self.assertAlmostEqual(records[0].away_spread_decimal_odds, 2.0)
        self.assertAlmostEqual(records[0].home_market_no_vig_probability, 0.521739, places=5)
        self.assertEqual(records[0].home_rest_days, 7)
        self.assertEqual(records[0].away_qb_id, "qb-away")

    def test_training_reports_exact_missing_periods(self):
        db = connect()
        initialize_schema(db)
        seed_reference_data(db)
        model, output = train_first_model(
            db,
            as_of=datetime(2026, 8, 14, tzinfo=UTC),
            persist=False,
        )
        self.assertIsNone(model)
        self.assertEqual(output["status"], "blocked")
        self.assertEqual(output["next_command"], "./bot history")
        self.assertIn("locked_test_decisions", output["missing_requirements"])
        db.close()


class CurrentModelRunTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        initialize_schema(self.db)
        seed_reference_data(self.db)
        self.as_of = datetime(2026, 8, 14, 22, tzinfo=UTC)
        kickoff = self.as_of + timedelta(hours=2)
        self.db.execute(
            """
            INSERT INTO games (
                game_id, season, season_type, week, home_team_id, away_team_id,
                kickoff_time, stadium_id, status, created_at, updated_at
            ) VALUES ('game-1', 2026, 'preseason', 1, 'BUF', 'MIA', ?,
                      'highmark_stadium_2026', 'scheduled', ?, ?)
            """,
            (kickoff.isoformat(), self.as_of.isoformat(), self.as_of.isoformat()),
        )
        market_time = self.as_of - timedelta(minutes=5)
        for book in ("draftkings", "hardrockbet_fl"):
            for team, opponent, spread in (
                ("BUF", "MIA", -2.5),
                ("MIA", "BUF", 2.5),
            ):
                self.db.execute(
                    """
                    INSERT INTO odds_snapshots (
                        game_id, sportsbook_key, market_key, period, settlement_rule,
                        outcome_team_id, opponent_team_id, spread_points, decimal_odds,
                        source_updated_at, collected_at, market_status,
                        is_accessible, is_consensus_eligible
                    ) VALUES ('game-1', ?, 'spreads', 'full_game', 'standard',
                              ?, ?, ?, 1.91, ?, ?, 'open', 1, 1)
                    """,
                    (
                        book,
                        team,
                        opponent,
                        spread,
                        market_time.isoformat(),
                        market_time.isoformat(),
                    ),
                )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_acceptable_artifact_creates_audited_test_only_run(self):
        output = build_current_model_runs(
            self.db,
            artifact=artifact(),
            as_of=self.as_of,
            persist=True,
        )
        self.assertEqual(output["predictions_stored"], 1)
        row = self.db.execute("SELECT * FROM model_runs").fetchone()
        self.assertEqual(row["status"], "test_only")
        self.assertEqual(row["market_spread_points"], -2.5)
        self.assertAlmostEqual(
            row["home_cover_probability"]
            + row["away_cover_probability"]
            + row["push_probability"],
            1.0,
        )
        self.assertGreaterEqual(row["uncertainty_margin"], 0.05)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], 1
        )

    def test_future_odds_are_never_used(self):
        self.db.execute("DELETE FROM odds_snapshots")
        future = self.as_of + timedelta(minutes=1)
        for team, opponent, spread in (
            ("BUF", "MIA", -2.5),
            ("MIA", "BUF", 2.5),
        ):
            self.db.execute(
                """
                INSERT INTO odds_snapshots (
                    game_id, sportsbook_key, market_key, period, settlement_rule,
                    outcome_team_id, opponent_team_id, spread_points, decimal_odds,
                    source_updated_at, collected_at, market_status,
                    is_accessible, is_consensus_eligible
                ) VALUES ('game-1', 'draftkings', 'spreads', 'full_game', 'standard',
                          ?, ?, ?, 1.91, ?, ?, 'open', 1, 1)
                """,
                (team, opponent, spread, future.isoformat(), future.isoformat()),
            )
        output = build_current_model_runs(
            self.db,
            artifact=artifact(),
            as_of=self.as_of,
            persist=False,
        )
        self.assertEqual(output["predictions_previewed"], 0)
        self.assertIn("No fresh", output["passes"][0]["reason"])

    def test_rejected_artifact_cannot_create_predictions(self):
        output = build_current_model_runs(
            self.db,
            artifact=artifact(status="rejected"),
            as_of=self.as_of,
        )
        self.assertEqual(output["status"], "blocked")
        self.assertEqual(output["predictions_stored"], 0)


if __name__ == "__main__":
    unittest.main()
