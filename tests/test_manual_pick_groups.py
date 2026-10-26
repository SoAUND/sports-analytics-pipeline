from datetime import datetime, timezone
import sqlite3
import unittest

from sports_analytics_pipeline.manual_pick_groups import (
    get_pick_group,
    render_grouped_ticket_message,
    set_pick_group,
)
from sports_analytics_pipeline.manual_pick_tracker import (
    add_price_snapshot,
    create_ticket,
    ensure_manual_pick_tracker_schema,
    settle_leg,
)

UTC = timezone.utc


class ManualPickGroupTests(unittest.TestCase):
    def connection(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        ensure_manual_pick_tracker_schema(db)
        return db

    def _ticket(self, db, ticket_id="system-ticket"):
        create_ticket(
            db,
            ticket_id=ticket_id,
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
                {
                    "sport": "mlb",
                    "event_label": "E @ F",
                    "market_key": "moneylines",
                    "selection": "F ML",
                    "entry_american_odds": -210,
                },
            ],
        )
        return ticket_id

    def test_system_and_recommended_are_permanent_separate_groups(self):
        db = self.connection()
        system_id = self._ticket(db, "system-ticket")
        recommended_id = self._ticket(db, "recommended-ticket")
        set_pick_group(db, system_id, "system")
        set_pick_group(db, recommended_id, "recommended")
        self.assertEqual(get_pick_group(db, system_id), "system")
        self.assertEqual(get_pick_group(db, recommended_id), "recommended")
        self.assertIn("🎯 — OUR SYSTEM PICKS", render_grouped_ticket_message(db, system_id))
        self.assertIn("⭐ — OUR RECOMMENDED PICKS", render_grouped_ticket_message(db, recommended_id))

    def test_message_keeps_entry_recheck_close_and_result_together(self):
        db = self.connection()
        ticket_id = self._ticket(db)
        set_pick_group(db, ticket_id, "system")
        add_price_snapshot(
            db,
            leg_id="system-ticket-leg-1",
            snapshot_type="recheck",
            american_odds=-150,
            captured_at=datetime(2026, 9, 8, 23, 10, tzinfo=UTC),
            source_key="manual_refresh",
        )
        add_price_snapshot(
            db,
            leg_id="system-ticket-leg-1",
            snapshot_type="close",
            american_odds=-160,
            captured_at=datetime(2026, 9, 8, 23, 39, tzinfo=UTC),
            source_key="manual_close",
        )
        message = render_grouped_ticket_message(db, ticket_id)
        self.assertIn("Entry Odds: -140", message)
        self.assertIn("Near-Start Odds: -150", message)
        self.assertIn("Final Odds: -160", message)
        self.assertIn("Result: PENDING", message)
        self.assertIn("Paid Sports-Data API Credits Used: 0", message)

    def test_final_message_preserves_group_and_record(self):
        db = self.connection()
        ticket_id = self._ticket(db, "recommended-ticket")
        set_pick_group(db, ticket_id, "recommended")
        settle_leg(db, leg_id="recommended-ticket-leg-1", result_status="win", final_score_or_result="B won", result_source_key="official")
        settle_leg(db, leg_id="recommended-ticket-leg-2", result_status="loss", final_score_or_result="D won", result_source_key="official")
        settle_leg(db, leg_id="recommended-ticket-leg-3", result_status="win", final_score_or_result="F won", result_source_key="official")
        message = render_grouped_ticket_message(db, ticket_id)
        self.assertIn("🏁 FINAL — OUR RECOMMENDED PICKS", message)
        self.assertIn("Group Record: 2-1", message)
        self.assertEqual(message.count("✅ WIN"), 2)
        self.assertEqual(message.count("❌ LOSS"), 1)

    def test_group_is_required_and_invalid_group_fails(self):
        db = self.connection()
        ticket_id = self._ticket(db)
        with self.assertRaises(ValueError):
            render_grouped_ticket_message(db, ticket_id)
        with self.assertRaises(ValueError):
            set_pick_group(db, ticket_id, "mixed")


if __name__ == "__main__":
    unittest.main()
