import unittest
from datetime import datetime, timezone

from sports_analytics_pipeline.collectors.base import MappingTeamResolver
from sports_analytics_pipeline.collectors.mlb_official import MappingStadiumResolver, parse_mlb_schedule
from sports_analytics_pipeline.collectors.storage import CollectorRepository
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import alias_mapping, seed_reference_data, stadium_alias_mapping

NOW = datetime(2026, 8, 16, 20, 0, tzinfo=timezone.utc)


class MLBStructuredResearchStep4ATests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)
        seed_reference_data(self.db, include_mlb=True)
        self.teams = MappingTeamResolver(alias_mapping(self.db, "mlb"))
        self.stadiums = MappingStadiumResolver(stadium_alias_mapping(self.db, "mlb"))

    def tearDown(self):
        self.db.close()

    def payload(self, with_lineups=True):
        game = {
            "gamePk": 777001,
            "season": "2026",
            "gameDate": "2026-08-16T23:05:00Z",
            "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
            "venue": {"name": "Yankee Stadium"},
            "teams": {
                "home": {"team": {"name": "New York Yankees"}, "probablePitcher": {"id": 1001, "fullName": "Home Starter"}},
                "away": {"team": {"name": "Boston Red Sox"}, "probablePitcher": {"id": 1002, "fullName": "Away Starter"}},
            },
        }
        if with_lineups:
            game["lineups"] = {
                "homePlayers": [
                    {"id": 2000 + i, "fullName": f"Home Batter {i}", "primaryPosition": {"abbreviation": "OF"}}
                    for i in range(1, 10)
                ],
                "awayPlayers": [
                    {"id": 3000 + i, "fullName": f"Away Batter {i}", "primaryPosition": {"abbreviation": "IF"}}
                    for i in range(1, 10)
                ],
            }
        return {"dates": [{"date": "2026-08-16", "games": [game]}]}

    def parse(self, payload):
        return parse_mlb_schedule(
            payload,
            team_resolver=self.teams,
            stadium_resolver=self.stadiums,
            collected_at=NOW,
            expected_date="2026-08-16",
        )

    def test_structured_lineups_are_parsed_in_batting_order(self):
        game = self.parse(self.payload()).games[0]
        self.assertEqual(len(game.home_lineup), 9)
        self.assertEqual(len(game.away_lineup), 9)
        self.assertEqual(game.home_lineup[0].player_name, "Home Batter 1")
        self.assertEqual(game.away_lineup[-1].player_id, "mlb_person_3009")

    def test_missing_lineups_remain_empty_and_fail_closed(self):
        game = self.parse(self.payload(with_lineups=False)).games[0]
        self.assertEqual(game.home_lineup, ())
        self.assertEqual(game.away_lineup, ())

    def test_storage_confirms_complete_lineups_and_pitchers(self):
        CollectorRepository(self.db).persist_mlb_schedule(self.parse(self.payload()))
        row = self.db.execute("SELECT * FROM mlb_game_context WHERE game_id='mlb_777001'").fetchone()
        self.assertEqual(row["home_lineup_status"], "confirmed")
        self.assertEqual(row["away_lineup_status"], "confirmed")
        self.assertEqual(row["confirmed_home_pitcher_id"], "mlb_person_1001")
        self.assertEqual(row["confirmed_away_pitcher_id"], "mlb_person_1002")
        self.assertIn("mlb_person_2001", row["home_batting_order_json"])
        self.assertIn("mlb_person_3009", row["away_batting_order_json"])

    def test_storage_does_not_confirm_missing_lineups(self):
        CollectorRepository(self.db).persist_mlb_schedule(self.parse(self.payload(with_lineups=False)))
        row = self.db.execute("SELECT * FROM mlb_game_context WHERE game_id='mlb_777001'").fetchone()
        self.assertEqual(row["home_lineup_status"], "unconfirmed")
        self.assertEqual(row["away_lineup_status"], "unconfirmed")
        self.assertIsNone(row["confirmed_home_pitcher_id"])
        self.assertIsNone(row["confirmed_away_pitcher_id"])

    def test_lineup_players_are_stored_as_verified_active_mlb_players(self):
        CollectorRepository(self.db).persist_mlb_schedule(self.parse(self.payload()))
        count = self.db.execute("SELECT COUNT(*) FROM players WHERE source_key='mlb_lineups'").fetchone()[0]
        self.assertEqual(count, 18)


if __name__ == "__main__":
    unittest.main()
