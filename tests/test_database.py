from pathlib import Path
import sqlite3
import sys
import unittest
from zoneinfo import ZoneInfo


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.data.db import (
    collection_table_names,
    connect,
    initialize_schema,
    required_table_names,
)
from sports_analytics_pipeline.data.seed import (
    alias_mapping,
    seed_reference_data,
    stadium_alias_mapping,
)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        initialize_schema(self.db)
        seed_reference_data(self.db)

    def tearDown(self):
        self.db.close()

    def test_required_tables_exist(self):
        tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue(required_table_names().issubset(tables))
        self.assertTrue(collection_table_names().issubset(tables))

    def test_all_32_teams_are_seeded(self):
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM teams").fetchone()[0], 32)
        conferences = dict(
            self.db.execute(
                "SELECT conference, COUNT(*) FROM teams GROUP BY conference"
            ).fetchall()
        )
        self.assertEqual(conferences, {"AFC": 16, "NFC": 16})
        divisions = self.db.execute(
            "SELECT conference, division, COUNT(*) FROM teams GROUP BY conference, division"
        ).fetchall()
        self.assertTrue(all(row[2] == 4 for row in divisions))

    def test_aliases_resolve_without_ambiguous_shortcuts(self):
        aliases = alias_mapping(self.db)
        self.assertEqual(aliases["Washington Commanders"], "WAS")
        self.assertEqual(aliases["WSH"], "WAS")
        self.assertEqual(aliases["JAC"], "JAX")
        self.assertNotIn("LA", aliases)
        self.assertNotIn("NY", aliases)

    def test_stadium_time_zones_and_team_links_are_valid(self):
        rows = self.db.execute(
            """
            SELECT s.time_zone, s.valid_from_season, s.valid_through_season
            FROM teams t JOIN stadiums s ON s.stadium_id = t.primary_stadium_id
            """
        ).fetchall()
        self.assertEqual(len(rows), 32)
        for row in rows:
            ZoneInfo(row["time_zone"])
            self.assertLessEqual(row["valid_from_season"], 2026)
            self.assertTrue(
                row["valid_through_season"] is None
                or row["valid_through_season"] >= 2026
            )

    def test_buffalo_uses_new_2026_stadium(self):
        row = self.db.execute(
            """
            SELECT s.official_name, s.surface_type, s.valid_from_season
            FROM teams t JOIN stadiums s ON s.stadium_id = t.primary_stadium_id
            WHERE t.team_id = 'BUF'
            """
        ).fetchone()
        self.assertEqual(row["official_name"], "Highmark Stadium")
        self.assertEqual(row["surface_type"], "natural_grass")
        self.assertEqual(row["valid_from_season"], 2026)

    def test_2026_international_venues_are_available(self):
        rows = self.db.execute(
            "SELECT stadium_id FROM stadiums WHERE country_code <> 'US'"
        ).fetchall()
        self.assertEqual(len(rows), 8)

    def test_reviewed_nflverse_stadium_aliases_resolve(self):
        aliases = stadium_alias_mapping(self.db)
        self.assertEqual(aliases["Reliant Stadium"], "nrg_stadium")
        self.assertEqual(aliases["Maracana Stadium"], "maracana_stadium")
        self.assertEqual(aliases["Bernabeu"], "bernabeu_stadium")
        self.assertEqual(
            aliases["FC Bayern Munich Stadium"], "fc_bayern_munich_arena"
        )

    def test_future_odds_cannot_be_attached_to_earlier_prediction(self):
        self.db.execute(
            """
            INSERT INTO games (
                game_id, season, season_type, week, home_team_id, away_team_id,
                kickoff_time, stadium_id, created_at, updated_at
            ) VALUES (
                'game-1', 2026, 'regular', 1, 'BUF', 'MIA',
                '2026-09-10T20:00:00+00:00', 'highmark_stadium_2026',
                '2026-08-12T12:00:00+00:00', '2026-08-12T12:00:00+00:00'
            )
            """
        )
        cursor = self.db.execute(
            """
            INSERT INTO odds_snapshots (
                game_id, sportsbook_key, market_key, period, settlement_rule,
                outcome_team_id, opponent_team_id, spread_points, decimal_odds,
                source_updated_at, collected_at, market_status
            ) VALUES (
                'game-1', 'example_book', 'spreads', 'full_game',
                'includes_overtime_push_returns_stake', 'BUF', 'MIA', -3, 1.91,
                '2026-09-10T14:00:00+00:00', '2026-09-10T14:01:00+00:00', 'open'
            )
            """
        )
        odds_id = cursor.lastrowid
        self.db.execute(
            """
            INSERT INTO model_runs (
                model_run_id, game_id, model_version, feature_version, data_version,
                rule_version, training_cutoff, prediction_time,
                home_cover_probability, away_cover_probability, push_probability,
                uncertainty_margin, status, created_at
            ) VALUES (
                'model-1', 'game-1', 'test', 'test', 'test', 'test',
                '2026-09-01T00:00:00+00:00', '2026-09-10T13:00:00+00:00',
                0.5, 0.5, 0.0, 0.02, 'test_only', '2026-09-10T13:00:00+00:00'
            )
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                """
                INSERT INTO decisions (
                    decision_id, game_id, model_run_id, selected_odds_snapshot_id,
                    selected_team_id, gate_results_json, decision_type,
                    manual_review_status, created_at
                ) VALUES (
                    'decision-1', 'game-1', 'model-1', ?, 'BUF', '{}',
                    'test_only_selection', 'pending', '2026-09-10T13:00:00+00:00'
                )
                """,
                (odds_id,),
            )


if __name__ == "__main__":
    unittest.main()
