from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.calculations import CalculationError
from sports_analytics_pipeline.market_matching import (
    SpreadQuote,
    match_two_sided_spread,
    quote_is_fresh,
    record_existed_by,
)


class MarketMatchingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 10, 16, 0, tzinfo=timezone.utc)
        common = dict(
            event_id="evt-1",
            bookmaker_key="example_book",
            market_key="spreads",
            period="full_game",
            settlement_rule="includes_overtime_push_returns_stake",
            decimal_odds=Decimal("1.91"),
            source_updated_at=self.now - timedelta(minutes=10),
            collected_at=self.now - timedelta(minutes=9),
        )
        self.home = SpreadQuote(
            team_id="BUF",
            opponent_team_id="MIA",
            points=Decimal("-3"),
            **common,
        )
        self.away = SpreadQuote(
            team_id="MIA",
            opponent_team_id="BUF",
            points=Decimal("3"),
            **common,
        )

    def test_exact_two_sided_market_matches(self):
        matched = match_two_sided_spread(self.home, self.away)
        self.assertEqual(matched, (self.home, self.away))

    def test_different_point_does_not_match(self):
        wrong = SpreadQuote(
            **{**self.away.__dict__, "points": Decimal("2.5")}
        )
        with self.assertRaises(CalculationError):
            match_two_sided_spread(self.home, wrong)

    def test_different_book_does_not_match(self):
        wrong = SpreadQuote(
            **{**self.away.__dict__, "bookmaker_key": "another_book"}
        )
        with self.assertRaises(CalculationError):
            match_two_sided_spread(self.home, wrong)

    def test_quote_freshness_and_future_data(self):
        self.assertTrue(quote_is_fresh(self.home, self.now))
        self.assertFalse(
            quote_is_fresh(self.home, self.now + timedelta(hours=2))
        )
        self.assertTrue(record_existed_by(self.home.collected_at, self.now))
        self.assertFalse(record_existed_by(self.now, self.now - timedelta(seconds=1)))


if __name__ == "__main__":
    unittest.main()
