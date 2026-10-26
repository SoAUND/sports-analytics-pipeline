from datetime import datetime, timezone
import sqlite3
import unittest

from sports_analytics_pipeline.multisport_market_baseline import (
    BASELINE_FEATURE_VERSION,
    baseline_model_version,
    ensure_market_baseline_model,
    is_market_baseline_model,
)
from sports_analytics_pipeline.multisport_runtime import ensure_runtime_schema

UTC = timezone.utc

class MarketBaselineTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        ensure_runtime_schema(self.db)

    def tearDown(self):
        self.db.close()

    def test_untrained_supported_sport_gets_test_only_nontraining_artifact(self):
        row = ensure_market_baseline_model(
            self.db, sport="wnba", as_of=datetime(2026, 8, 29, 18, 0, tzinfo=UTC)
        )
        self.assertEqual(row["algorithm"], "binary_logistic")
        self.assertEqual(row["status"], "test_only")
        self.assertEqual(row["training_count"], 0)
        self.assertEqual(row["holdout_count"], 0)
        self.assertEqual(row["feature_version"], BASELINE_FEATURE_VERSION)
        self.assertTrue(is_market_baseline_model(self.db, row["model_version"]))

    def test_baseline_identity_is_stable_and_rejects_epl(self):
        self.assertEqual(baseline_model_version("mma"), "mma-market-baseline-1.0")
        with self.assertRaises(ValueError):
            baseline_model_version("epl")

if __name__ == "__main__":
    unittest.main()
