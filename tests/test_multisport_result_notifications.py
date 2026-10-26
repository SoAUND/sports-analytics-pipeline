import unittest
from datetime import datetime, timedelta, timezone

from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.multisport_operations import bootstrap
from sports_analytics_pipeline.multisport_results import (
    mark_result_notified,
    pending_result_notifications,
    record_result,
    settle_decision,
)
from scripts.build_multisport_telegram_messages import (
    build_messages,
    build_multisport_summary,
    build_settled_result_message,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 2, 20, 0, tzinfo=UTC)


class AutomaticResultNotificationTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)
        bootstrap(self.db, as_of=NOW)
        for participant_id, source_id, name in (
            ("nba:home", "home", "New York"),
            ("nba:away", "away", "Boston"),
        ):
            self.db.execute(
                """
                INSERT INTO multisport_participants (
                    participant_id, sport, provider_sport_key, source_participant_id,
                    full_name, participant_type, first_seen_at, last_seen_at, active_status
                ) VALUES (?, 'nba', 'basketball_nba', ?, ?, 'team', ?, ?, 'provider_listed')
                """,
                (participant_id, source_id, name, NOW.isoformat(), NOW.isoformat()),
            )
        self.db.execute(
            """
            INSERT INTO multisport_events (
                event_id, sport, provider_sport_key, source_event_id, commence_time,
                home_participant_id, away_participant_id, status,
                first_seen_at, last_seen_at, test_only
            ) VALUES ('nba:event', 'nba', 'basketball_nba', 'event',
                      ?, 'nba:home', 'nba:away', 'scheduled', ?, ?, 1)
            """,
            ((NOW - timedelta(hours=4)).isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
        cursor = self.db.execute(
            """
            INSERT INTO multisport_odds_snapshots (
                event_id, sport, provider_sport_key, sportsbook_key, sportsbook_title,
                market_key, provider_market_key, period, settlement_rule,
                outcome_participant_id, outcome_name, outcome_role, points,
                decimal_odds, source_updated_at, collected_at, market_status, test_only
            ) VALUES ('nba:event', 'nba', 'basketball_nba', 'hardrockbet', 'Primary Sportsbook',
                      'spreads', 'spreads', 'full_game',
                      'basketball_full_game_spread_overtime_push_refund',
                      'nba:home', 'New York', 'home', -3, 1.91, ?, ?, 'open', 1)
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
        self.db.execute(
            """
            INSERT INTO multisport_paper_decisions (
                paper_decision_id, event_id, sport, market_key, period,
                outcome_role, outcome_participant_id, sportsbook_key, odds_snapshot_id,
                points, decimal_odds, check_time, decision_type, gate_results_json,
                reasons_json, loss_reasons_json, manual_approval_status, countable_oos,
                rule_version, created_at
            ) VALUES ('decision-1', 'nba:event', 'nba', 'spreads', 'full_game',
                      'home', 'nba:home', 'hardrockbet', ?, -3, 1.91, ?,
                      'test_only_selection', '{}', '[]', '[]',
                      'pending', 0, 'test-v1', ?)
            """,
            (int(cursor.lastrowid), NOW.isoformat(), NOW.isoformat()),
        )
        self.db.commit()
        record_result(
            self.db,
            event_id="nba:event",
            result_status="final",
            source_key="the_odds_api",
            source_updated_at=NOW,
            collected_at=NOW,
            home_score=110,
            away_score=100,
            winner_role="home",
        )
        self.assertEqual(
            settle_decision(self.db, paper_decision_id="decision-1", settled_at=NOW),
            "win",
        )

    def tearDown(self):
        self.db.close()

    def test_new_result_is_clear_and_delivered_only_once(self):
        pending = pending_result_notifications(self.db)
        self.assertEqual(len(pending), 1)
        result = pending[0]
        self.assertEqual(result["selection_name"], "New York")
        self.assertEqual(result["record"], {"win": 1, "loss": 0, "push": 0, "void": 0})

        message = build_settled_result_message(result)
        self.assertIn("✅ WIN 1/1", message)
        self.assertIn("BOT PICK: NEW YORK -3", message)
        self.assertIn("Market: Full-Game Spread", message)
        self.assertIn("Final: New York 110 — Boston 100", message)
        self.assertIn("Paper P/L: +0.91 units (+$9.10)", message)
        self.assertIn("Running Test Record: 1-0-0 | Voids: 0", message)
        self.assertIn("Counts Toward Training/OOS: NO", message)
        self.assertIn("TEST ONLY — No real-money action was taken.", message)

        self.assertTrue(
            mark_result_notified(
                self.db,
                paper_decision_id="decision-1",
                channel="telegram",
                notified_at=NOW,
                source_workflow_run_id="12345",
            )
        )
        self.assertFalse(
            mark_result_notified(
                self.db,
                paper_decision_id="decision-1",
                channel="telegram",
                notified_at=NOW,
                source_workflow_run_id="12345",
            )
        )
        self.assertEqual(pending_result_notifications(self.db), ())


class CurrentSelectionTelegramTests(unittest.TestCase):
    def selection(self, *, event_time=NOW + timedelta(minutes=30), settled=False):
        return {
            "decision_type": "test_only_selection",
            "sport": "nba",
            "event_id": "nba:event-current",
            "market_key": "spreads",
            "selection_name": "New York",
            "outcome_role": "home",
            "points": -3,
            "sportsbook": "hardrockbet",
            "american_odds": -110,
            "check_time": (NOW - timedelta(minutes=5)).isoformat(),
            "event_commence_time": event_time.isoformat(),
            "event_status": "scheduled",
            "odds_collected_at": (NOW - timedelta(minutes=5)).isoformat(),
            "odds_market_status": "open",
            "selection_settled": settled,
        }

    @staticmethod
    def status(selection):
        return {
            "mode": "test_only",
            "sports": {
                "nba": {
                    "model_status": "test_only",
                    "best_current_paper_selection_display": selection,
                }
            },
            "best_current_paper_selection_display": selection,
        }

    @staticmethod
    def lookup(_event_id):
        return {
            "commence_time": (NOW + timedelta(minutes=30)).isoformat(),
            "event_status": "scheduled",
            "home_name": "New York",
            "away_name": "Boston",
        }

    def test_current_upcoming_selection_is_sent(self):
        status = self.status(self.selection())
        message = build_messages(
            status,
            ("nba",),
            event_lookup=self.lookup,
            as_of=NOW,
        )["nba"]
        self.assertIn("Status: READY", message)
        self.assertIn("BOT LEAN: NEW YORK -3", message)

    def test_previous_day_and_settled_selections_are_not_sent_as_leans(self):
        for selection in (
            self.selection(event_time=NOW - timedelta(days=1)),
            self.selection(settled=True),
        ):
            with self.subTest(selection=selection):
                status = self.status(selection)
                message = build_messages(
                    status,
                    ("nba",),
                    event_lookup=self.lookup,
                    as_of=NOW,
                )["nba"]
                summary = build_multisport_summary(
                    status,
                    event_lookup=self.lookup,
                    as_of=NOW,
                )
                self.assertIn("Status: NO NEW SELECTION", message)
                self.assertIn("Previous selections are settled, expired", message)
                self.assertNotIn("BOT LEAN:", message)
                self.assertIn("Best Current Selection:\nNone", summary)


if __name__ == "__main__":
    unittest.main()

