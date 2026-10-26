from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sports_analytics_pipeline.data.db import initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.mlb_evaluation import evaluate_mlb_upcoming
from sports_analytics_pipeline.mlb_live_paper import (
    latest_live_paper_run_for_date,
    ready_game_ids_from_report,
    store_live_paper_run,
)
from sports_analytics_pipeline.mlb_modeling import create_upcoming_mlb_model_runs
from sports_analytics_pipeline.mlb_operations import build_daily_mlb_plan
from sports_analytics_pipeline.mlb_weather import collect_required_mlb_weather

NOW = datetime(2026, 8, 16, 22, 0, tzinfo=timezone.utc)


class FakeRepo:
    def __init__(self):
        self.runs = []
        self.finished = []
        self.persisted = []

    def begin_run(self, source_key, endpoint_name, started_at):
        run_id = f"run-{len(self.runs)+1}"
        self.runs.append((run_id, source_key, endpoint_name, started_at))
        return run_id

    def finish_run(self, run_id, **kwargs):
        self.finished.append((run_id, kwargs))

    def persist_weather(self, game_id, batch, roof_status):
        self.persisted.append((game_id, batch, roof_status))
        return 1


class FakeWeatherCollector:
    calls = []

    def __init__(self, *, transport, user_agent):
        if "replace-with-real-contact" in user_agent:
            raise ValueError("A real identifying NWS User-Agent is required")
        self.transport = transport
        self.user_agent = user_agent

    def collect_forecast(self, *, latitude, longitude, kickoff_time):
        self.__class__.calls.append((latitude, longitude, kickoff_time))
        return SimpleNamespace(record=SimpleNamespace())


