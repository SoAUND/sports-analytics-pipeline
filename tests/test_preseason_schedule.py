from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import os
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.collectors.base import CollectionBatch, MappingTeamResolver
from sports_analytics_pipeline.collectors.official_schedule import load_bundled_preseason_schedule
from sports_analytics_pipeline.collectors.storage import CollectorRepository
from sports_analytics_pipeline.collectors.the_odds_api import PRESEASON_SPORT_KEY, parse_current_spreads
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import alias_mapping, seed_reference_data
from scripts.run_collectors import diagnose_preseason_odds, replay_latest_odds


class PreseasonScheduleTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        initialize_schema(self.db)
        seed_reference_data(self.db)
        self.collected = datetime(2026, 8, 14, 22, 50, tzinfo=timezone.utc)
        self.batch = load_bundled_preseason_schedule(
            2026, self.db, collected_at=self.collected
        )
        self.repo = CollectorRepository(self.db)

    def tearDown(self):
        self.db.close()

    def test_schedule_has_hall_of_fame_game_and_all_three_full_weeks(self):
        self.assertEqual(len(self.batch.records), 49)
        self.assertEqual(
            Counter(game.week for game in self.batch.records),
            Counter({0: 1, 1: 16, 2: 16, 3: 16}),
        )
        for week in (1, 2, 3):
            teams = [
                team
                for game in self.batch.records
                if game.week == week
                for team in (game.home_team_id, game.away_team_id)
            ]
            self.assertEqual(len(teams), 32)
            self.assertEqual(len(set(teams)), 32)

        hall = next(game for game in self.batch.records if game.week == 0)
        self.assertEqual((hall.away_team_id, hall.home_team_id), ("CAR", "ARI"))
        self.assertTrue(hall.neutral_site)
        self.assertEqual(hall.stadium_id, "tom_benson_hall_of_fame_stadium")

    def test_official_schedule_persists_and_is_idempotent(self):
        self.assertEqual(self.repo.persist_official_schedules(self.batch), 49)
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM games WHERE season = 2026 AND season_type = 'preseason'"
            ).fetchone()[0],
            49,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM game_source_ids WHERE source_key = 'nfl_schedule'"
            ).fetchone()[0],
            49,
        )
        self.assertEqual(self.repo.persist_official_schedules(self.batch), 49)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM games").fetchone()[0], 49)

    def test_imported_schedule_matches_preserved_preseason_odds(self):
        self.repo.persist_official_schedules(self.batch)
        payload = [{
            "id": "preseason-event-1",
            "sport_key": "americanfootball_nfl_preseason",
            "commence_time": "2026-08-14T23:00:00Z",
            "home_team": "Atlanta Falcons",
            "away_team": "Denver Broncos",
            "bookmakers": [{
                "key": "draftkings",
                "title": "DraftKings",
                "last_update": "2026-08-14T22:49:00Z",
                "markets": [{
                    "key": "spreads",
                    "last_update": "2026-08-14T22:49:00Z",
                    "outcomes": [
                        {"name": "Atlanta Falcons", "price": 1.91, "point": -2.5},
                        {"name": "Denver Broncos", "price": 1.91, "point": 2.5}
                    ]
                }]
            }]
        }]
        parsed = parse_current_spreads(
            payload,
            MappingTeamResolver(alias_mapping(self.db)),
            self.collected,
            sport_key=PRESEASON_SPORT_KEY,
        )
        batch = CollectionBatch(
            provider_key=parsed.provider_key,
            collected_at=parsed.collected_at,
            raw_payload=parsed.raw_payload,
            quotes=parsed.quotes,
            metadata={"endpoint": "current_preseason_odds"},
        )
        stored, warnings = self.repo.persist_odds(
            batch,
            accessible_bookmakers=("draftkings",),
            consensus_bookmakers=("draftkings",),
        )
        self.assertEqual(stored, 2)
        self.assertEqual(warnings, ())
        game_ids = {
            row[0] for row in self.db.execute("SELECT DISTINCT game_id FROM odds_snapshots")
        }
        self.assertEqual(game_ids, {"2026_PRE_1_DEN_ATL"})

    def test_odds_match_same_teams_with_eleven_minute_kickoff_drift(self):
        self.repo.persist_official_schedules(self.batch)
        payload = [{
            "id": "preseason-event-delayed",
            "sport_key": "americanfootball_nfl_preseason",
            "commence_time": "2026-08-14T23:11:00Z",
            "home_team": "Washington Commanders",
            "away_team": "Miami Dolphins",
            "bookmakers": [{
                "key": "hardrockbet",
                "title": "Primary Sportsbook",
                "last_update": "2026-08-14T22:49:00Z",
                "markets": [{
                    "key": "spreads",
                    "outcomes": [
                        {"name": "Washington Commanders", "price": 1.91, "point": -1.5},
                        {"name": "Miami Dolphins", "price": 1.91, "point": 1.5}
                    ]
                }]
            }]
        }]
        parsed = parse_current_spreads(
            payload,
            MappingTeamResolver(alias_mapping(self.db)),
            self.collected,
            sport_key=PRESEASON_SPORT_KEY,
        )
        odds_batch = CollectionBatch(
            provider_key=parsed.provider_key,
            collected_at=parsed.collected_at,
            raw_payload=parsed.raw_payload,
            quotes=parsed.quotes,
            metadata={"endpoint": "current_preseason_odds"},
        )
        stored, warnings = self.repo.persist_odds(
            odds_batch,
            accessible_bookmakers=("hardrockbet",),
            consensus_bookmakers=("hardrockbet",),
        )
        self.assertEqual(stored, 2)
        self.assertEqual(warnings, ())
        game_id = self.db.execute(
            "SELECT DISTINCT game_id FROM odds_snapshots"
        ).fetchone()[0]
        self.assertEqual(game_id, "2026_PRE_1_MIA_WAS")

    def test_other_seasons_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "only for 2026"):
            load_bundled_preseason_schedule(2025, self.db)

    def test_latest_preserved_odds_replay_uses_zero_new_requests(self):
        payload = [{
            "id": "preseason-event-replay",
            "sport_key": "americanfootball_nfl_preseason",
            "commence_time": "2026-08-14T23:00:00Z",
            "home_team": "Atlanta Falcons",
            "away_team": "Denver Broncos",
            "bookmakers": [{
                "key": "draftkings",
                "title": "DraftKings",
                "last_update": "2026-08-14T22:49:00Z",
                "markets": [{
                    "key": "spreads",
                    "outcomes": [
                        {"name": "Atlanta Falcons", "price": 1.91, "point": -2.5},
                        {"name": "Denver Broncos", "price": 1.91, "point": 2.5}
                    ]
                }]
            }]
        }]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "bot.sqlite3"
            db = connect(database)
            initialize_schema(db)
            seed_reference_data(db)
            repo = CollectorRepository(db)
            schedule = load_bundled_preseason_schedule(
                2026, db, collected_at=self.collected
            )
            repo.persist_official_schedules(schedule)
            repo.store_raw(
                source_key="the_odds_api",
                endpoint_name="current_preseason_odds",
                payload=payload,
                collected_at=self.collected,
            )
            db.commit()
            db.close()

            with patch.dict(
                os.environ,
                {"DATABASE_PATH": str(database), "BOT_MODE": "test_only"},
                clear=False,
            ):
                result = replay_latest_odds(
                    SimpleNamespace(env="missing-test.env"), preseason=True
                )
            self.assertEqual(result["new_api_requests"], 0)
            self.assertEqual(result["stored"], 2)
            self.assertEqual(result["warnings"], [])

    def test_diagnostic_explains_unmatched_event_without_api_request(self):
        payload = [{
            "id": "preseason-event-mismatch",
            "sport_key": "americanfootball_nfl_preseason",
            "commence_time": "2026-08-15T00:00:00Z",
            "home_team": "Atlanta Falcons",
            "away_team": "Denver Broncos",
            "bookmakers": [],
        }]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "bot.sqlite3"
            db = connect(database)
            initialize_schema(db)
            seed_reference_data(db)
            repo = CollectorRepository(db)
            schedule = load_bundled_preseason_schedule(
                2026, db, collected_at=self.collected
            )
            repo.persist_official_schedules(schedule)
            repo.store_raw(
                source_key="the_odds_api",
                endpoint_name="current_preseason_odds",
                payload=payload,
                collected_at=self.collected,
            )
            db.commit()
            db.close()

            with patch.dict(
                os.environ,
                {"DATABASE_PATH": str(database), "BOT_MODE": "test_only"},
                clear=False,
            ):
                result = diagnose_preseason_odds(SimpleNamespace(env="missing-test.env"))
            self.assertEqual(result["new_api_requests"], 0)
            self.assertEqual(result["unmatched_count"], 1)
            event = result["unmatched_events"][0]
            self.assertEqual(event["event_id"], "preseason-event-mismatch")
            self.assertEqual(event["reason"], "kickoff_time_difference_over_10_minutes")
            self.assertEqual(event["closest_schedule_entry"]["game_id"], "2026_PRE_1_DEN_ATL")
            self.assertEqual(event["closest_schedule_entry"]["difference_minutes"], 60.0)


if __name__ == "__main__":
    unittest.main()
