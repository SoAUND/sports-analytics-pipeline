from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sports_analytics_pipeline.data.db import initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.mlb_modeling import create_upcoming_mlb_model_runs
from sports_analytics_pipeline.mlb_persistence import (
    build_database_manifest,
    validate_mlb_paper_database,
    verify_database_manifest,
    write_database_manifest,
)
from sports_analytics_pipeline.mlb_reliability import resolve_operation_date


class Step6ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "paper.sqlite3"
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        initialize_schema(self.db)
        seed_reference_data(self.db, include_mlb=True)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_default_operation_date_uses_eastern_calendar_day(self):
        now = datetime(2026, 8, 17, 3, 30, tzinfo=timezone.utc)
        self.assertEqual(resolve_operation_date(None, now=now), "2026-08-16")

    def test_explicit_operation_date_is_preserved(self):
        self.assertEqual(resolve_operation_date("2026-08-20"), "2026-08-20")

    def test_invalid_operation_date_fails_closed(self):
        with self.assertRaises(ValueError):
            resolve_operation_date("08/20/2026")

    def test_database_validation_requires_complete_shared_and_mlb_schema(self):
        health = validate_mlb_paper_database(self.db_path)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["nfl_teams"], 32)
        self.assertEqual(health["mlb_teams"], 30)

    def test_database_validation_can_require_model_artifact(self):
        with self.assertRaisesRegex(ValueError, "No stored MLB model artifact"):
            validate_mlb_paper_database(self.db_path, require_model_artifact=True)

    def test_manifest_round_trip_detects_unchanged_database(self):
        manifest_path = Path(self.tmp.name) / "manifest.json"
        write_database_manifest(
            self.db_path,
            manifest_path,
            created_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
            source_run_id="123",
        )
        result = verify_database_manifest(self.db_path, manifest_path)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["sha256"]), 64)

    def test_manifest_detects_database_tampering(self):
        manifest_path = Path(self.tmp.name) / "manifest.json"
        write_database_manifest(self.db_path, manifest_path)
        self.db.close()
        with self.db_path.open("ab") as handle:
            handle.write(b"tamper")
        with self.assertRaisesRegex(ValueError, "size does not match"):
            verify_database_manifest(self.db_path, manifest_path)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row

    def test_manifest_contains_safe_recovery_metadata(self):
        manifest = build_database_manifest(
            self.db_path,
            created_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
            source_run_id="456",
        )
        self.assertEqual(manifest["format"], "our-mlb-paper-db-manifest-v1")
        self.assertEqual(manifest["source_run_id"], "456")
        self.assertNotIn("api_key", json.dumps(manifest).lower())

    def test_single_game_model_filter_is_supported(self):
        teams = self.db.execute(
            "SELECT team_id FROM teams WHERE sport='mlb' ORDER BY team_id LIMIT 2"
        ).fetchall()
        for index, game_id in enumerate(("one", "two"), 1):
            kickoff = f"2026-08-17T0{index}:00:00+00:00"
            self.db.execute(
                """
                INSERT INTO games(
                    game_id, source_game_id, sport, season, season_type, week,
                    home_team_id, away_team_id, kickoff_time, stadium_id,
                    roof_status, neutral_site, status, created_at, updated_at
                ) VALUES (?, ?, 'mlb', 2026, 'regular', 0, ?, ?, ?, 'rate_field',
                          'unknown', 0, 'scheduled', ?, ?)
                """,
                (
                    game_id,
                    game_id,
                    teams[0]["team_id"],
                    teams[1]["team_id"],
                    kickoff,
                    "2026-08-16T22:00:00+00:00",
                    "2026-08-16T22:00:00+00:00",
                ),
            )
        self.db.commit()
        artifact = type("Artifact", (), {"model_version": "m", "market_audit_status": "missing"})()
        with patch("sports_analytics_pipeline.mlb_modeling.load_mlb_artifact", return_value=artifact), patch(
            "sports_analytics_pipeline.mlb_modeling.create_mlb_model_run",
            side_effect=lambda db, game_id, prediction_time, artifact: f"model-{game_id}",
        ):
            out = create_upcoming_mlb_model_runs(
                self.db,
                prediction_time=datetime(2026, 8, 16, 23, 0, tzinfo=timezone.utc),
                hours=6,
                game_id="two",
            )
        self.assertEqual(out["games_found"], 1)
        self.assertEqual(out["model_runs"], ["model-two"])

    def test_reliable_scripts_use_current_settings_loader(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "scripts/run_mlb_reliable.py",
            "scripts/run_mlb_reliability_status.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            self.assertIn("Settings.load", source)
            self.assertNotIn("Settings.from_env_file", source)

    def test_reliable_script_propagates_failed_status(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts/run_mlb_reliable.py").read_text(encoding="utf-8")
        self.assertIn('return 0 if output["status"] in {"succeeded", "skipped_duplicate"} else 1', source)

    def test_workflow_is_manual_and_fail_closed_on_missing_backup(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / ".github/workflows/mlb-paper.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", source)
        self.assertNotIn("\n  push:\n", source)
        self.assertIn("Refusing to create a new database", source)
        self.assertIn("cancel-in-progress: false", source)

    def test_workflow_requires_nws_identity_and_preserves_failed_state(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / ".github/workflows/mlb-paper.yml").read_text(encoding="utf-8")
        self.assertIn("NWS_USER_AGENT: ${{ secrets.NWS_USER_AGENT }}", source)
        self.assertIn("Preserve failed state for diagnosis", source)
        self.assertIn("mlb-paper-manifest.json", source)


if __name__ == "__main__":
    unittest.main()
