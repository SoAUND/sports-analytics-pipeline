import unittest
from datetime import datetime, timezone

from sports_analytics_pipeline.collectors.multisport_odds import (
    MultiSportCollectionBatch,
    MultiSportEvent,
    MultiSportParticipant,
)
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.multisport_storage import store_events, store_participants


NOW = datetime(2026, 8, 22, 18, 0, tzinfo=timezone.utc)


class MultiSportEventVenueStorageTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)

    def tearDown(self):
        self.db.close()

    def test_reviewed_venue_identity_survives_event_storage(self):
        batch = MultiSportCollectionBatch(
            provider_key="the_odds_api",
            sport="epl",
            provider_sport_key="soccer_epl",
            collected_at=NOW,
            raw_payload=[],
            participants=(
                MultiSportParticipant("epl", "soccer_epl", "home", "Home", "team"),
                MultiSportParticipant("epl", "soccer_epl", "away", "Away", "team"),
            ),
            events=(
                MultiSportEvent(
                    sport="epl",
                    provider_sport_key="soccer_epl",
                    source_event_id="event-1",
                    commence_time=NOW,
                    home_participant_id="home",
                    away_participant_id="away",
                    home_name="Home",
                    away_name="Away",
                    venue_id="emirates_stadium",
                    venue_name="Emirates Stadium",
                    source_venue_id="provider-venue-1",
                ),
            ),
        )
        store_participants(self.db, batch)
        store_events(self.db, batch)
        row = self.db.execute("SELECT * FROM multisport_events").fetchone()
        self.assertEqual(row["venue_id"], "emirates_stadium")
        self.assertEqual(row["venue_name"], "Emirates Stadium")
        self.assertEqual(row["source_venue_id"], "provider-venue-1")

    def test_existing_database_receives_additive_venue_columns(self):
        db = connect(":memory:")
        db.executescript(
            """
            CREATE TABLE multisport_events (
                event_id TEXT PRIMARY KEY,
                sport TEXT,
                provider_sport_key TEXT,
                source_event_id TEXT,
                commence_time TEXT,
                home_participant_id TEXT,
                away_participant_id TEXT,
                status TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT,
                test_only INTEGER
            );
            """
        )
        initialize_schema(db)
        columns = {row[1] for row in db.execute("PRAGMA table_info(multisport_events)")}
        self.assertTrue({"venue_id", "venue_name", "source_venue_id"}.issubset(columns))
        db.close()


if __name__ == "__main__":
    unittest.main()
