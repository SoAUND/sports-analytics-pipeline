from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.collectors.base import MappingTeamResolver
from sports_analytics_pipeline.collectors.nflverse import MappingStadiumResolver, parse_schedules_csv
from sports_analytics_pipeline.collectors.storage import CollectorRepository
from sports_analytics_pipeline.collectors.the_odds_api import parse_current_spreads
from sports_analytics_pipeline.collectors.therundown import parse_spreads as parse_rundown_spreads
from sports_analytics_pipeline.collectors.official_nfl import OfficialPageCheck
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import alias_mapping, seed_reference_data, stadium_alias_mapping


class Phase2StorageTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        initialize_schema(self.db)
        seed_reference_data(self.db)
        self.repo = CollectorRepository(self.db)
        self.teams = MappingTeamResolver(alias_mapping(self.db))
        self.stadiums = MappingStadiumResolver(stadium_alias_mapping(self.db))
        self.collected = datetime(2026, 9, 13, 15, 1, tzinfo=timezone.utc)
        csv_text = (
            "game_id,season,game_type,week,gameday,gametime,away_team,away_score,"
            "home_team,home_score,location,stadium,roof,surface\n"
            "2026_01_MIA_BUF,2026,REG,1,2026-09-13,13:00,MIA,,BUF,,Home,"
            "Highmark Stadium,outdoors,grass\n"
        )
        self.schedule = parse_schedules_csv(
            csv_text,
            season=2026,
            team_resolver=self.teams,
            stadium_resolver=self.stadiums,
            collected_at=self.collected,
        )

    def tearDown(self):
        self.db.close()

    def _odds(self):
        payload = [{
            "id": "event-1",
            "sport_key": "americanfootball_nfl",
            "commence_time": "2026-09-13T17:00:00Z",
            "home_team": "Buffalo Bills",
            "away_team": "Miami Dolphins",
            "bookmakers": [{
                "key": "book-a", "title": "Book A", "last_update": "2026-09-13T15:00:00Z",
                "markets": [{"key": "spreads", "last_update": "2026-09-13T15:00:00Z", "outcomes": [
                    {"name": "Buffalo Bills", "price": 1.91, "point": -3},
                    {"name": "Miami Dolphins", "price": 1.91, "point": 3},
                ]}],
            }],
        }]
        batch = parse_current_spreads(payload, self.teams, self.collected)
        return batch.__class__(
            provider_key=batch.provider_key,
            collected_at=batch.collected_at,
            raw_payload=batch.raw_payload,
            quotes=batch.quotes,
            warnings=batch.warnings,
            metadata={"endpoint": "current_odds"},
        )

    def test_schedule_then_odds_are_matched_and_deduplicated(self):
        self.assertEqual(self.repo.persist_schedules(self.schedule), 1)
        stored, warnings = self.repo.persist_odds(
            self._odds(),
            accessible_bookmakers=("book-a",),
            consensus_bookmakers=("book-a",),
        )
        self.assertEqual(stored, 2)
        self.assertEqual(warnings, ())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0], 2)
        stored_again, _ = self.repo.persist_odds(self._odds())
        self.assertEqual(stored_again, 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM raw_ingests").fetchone()[0], 2)

        # Reopening a collector safely refreshes reference rows without deleting
        # teams, stadiums, or sources that collected records already reference.
        seed_reference_data(self.db)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0], 2)

    def test_odds_without_schedule_are_not_silently_attached(self):
        stored, warnings = self.repo.persist_odds(self._odds())
        self.assertEqual(stored, 0)
        self.assertIn("matched 0 games", warnings[0])

    def test_therundown_uses_its_own_source_mapping_and_shared_book_identity(self):
        self.repo.persist_schedules(self.schedule)
        payload = {
            "events": [{
                "event_id": "rundown-event-1",
                "sport_id": 2,
                "event_date": "2026-09-13T17:00:00Z",
                "score": {"event_status": "STATUS_SCHEDULED"},
                "teams": [
                    {"team_id": 1, "name": "Miami Dolphins", "is_away": True, "is_home": False},
                    {"team_id": 2, "name": "Buffalo Bills", "is_away": False, "is_home": True},
                ],
                "markets": [{
                    "id": 10,
                    "market_id": 2,
                    "period_id": 0,
                    "participants": [
                        {"id": 1, "type": "TYPE_TEAM", "name": "Miami Dolphins", "lines": [{
                            "id": "away", "value": "+3", "prices": {"19": {
                                "id": "pa", "price": -110, "is_main_line": True,
                                "updated_at": "2026-09-13T15:00:00Z", "closed_at": ""
                            }}
                        }]},
                        {"id": 2, "type": "TYPE_TEAM", "name": "Buffalo Bills", "lines": [{
                            "id": "home", "value": "-3", "prices": {"19": {
                                "id": "ph", "price": -110, "is_main_line": True,
                                "updated_at": "2026-09-13T15:00:00Z", "closed_at": ""
                            }}
                        }]},
                    ],
                }],
            }]
        }
        batch = parse_rundown_spreads(
            payload, self.teams, expected_sport_id=2, collected_at=self.collected
        )
        batch = batch.__class__(
            provider_key=batch.provider_key,
            collected_at=batch.collected_at,
            raw_payload=batch.raw_payload,
            quotes=batch.quotes,
            warnings=batch.warnings,
            metadata={"endpoint": "current_odds"},
        )
        stored, warnings = self.repo.persist_odds(
            batch,
            accessible_bookmakers=("draftkings",),
            consensus_bookmakers=("draftkings",),
        )
        self.assertEqual(stored, 2)
        self.assertEqual(warnings, ())
        mapping = self.db.execute(
            "SELECT source_key FROM game_source_ids WHERE source_game_id = ?",
            ("rundown-event-1",),
        ).fetchone()
        self.assertEqual(mapping["source_key"], "therundown")
        source = self.db.execute(
            """
            SELECT r.source_key FROM odds_snapshots o
            JOIN raw_ingests r ON r.ingest_id = o.ingest_id
            WHERE o.sportsbook_key = 'draftkings' LIMIT 1
            """
        ).fetchone()
        self.assertEqual(source["source_key"], "therundown")

    def test_collection_run_records_failed_attempt(self):
        run_id = self.repo.begin_run("the_odds_api", "current_odds", self.collected)
        finished = datetime(2026, 9, 13, 15, 2, tzinfo=timezone.utc)
        self.repo.finish_run(
            run_id,
            finished_at=finished,
            status="failed",
            error_message="API key is not configured",
        )
        row = self.db.execute(
            "SELECT status, error_message FROM collection_runs WHERE collection_run_id = ?",
            (run_id,),
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_message"], "API key is not configured")

    def test_official_page_access_does_not_claim_player_confirmation(self):
        self.repo.persist_schedules(self.schedule)
        check = OfficialPageCheck(
            check_type="injuries",
            source_url="https://www.nfl.com/injuries/",
            checked_at=self.collected,
            http_status=200,
            content_sha256="a" * 64,
            access_status="confirmed",
        )
        stored = self.repo.persist_official_check(
            check, game_ids=("2026_01_MIA_BUF",)
        )
        self.assertEqual(stored, 1)
        row = self.db.execute(
            "SELECT missing_information, reviewer_notes FROM source_checks"
        ).fetchone()
        self.assertIn("player-level facts", row["missing_information"])
        self.assertIn("does not count", row["reviewer_notes"])


if __name__ == "__main__":
    unittest.main()
