import unittest
from datetime import datetime, timedelta, timezone

from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.multisport_operations import bootstrap, status_report
from sports_analytics_pipeline.multisport_research import (
    all_research_sports,
    live_readiness_report,
    record_research_check,
    required_categories,
    research_policy,
    research_sources,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 22, 16, 0, tzinfo=UTC)
PROVIDER_KEYS = {
    "nba": "basketball_nba",
    "wnba": "basketball_wnba",
    "epl": "soccer_epl",
    "mma": "mma_mixed_martial_arts",
    "atp": "tennis_atp_test",
    "wta": "tennis_wta_test",
}


class MultiSportLiveReadinessTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)
        bootstrap(self.db, as_of=NOW)

    def tearDown(self):
        self.db.close()

    def event(self, sport):
        provider = PROVIDER_KEYS[sport]
        home = f"{sport}:home"
        away = f"{sport}:away"
        event_id = f"{sport}:{provider}:event"
        participant_type = "individual" if sport in {"mma", "atp", "wta"} else "team"
        for participant_id, suffix in ((home, "home"), (away, "away")):
            self.db.execute(
                """
                INSERT INTO multisport_participants (
                    participant_id, sport, provider_sport_key, source_participant_id,
                    full_name, participant_type, first_seen_at, last_seen_at, active_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'provider_listed')
                """,
                (
                    participant_id, sport, provider, suffix, f"{sport.upper()} {suffix}",
                    participant_type, NOW.isoformat(), NOW.isoformat(),
                ),
            )
        self.db.execute(
            """
            INSERT INTO multisport_events (
                event_id, sport, provider_sport_key, source_event_id, commence_time,
                home_participant_id, away_participant_id, status,
                first_seen_at, last_seen_at, test_only
            ) VALUES (?, ?, ?, 'event', ?, ?, ?, 'scheduled', ?, ?, 1)
            """,
            (
                event_id, sport, provider, (NOW + timedelta(hours=1)).isoformat(),
                home, away, NOW.isoformat(), NOW.isoformat(),
            ),
        )
        self.db.commit()
        return event_id

    def verify_all(self, sport, event_id, *, collected_at=NOW):
        by_category = {}
        for source in research_sources(sport):
            by_category.setdefault(source.category, source)
        for category in required_categories(sport):
            source = by_category[category]
            record_research_check(
                self.db,
                event_id=event_id,
                sport=sport,
                category=category,
                source_key=source.source_key,
                verification_status="verified",
                official=source.official,
                source_url=source.url,
                source_updated_at=collected_at,
                collected_at=collected_at,
                evidence={
                    "event_id": event_id,
                    "category": category,
                    "material_change": False,
                },
            )

    def test_every_new_sport_has_a_complete_live_policy(self):
        self.assertEqual(set(all_research_sports()), {"nba", "wnba", "epl", "mma", "atp", "wta"})
        for sport in all_research_sports():
            policy = research_policy(sport)
            self.assertEqual(set(policy.freshness_minutes), set(required_categories(sport)))
        self.assertIn("weather_environment", required_categories("epl"))
        self.assertIn("weather_environment", required_categories("atp"))
        self.assertIn("weather_environment", required_categories("wta"))
        self.assertIn("weigh_in_status", required_categories("mma"))
        self.assertIn("rest_travel", required_categories("nba"))
        self.assertNotIn("weather_environment", required_categories("nba"))

    def test_fresh_complete_evidence_is_ready_for_all_sports(self):
        for sport in all_research_sports():
            event_id = self.event(sport)
            self.verify_all(sport, event_id)
            report = live_readiness_report(
                self.db, event_id=event_id, sport=sport, as_of=NOW + timedelta(minutes=30)
            )
            self.assertTrue(report.ready, (sport, report.blockers))
            self.assertEqual(report.blockers, ())

    def test_stale_lineup_and_odds_are_named_exactly(self):
        event_id = self.event("nba")
        self.verify_all("nba", event_id, collected_at=NOW - timedelta(minutes=100))
        report = live_readiness_report(self.db, event_id=event_id, sport="nba", as_of=NOW)
        self.assertFalse(report.ready)
        self.assertIn("lineup_or_participants:stale(100m>90m)", report.blockers)
        self.assertIn("current_odds:stale(100m>60m)", report.blockers)

    def test_material_change_after_lineup_requires_a_new_lineup(self):
        event_id = self.event("nba")
        self.verify_all("nba", event_id, collected_at=NOW - timedelta(minutes=20))
        injury = next(
            source for source in research_sources("nba")
            if source.category == "injury_availability"
        )
        record_research_check(
            self.db,
            event_id=event_id,
            sport="nba",
            category="injury_availability",
            source_key=injury.source_key,
            verification_status="verified",
            official=injury.official,
            source_url=injury.url,
            source_updated_at=NOW - timedelta(minutes=5),
            collected_at=NOW - timedelta(minutes=5),
            evidence={"event_id": event_id, "material_change": True, "player_status": "out"},
        )
        report = live_readiness_report(self.db, event_id=event_id, sport="nba", as_of=NOW)
        self.assertFalse(report.ready)
        self.assertIn(
            "lineup_or_participants:refresh_required_after_injury_availability",
            report.blockers,
        )

    def test_future_evidence_is_ignored(self):
        event_id = self.event("wnba")
        self.verify_all("wnba", event_id, collected_at=NOW + timedelta(minutes=1))
        report = live_readiness_report(self.db, event_id=event_id, sport="wnba", as_of=NOW)
        self.assertFalse(report.ready)
        self.assertIn("official_status:missing", report.blockers)

    def test_verified_live_evidence_cannot_be_empty(self):
        event_id = self.event("epl")
        self.verify_all("epl", event_id)
        weather = next(
            source for source in research_sources("epl")
            if source.category == "weather_environment"
        )
        record_research_check(
            self.db,
            event_id=event_id,
            sport="epl",
            category="weather_environment",
            source_key=weather.source_key,
            verification_status="verified",
            official=weather.official,
            source_updated_at=NOW + timedelta(minutes=1),
            collected_at=NOW + timedelta(minutes=1),
            evidence={},
        )
        report = live_readiness_report(
            self.db, event_id=event_id, sport="epl", as_of=NOW + timedelta(minutes=2)
        )
        self.assertFalse(report.ready)
        self.assertIn("weather_environment:missing_event_specific_evidence", report.blockers)

    def test_status_exposes_policy_and_protected_nfl_mlb_paths(self):
        report = status_report(self.db, as_of=NOW)
        self.assertEqual(report["protected_existing_research_paths"], ("nfl", "mlb"))
        self.assertTrue(report["sports"]["mma"]["live_research_policy"]["late_change_detection"])
        self.assertIn(
            "weigh_in_status",
            report["sports"]["mma"]["live_research_policy"]["required_categories"],
        )
        self.assertFalse(report["automatic_wagering"])

    def test_existing_research_table_migrates_without_losing_legacy_columns(self):
        db = connect(":memory:")
        try:
            initialize_schema(db)
            db.execute("DROP TABLE multisport_research_checks")
            db.execute(
                """
                CREATE TABLE multisport_research_checks (
                    research_check_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    sport TEXT NOT NULL,
                    category TEXT NOT NULL CHECK (
                        category IN (
                            'official_status','injury_availability','lineup_or_participants',
                            'statistics_analytics','transactions_or_changes','current_odds'
                        )
                    ),
                    source_key TEXT NOT NULL,
                    source_url TEXT,
                    official INTEGER NOT NULL,
                    verification_status TEXT NOT NULL,
                    source_updated_at TEXT,
                    collected_at TEXT NOT NULL,
                    notes TEXT
                )
                """
            )
            db.commit()
            bootstrap(db, as_of=NOW)
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(multisport_research_checks)")
            }
            sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='multisport_research_checks'"
            ).fetchone()[0]
            self.assertIn("evidence_json", columns)
            self.assertIn("evidence_hash", columns)
            self.assertIn("weather_environment", sql)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
