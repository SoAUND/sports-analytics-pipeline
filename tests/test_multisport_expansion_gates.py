import json
import unittest
from datetime import datetime, timezone

from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.multisport_expansion import expansion_readiness, require_market_expansion_ready
from sports_analytics_pipeline.multisport_operations import bootstrap
from sports_analytics_pipeline.multisport_runtime import set_manual_model_approval


NOW = datetime(2026, 8, 17, 23, 0, tzinfo=timezone.utc)


class MultiSportExpansionGateTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)
        bootstrap(self.db, as_of=NOW)
        self.db.execute(
            """
            INSERT INTO multisport_model_artifacts (
                model_version, sport, market_key, period, feature_version, algorithm,
                feature_names_json, parameters_json, trained_at, training_cutoff,
                training_count, holdout_count, holdout_log_loss, holdout_brier,
                calibration_error, base_uncertainty, data_hash, status, notes_json
            ) VALUES (
                'nba-approved-test', 'nba', 'spreads', 'full_game', 'v1', 'binary_logistic',
                '[]', '{}', ?, ?, 100, 20, 0.6, 0.2, 0.03, 0.04,
                'hash', 'test_only', '[]'
            )
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_expansion_is_locked_before_validation(self):
        readiness = expansion_readiness(
            self.db, sport="nba", model_version="nba-approved-test"
        )
        self.assertFalse(readiness.may_add_second_market)
        self.assertFalse(readiness.may_enable_parlay_ranking)
        self.assertIn("validation:0/100", readiness.blockers)
        with self.assertRaises(RuntimeError):
            require_market_expansion_ready(
                self.db, sport="nba", model_version="nba-approved-test"
            )

    def test_even_100_decisions_do_not_bypass_manual_review(self):
        self.db.execute(
            """
            UPDATE multisport_validation_metrics
            SET completed_count=100, calibration_status='acceptable', clv_status='favorable',
                review_status='ready_for_review', updated_at=?
            WHERE validation_bucket='nba:spreads:full_game'
            """,
            (NOW.isoformat(),),
        )
        self.db.commit()
        readiness = expansion_readiness(
            self.db, sport="nba", model_version="nba-approved-test"
        )
        self.assertFalse(readiness.may_add_second_market)
        self.assertIn("review:ready_for_review", readiness.blockers)


if __name__ == "__main__":
    unittest.main()
