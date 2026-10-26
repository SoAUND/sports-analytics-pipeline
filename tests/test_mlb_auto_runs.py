from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from sports_analytics_pipeline.mlb_auto import (
    automatic_cooldown_active,
    build_automatic_gate,
    candidate_schedule_dates,
    find_games_in_pregame_window,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 17, 20, 0, tzinfo=UTC)


def game(game_pk: int, game_date: str, *, game_type: str = "R", state: str = "Preview", detail: str = "Scheduled"):
    return {
        "gamePk": game_pk,
        "gameType": game_type,
        "gameDate": game_date,
        "status": {"abstractGameState": state, "detailedState": detail},
        "teams": {
            "home": {"team": {"name": "Home Club"}},
            "away": {"team": {"name": "Away Club"}},
        },
    }


def payload(*games):
    return {"dates": [{"date": "2026-08-17", "games": list(games)}]}


class MLBAutomaticRunTests(unittest.TestCase):
    def test_due_regular_game_inside_25_to_65_minute_window_is_found(self):
        out = find_games_in_pregame_window(
            payload(game(1, "2026-08-17T20:50:00Z")),
            now=NOW,
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source_game_id"], "1")
        self.assertEqual(out[0]["minutes_to_start"], 50.0)

    def test_too_early_started_postseason_and_postponed_games_are_skipped(self):
        out = find_games_in_pregame_window(
            payload(
                game(1, "2026-08-17T21:30:00Z"),
                game(2, "2026-08-17T19:55:00Z", state="Live", detail="In Progress"),
                game(3, "2026-08-17T20:50:00Z", game_type="F"),
                game(4, "2026-08-17T20:50:00Z", detail="Postponed"),
            ),
            now=NOW,
        )
        self.assertEqual(out, ())

    def test_scheduled_gate_uses_two_hour_operations_window(self):
        out = build_automatic_gate(
            event_name="schedule",
            now=NOW,
            requested_date="2026-08-17",
            schedule_payload=payload(game(9, "2026-08-17T21:00:00Z")),
        )
        self.assertTrue(out["should_run"])
        self.assertTrue(out["automated"])
        self.assertEqual(out["run_mode"], "paper_run")
        self.assertEqual(out["hours"], 2)
        self.assertEqual(out["attempts"], 3)
        self.assertFalse(out["cooldown_active"])

    def test_recent_success_marker_throttles_duplicate_automatic_run(self):
        out = build_automatic_gate(
            event_name="schedule",
            now=NOW,
            requested_date="2026-08-17",
            schedule_payload=payload(game(9, "2026-08-17T21:00:00Z")),
            latest_auto_success_at=NOW - timedelta(minutes=10),
        )
        self.assertFalse(out["should_run"])
        self.assertTrue(out["cooldown_active"])
        self.assertEqual(len(out["due_games"]), 1)
        self.assertIn("cooldown", out["notice"])

    def test_expired_success_marker_allows_next_automatic_window(self):
        out = build_automatic_gate(
            event_name="schedule",
            now=NOW,
            requested_date="2026-08-17",
            schedule_payload=payload(game(9, "2026-08-17T21:00:00Z")),
            latest_auto_success_at=NOW - timedelta(minutes=21),
        )
        self.assertTrue(out["should_run"])
        self.assertFalse(out["cooldown_active"])
        self.assertFalse(
            automatic_cooldown_active(NOW - timedelta(minutes=21), now=NOW)
        )

    def test_future_marker_does_not_suppress_safe_paper_check(self):
        self.assertFalse(
            automatic_cooldown_active(NOW + timedelta(minutes=1), now=NOW)
        )

    def test_scheduled_gate_skips_when_no_game_is_due(self):
        out = build_automatic_gate(
            event_name="schedule",
            now=NOW,
            requested_date="2026-08-17",
            schedule_payload=payload(game(9, "2026-08-17T23:00:00Z")),
        )
        self.assertFalse(out["should_run"])
        self.assertEqual(out["due_games"], [])

    def test_manual_workflow_inputs_are_preserved(self):
        out = build_automatic_gate(
            event_name="workflow_dispatch",
            now=NOW,
            requested_date="2026-08-18",
            requested_mode="preflight",
            requested_hours="24",
            requested_attempts="3",
        )
        self.assertTrue(out["should_run"])
        self.assertFalse(out["automated"])
        self.assertEqual(out["operation_date"], "2026-08-18")
        self.assertEqual(out["hours"], 24)

    def test_previous_schedule_date_is_checked_only_just_after_eastern_midnight(self):
        just_after_midnight = datetime(2026, 8, 17, 4, 30, tzinfo=UTC)
        afternoon = datetime(2026, 8, 17, 20, 0, tzinfo=UTC)
        self.assertEqual(
            candidate_schedule_dates(now=just_after_midnight),
            ("2026-08-17", "2026-08-16"),
        )
        self.assertEqual(candidate_schedule_dates(now=afternoon), ("2026-08-17",))

    def test_workflow_has_redundant_schedule_gate_and_success_marker(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / ".github/workflows/mlb-paper.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "7-57/10 10-23 * 3-10 *"', source)
        self.assertIn('cron: "7-57/10 0-2 * 3-10 *"', source)
        self.assertGreaterEqual(source.count('timezone: "America/New_York"'), 2)
        self.assertIn("GITHUB_API_TOKEN: ${{ github.token }}", source)
        self.assertIn("Resolve automatic or manual run", source)
        self.assertIn("needs.gate.outputs.should_run == 'true'", source)
        self.assertIn("actions/artifacts?name=mlb-paper-database", source)
        self.assertIn('./bot mlb-ops "$OPERATION_DATE"', source)
        self.assertIn("name: mlb-auto-success", source)
        self.assertIn('if settings.bot_mode not in {"research", "test_only"}:', source)
        self.assertIn("BOT_MODE must remain research or test_only", source)


if __name__ == "__main__":
    unittest.main()

