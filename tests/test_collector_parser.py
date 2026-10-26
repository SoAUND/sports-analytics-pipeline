from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.collectors.base import MappingTeamResolver
from sports_analytics_pipeline.collectors.the_odds_api import parse_current_spreads


class OddsApiParserTests(unittest.TestCase):
    def setUp(self):
        self.resolver = MappingTeamResolver(
            {
                "Buffalo Bills": "BUF",
                "Miami Dolphins": "MIA",
            }
        )
        self.payload = [
            {
                "id": "event-123",
                "sport_key": "americanfootball_nfl",
                "commence_time": "2026-09-17T00:15:00Z",
                "home_team": "Buffalo Bills",
                "away_team": "Miami Dolphins",
                "bookmakers": [
                    {
                        "key": "example_book",
                        "title": "Example Book",
                        "last_update": "2026-09-16T22:00:00Z",
                        "markets": [
                            {
                                "key": "spreads",
                                "last_update": "2026-09-16T22:01:00Z",
                                "outcomes": [
                                    {"name": "Buffalo Bills", "price": 1.91, "point": -3.0},
                                    {"name": "Miami Dolphins", "price": 1.91, "point": 3.0},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]

    def test_parser_preserves_raw_payload_and_normalizes_quotes(self):
        collected = datetime(2026, 9, 16, 22, 2, tzinfo=timezone.utc)
        batch = parse_current_spreads(self.payload, self.resolver, collected)
        self.assertIs(batch.raw_payload, self.payload)
        self.assertEqual(len(batch.quotes), 2)
        self.assertEqual({quote.outcome_team_id for quote in batch.quotes}, {"BUF", "MIA"})
        self.assertTrue(all(quote.period == "full_game" for quote in batch.quotes))

    def test_unknown_team_alias_fails(self):
        self.payload[0]["home_team"] = "Unknown Team"
        with self.assertRaises(ValueError):
            parse_current_spreads(self.payload, self.resolver)

    def test_mismatched_points_fail(self):
        self.payload[0]["bookmakers"][0]["markets"][0]["outcomes"][1]["point"] = 2.5
        with self.assertRaises(ValueError):
            parse_current_spreads(self.payload, self.resolver)


if __name__ == "__main__":
    unittest.main()