class Step5LiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = sqlite3.connect(Path(self.tmp.name) / "test.sqlite3")
        self.db.row_factory = sqlite3.Row
        initialize_schema(self.db)
        seed_reference_data(self.db, include_mlb=True)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _insert_game(self, game_id, kickoff, stadium_id="rate_field"):
        teams = self.db.execute(
            "SELECT team_id FROM teams WHERE sport='mlb' ORDER BY team_id LIMIT 2"
        ).fetchall()
        self.db.execute(
            """
            INSERT INTO games(
                game_id, source_game_id, sport, season, season_type, week,
                home_team_id, away_team_id, kickoff_time, stadium_id,
                roof_status, neutral_site, status, created_at, updated_at
            ) VALUES (?, ?, 'mlb', 2026, 'regular', 0, ?, ?, ?, ?, 'unknown', 0,
                      'scheduled', ?, ?)
            """,
            (
                game_id,
                game_id,
                teams[0]["team_id"],
                teams[1]["team_id"],
                kickoff,
                stadium_id,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        self.db.commit()

    def test_operations_plan_preserves_names_and_requires_readiness(self):
        plan = build_daily_mlb_plan(date="2026-08-16", env_path=".env", hours=24)
        self.assertEqual(
            [step.name for step in plan],
            ["collect", "model", "evaluate_track", "settle", "report", "review"],
        )
        model = next(step for step in plan if step.name == "model")
        evaluate = next(step for step in plan if step.name == "evaluate_track")
        self.assertIn("--require-readiness", model.command)
        self.assertIn("--operation-date", model.command)
        self.assertIn("2026-08-16", model.command)
        self.assertIn("--require-readiness", evaluate.command)
        self.assertIn("--model-runs-already-created", evaluate.command)

    def test_ready_game_ids_only_returns_explicit_ready_games(self):
        report = {
            "games": [
                {"game_id": "ready-1", "status": "ready"},
                {"game_id": "blocked-1", "status": "blocked"},
                {"game_id": "ready-2", "status": "ready"},
            ]
        }
        self.assertEqual(ready_game_ids_from_report(report), ("ready-1", "ready-2"))

    def test_latest_readiness_is_scoped_to_operation_date(self):
        first = {
            "operation_date": "2026-08-15",
            "scheduled_regular_season_games": 0,
            "ready_games": 0,
            "blocked_games": 0,
            "paper_decisions": 0,
            "games": [],
        }
        second = {
            "operation_date": "2026-08-16",
            "scheduled_regular_season_games": 1,
            "ready_games": 0,
            "blocked_games": 1,
            "paper_decisions": 0,
            "games": [{"game_id": "g", "status": "blocked", "blockers": ["x"]}],
        }
        store_live_paper_run(self.db, report=first, recorded_at=NOW)
        store_live_paper_run(self.db, report=second, recorded_at=NOW)
        loaded = latest_live_paper_run_for_date(self.db, "2026-08-16")
        self.assertEqual(loaded["operation_date"], "2026-08-16")
        self.assertEqual(loaded["blocked_games"], 1)

    def test_empty_ready_allowlist_creates_no_model_runs(self):
        artifact = SimpleNamespace(model_version="m", market_audit_status="missing")
        with patch("sports_analytics_pipeline.mlb_modeling.load_mlb_artifact", return_value=artifact), patch(
            "sports_analytics_pipeline.mlb_modeling.create_mlb_model_run"
        ) as create:
            out = create_upcoming_mlb_model_runs(
                self.db,
                prediction_time=NOW,
                hours=24,
                eligible_game_ids=(),
            )
        self.assertEqual(out["games_found"], 0)
        self.assertEqual(out["model_runs"], [])
        create.assert_not_called()

    def test_model_allowlist_filters_blocked_game(self):
        self._insert_game("ready-game", "2026-08-17T01:00:00+00:00")
        # different teams required by uniqueness; reuse same teams but different kickoff is valid
        self._insert_game("blocked-game", "2026-08-17T02:00:00+00:00")
        artifact = SimpleNamespace(model_version="m", market_audit_status="missing")
        with patch("sports_analytics_pipeline.mlb_modeling.load_mlb_artifact", return_value=artifact), patch(
            "sports_analytics_pipeline.mlb_modeling.create_mlb_model_run", side_effect=lambda db, game_id, prediction_time, artifact: f"model-{game_id}"
        ) as create:
            out = create_upcoming_mlb_model_runs(
                self.db,
                prediction_time=NOW,
                hours=6,
                eligible_game_ids=("ready-game",),
            )
        self.assertEqual(out["games_found"], 1)
        self.assertEqual(out["model_runs"], ["model-ready-game"])
        self.assertEqual(create.call_args.kwargs["game_id"], "ready-game")

    def test_evaluator_empty_ready_allowlist_evaluates_nothing(self):
        self._insert_game("blocked-game", "2026-08-17T01:00:00+00:00")
        out = evaluate_mlb_upcoming(
            self.db,
            as_of=NOW,
            hours=6,
            game_ids=(),
            persist=False,
        )
        self.assertEqual(out["games_found"], 0)
        self.assertEqual(out["paper_records"], [])

    def test_non_exposed_mlb_venue_skips_weather_without_collector(self):
        # Chase Field is seeded as non-weather-exposed.
        self._insert_game("indoor", "2026-08-17T01:00:00+00:00", stadium_id="chase_field")
        repo = FakeRepo()
        FakeWeatherCollector.calls = []
        out = collect_required_mlb_weather(
            self.db,
            repo=repo,
            transport=object(),
            user_agent="placeholder is irrelevant for indoor",
            game_ids=("indoor",),
            now_fn=lambda: NOW,
            collector_factory=FakeWeatherCollector,
        )
        self.assertEqual(out[0]["status"], "skipped")
        self.assertEqual(FakeWeatherCollector.calls, [])
        self.assertEqual(repo.persisted, [])

    def test_exposed_us_venue_requires_real_nws_identity_and_fails_closed(self):
        self._insert_game("outdoor", "2026-08-17T01:00:00+00:00", stadium_id="rate_field")
        repo = FakeRepo()
        out = collect_required_mlb_weather(
            self.db,
            repo=repo,
            transport=object(),
            user_agent="SportsAnalyticsPipeline/0.1 (contact: replace-with-real-contact)",
            game_ids=("outdoor",),
            now_fn=lambda: NOW,
            collector_factory=FakeWeatherCollector,
        )
        self.assertEqual(out[0]["status"], "failed")
        self.assertIn("User-Agent", out[0]["reason"])
        self.assertEqual(repo.persisted, [])

    def test_exposed_us_venue_persists_forecast_when_collector_succeeds(self):
        self._insert_game("outdoor", "2026-08-17T01:00:00+00:00", stadium_id="rate_field")
        repo = FakeRepo()
        FakeWeatherCollector.calls = []
        out = collect_required_mlb_weather(
            self.db,
            repo=repo,
            transport=object(),
            user_agent="SportsAnalyticsPipeline/0.1 (contact: ops@example.com)",
            game_ids=("outdoor",),
            now_fn=lambda: NOW,
            collector_factory=FakeWeatherCollector,
        )
        self.assertEqual(out[0]["status"], "succeeded")
        self.assertEqual(len(repo.persisted), 1)
        self.assertEqual(repo.persisted[0][0], "outdoor")


if __name__ == "__main__":
    unittest.main()
