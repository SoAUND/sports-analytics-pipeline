import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from sports_analytics_pipeline.quota_budget import DailyCreditBudget, poll_due, record_poll


UTC = timezone.utc


class DailyCreditBudgetTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")

    def tearDown(self):
        self.db.close()

    def test_remaining_credits_are_spread_across_rest_of_month(self):
        budget = DailyCreditBudget.from_metadata(
            self.db,
            provider_key="the_odds_api",
            metadata={"requests_remaining": 443, "requests_used": 57},
            as_of=datetime(2026, 9, 2, 12, tzinfo=UTC),
            reserve_credits=50,
            daily_credit_cap=10,
        )
        self.assertEqual(budget.daily_allowance, 10)
        self.assertTrue(budget.reserve(4))
        self.assertTrue(budget.reserve(6))
        self.assertFalse(budget.reserve(1))
        self.assertEqual(budget.available_today, 0)

    def test_low_balance_automatically_reduces_daily_allowance(self):
        budget = DailyCreditBudget.from_metadata(
            self.db,
            provider_key="the_odds_api",
            metadata={"requests_remaining": 79},
            as_of=datetime(2026, 9, 2, 12, tzinfo=UTC),
            reserve_credits=50,
            daily_credit_cap=10,
        )
        self.assertEqual(budget.daily_allowance, 1)

    def test_missing_headers_fail_closed(self):
        budget = DailyCreditBudget.from_metadata(
            self.db,
            provider_key="the_odds_api",
            metadata={},
            as_of=datetime(2026, 9, 2, 12, tzinfo=UTC),
            reserve_credits=50,
            daily_credit_cap=10,
        )
        self.assertFalse(budget.reserve(1))
        self.assertIn("unavailable", budget.disabled_reason)

    def test_polling_interval_survives_separate_cycles(self):
        now = datetime(2026, 9, 2, 12, tzinfo=UTC)
        self.assertTrue(
            poll_due(
                self.db,
                provider_key="the_odds_api",
                sport="atp",
                endpoint_name="scores",
                scope_key="tennis_atp_us_open",
                as_of=now,
                interval_minutes=720,
            )
        )
        record_poll(
            self.db,
            provider_key="the_odds_api",
            sport="atp",
            endpoint_name="scores",
            scope_key="tennis_atp_us_open",
            polled_at=now,
            request_cost=2,
            requests_remaining=441,
        )
        self.assertFalse(
            poll_due(
                self.db,
                provider_key="the_odds_api",
                sport="atp",
                endpoint_name="scores",
                scope_key="tennis_atp_us_open",
                as_of=now + timedelta(hours=11),
                interval_minutes=720,
            )
        )
        self.assertTrue(
            poll_due(
                self.db,
                provider_key="the_odds_api",
                sport="atp",
                endpoint_name="scores",
                scope_key="tennis_atp_us_open",
                as_of=now + timedelta(hours=12),
                interval_minutes=720,
            )
        )


if __name__ == "__main__":
    unittest.main()
