from datetime import datetime, timezone
import sqlite3
import unittest

from sports_analytics_pipeline.manual_pick_tracker import (
    add_price_snapshot,
    create_ticket,
    due_rechecks,
    ensure_manual_pick_tracker_schema,
    render_ticket_message,
    settle_leg,
    ticket_summary,
)

UTC = timezone.utc


class ManualPickTrackerTests(unittest.TestCase):
    def connection(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        ensure_manual_pick_tracker_schema(db)
        return db

    def test_entry_price_is_preserved_when_recheck_is_added(self):
        db = self.connection()
        ticket_id = create_ticket(
            db,
            ticket_id="ticket-1",
            source_kind="chatgpt_video",
            sportsbook_key="hardrockbet",
            created_at=datetime(2026, 9, 7, 22, 30, tzinfo=UTC),
            legs=[
                {
                    "sport": "mlb",
                    "event_label": "Cubs @ Brewers",
                    "market_key": "moneylines",
                    "selection": "Milwaukee Brewers ML",
                    "entry_american_odds": -210,
                    "commence_time": "2026-09-08T23:40:00+00:00",
                }
            ],
        )
        leg_id = f"{ticket_id}-leg-1"
        add_price_snapshot(
            db,
            leg_id=leg_id,
            snapshot_type="recheck",
            american_odds=-225,
            captured_at=datetime(2026, 9, 8, 23, 10, tzinfo=UTC),
            source_key="manual_book_refresh",
            sportsbook_key="hardrockbet",
        )
        summary = ticket_summary(db, ticket_id)
        self.assertEqual(summary["legs"][0]["entry_american_odds"], -210)
        self.assertEqual(summary["legs"][0]["latest_price"]["american_odds"], -225)
        self.assertEqual(summary["legs"][0]["latest_price"]["paid_credit_used"], 0)

    def test_paid_credit_flag_is_schema_locked_to_zero(self):
        db = self.connection()
        create_ticket(
            db,
            ticket_id="ticket-2",
            source_kind="chatgpt_video",
            legs=[
                {
                    "sport": "atp",
                    "event_label": "Player A vs Player B",
                    "market_key": "match_moneyline",
                    "selection": "Player A ML",
                    "entry_american_odds": -155,
                }
            ],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                """
                INSERT INTO manual_pick_price_snapshots (
                    leg_id, snapshot_type, american_odds, decimal_odds, captured_at,
                    source_key, provenance, paid_credit_used
                ) VALUES ('ticket-2-leg-1','manual',-160,1.625,'2026-09-08T00:00:00+00:00',
                          'forbidden_paid_source','test',1)
                """
            )

    def test_due_recheck_only_returns_near_start_pending_legs(self):
        db = self.connection()
        create_ticket(
            db,
            ticket_id="ticket-3",
            source_kind="chatgpt_video",
            legs=[
                {
                    "sport": "mlb",
                    "event_label": "A @ B",
                    "market_key": "moneylines",
                    "selection": "B ML",
                    "entry_american_odds": -140,
                    "commence_time": "2026-09-08T23:40:00+00:00",
                },
                {
                    "sport": "mlb",
                    "event_label": "C @ D",
                    "market_key": "moneylines",
                    "selection": "D ML",
                    "entry_american_odds": -135,
                    "commence_time": "2026-09-09T02:00:00+00:00",
                },
            ],
        )
        rows = due_rechecks(
            db,
            as_of=datetime(2026, 9, 8, 23, 15, tzinfo=UTC),
            window_minutes=30,
        )
        self.assertEqual([row["leg_id"] for row in rows], ["ticket-3-leg-1"])

    def test_parlay_settles_only_after_all_legs_are_terminal(self):
        db = self.connection()
        create_ticket(
            db,
            ticket_id="ticket-4",
            source_kind="chatgpt_video",
            legs=[
                {
                    "sport": "mlb",
                    "event_label": "A @ B",
                    "market_key": "moneylines",
                    "selection": "B ML",
                    "entry_american_odds": -140,
                },
                {
                    "sport": "atp",
                    "event_label": "C vs D",
                    "market_key": "match_moneyline",
                    "selection": "C ML",
                    "entry_american_odds": -155,
                },
            ],
        )
        settle_leg(
            db,
            leg_id="ticket-4-leg-1",
            result_status="win",
            final_score_or_result="B 5, A 2",
            result_source_key="official",
        )
        self.assertEqual(ticket_summary(db, "ticket-4")["status"], "pre_pick")
        settle_leg(
            db,
            leg_id="ticket-4-leg-2",
            result_status="loss",
            final_score_or_result="D def. C",
            result_source_key="official",
        )
        self.assertEqual(ticket_summary(db, "ticket-4")["status"], "settled")

    def test_render_message_labels_tracker_test_only(self):
        db = self.connection()
        create_ticket(
            db,
            ticket_id="ticket-5",
            source_kind="chatgpt_video",
            legs=[
                {
                    "sport": "mlb",
                    "event_label": "Cubs @ Brewers",
                    "market_key": "moneylines",
                    "selection": "Milwaukee Brewers ML",
                    "entry_american_odds": -210,
                }
            ],
        )
        message = render_ticket_message(db, "ticket-5")
        self.assertIn("Pre-odds: -210", message)
        self.assertIn("Test-only tracker", message)
        self.assertIn("Zero paid sports-data credits", message)


if __name__ == "__main__":
    unittest.main()
