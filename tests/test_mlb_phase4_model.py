from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest

from sports_analytics_pipeline.collectors.base import MappingTeamResolver
from sports_analytics_pipeline.collectors.http import HttpResponse
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import alias_mapping, seed_reference_data
from sports_analytics_pipeline.mlb_history import parse_historical_mlb_schedule, collect_historical_mlb_season
from sports_analytics_pipeline.mlb_modeling import (
    DEVELOPMENT_SEASONS,
    LOCKED_TEST_SEASON,
    VALIDATION_SEASON,
    create_mlb_model_run,
    historical_examples,
    load_mlb_artifact,
    train_mlb_model,
)

UTC = timezone.utc


class FakeTransport:
    def __init__(self, payload, received_at):
        self.payload = payload
        self.received_at = received_at
        self.urls = []

    def get(self, url, *, params=None, headers=None):
        self.urls.append(url)
        return HttpResponse(
            status=200,
            body=json.dumps(self.payload).encode("utf-8"),
            headers={"content-type": "application/json"},
            received_at=self.received_at,
        )


def _final_game(game_pk: int, season: int, when: datetime, home: str, away: str, hs: int, as_: int):
    return {
        "gamePk": game_pk,
        "season": str(season),
        "gameType": "R",
        "gameDate": when.isoformat().replace("+00:00", "Z"),
        "gameNumber": 1,
        "status": {"abstractGameState": "Final", "detailedState": "Final"},
        "teams": {
            "home": {"team": {"name": home}, "score": hs},
            "away": {"team": {"name": away}, "score": as_},
        },
    }


class MLBPhase4ModelTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)
        seed_reference_data(self.db, include_mlb=True)
        self.resolver = MappingTeamResolver(alias_mapping(self.db, "mlb"))
        self.teams = [
            (row["team_id"], row["official_name"], row["primary_stadium_id"])
            for row in self.db.execute(
                "SELECT team_id, official_name, primary_stadium_id FROM teams WHERE sport='mlb' ORDER BY team_id"
            ).fetchall()
        ]

    def tearDown(self):
        self.db.close()

    def test_historical_parser_keeps_only_final_regular_games(self):
        now = datetime(2025, 6, 1, 23, tzinfo=UTC)
        home = self.teams[0][1]
        away = self.teams[1][1]
        payload = {"dates": [{"date": "2025-06-01", "games": [
            _final_game(1, 2025, now, home, away, 5, 3),
            {**_final_game(2, 2025, now + timedelta(hours=1), home, away, 2, 1), "gameType": "S"},
            {**_final_game(3, 2025, now + timedelta(hours=2), home, away, 2, 1), "status": {"abstractGameState": "Live"}},
        ]}]}
        games, warnings = parse_historical_mlb_schedule(payload, resolver=self.resolver, seasons=(2025,))
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0].source_game_id, "1")
        self.assertEqual(warnings, ())

    def test_historical_parser_fails_closed_on_unknown_team(self):
        now = datetime(2025, 6, 1, 23, tzinfo=UTC)
        payload = {"dates": [{"date": "2025-06-01", "games": [
            _final_game(9, 2025, now, "Imaginary Club", self.teams[1][1], 5, 3)
        ]}]}
        games, warnings = parse_historical_mlb_schedule(payload, resolver=self.resolver)
        self.assertEqual(games, ())
        self.assertEqual(len(warnings), 1)
        self.assertIn("Unknown team alias", warnings[0])

    def test_historical_collector_is_idempotent_and_does_not_invent_prices(self):
        when = datetime(2025, 6, 1, 23, tzinfo=UTC)
        payload = {"dates": [{"date": "2025-06-01", "games": [
            _final_game(50, 2025, when, self.teams[0][1], self.teams[1][1], 5, 3)
        ]}]}
        transport = FakeTransport(payload, when + timedelta(hours=3))
        first = collect_historical_mlb_season(self.db, transport=transport, resolver=self.resolver, season=2025)
        second = collect_historical_mlb_season(self.db, transport=transport, resolver=self.resolver, season=2025)
        self.assertEqual(first["stored_total"], 1)
        self.assertEqual(second["stored_total"], 1)
        row = self.db.execute("SELECT * FROM historical_mlb_games WHERE source_game_id='50'").fetchone()
        self.assertIsNone(row["home_moneyline_decimal_odds"])
        self.assertIsNone(row["home_market_no_vig_probability"])

    def _seed_synthetic_history(self):
        start = datetime(2020, 4, 1, 17, tzinfo=UTC)
        game_id = 1000
        seasons = list(DEVELOPMENT_SEASONS) + [VALIDATION_SEASON, LOCKED_TEST_SEASON, 2026]
        for season_index, season in enumerate(seasons):
            for i in range(36):
                home_idx = (i + season_index) % len(self.teams)
                away_idx = (i * 7 + season_index + 1) % len(self.teams)
                if away_idx == home_idx:
                    away_idx = (away_idx + 1) % len(self.teams)
                home_id = self.teams[home_idx][0]
                away_id = self.teams[away_idx][0]
                # A stable but non-perfect home edge with enough variation for fitting.
                home_wins = ((i + season_index) % 5) != 0
                hs, aws = ((5, 3) if home_wins else (2, 4))
                kickoff = start.replace(year=season) + timedelta(days=i * 3)
                self.db.execute(
                    """
                    INSERT INTO historical_mlb_games (
                        source_game_id, season, kickoff_time, home_team_id, away_team_id,
                        home_score, away_score, game_number, source_key, collected_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'mlb_schedule', ?)
                    """,
                    (str(game_id), season, kickoff.isoformat(), home_id, away_id, hs, aws, kickoff.isoformat()),
                )
                game_id += 1
        self.db.commit()

    def test_time_ordered_elo_ignores_games_at_or_after_cutoff(self):
        self._seed_synthetic_history()
        cutoff = datetime(2025, 5, 1, tzinfo=UTC)
        examples, ratings = historical_examples(self.db, cutoff=cutoff)
        self.assertTrue(examples)
        self.assertTrue(all(item.kickoff_time < cutoff for item in examples))
        before = dict(ratings)
        # Add a future blowout; it must not alter ratings at the old cutoff.
        h, a = self.teams[0][0], self.teams[1][0]
        self.db.execute(
            """INSERT INTO historical_mlb_games
               (source_game_id,season,kickoff_time,home_team_id,away_team_id,home_score,away_score,game_number,source_key,collected_at)
               VALUES ('future-x',2026,?,?,?,?,?,1,'mlb_schedule',?)""",
            ((cutoff + timedelta(days=100)).isoformat(), h, a, 20, 0, (cutoff + timedelta(days=101)).isoformat()),
        )
        self.db.commit()
        _examples2, after = historical_examples(self.db, cutoff=cutoff)
        self.assertEqual(before, after)

    def test_training_uses_locked_splits_and_records_missing_market_audit(self):
        self._seed_synthetic_history()
        artifact = train_mlb_model(
            self.db,
            trained_at=datetime(2026, 1, 15, tzinfo=UTC),
            minimum_development_games=20,
            minimum_validation_games=20,
            minimum_locked_test_games=20,
        )
        self.assertEqual(artifact.development_count, 36 * 4)
        self.assertEqual(artifact.validation_count, 36)
        self.assertEqual(artifact.locked_test_count, 36)
        self.assertEqual(artifact.market_audit_status, "missing_historical_prediction_time_prices")
        self.assertEqual(artifact.status, "test_only")
        self.assertLess(artifact.training_cutoff.year, 2026)

    def test_training_rejects_insufficient_history(self):
        self._seed_synthetic_history()
        with self.assertRaisesRegex(ValueError, "Insufficient MLB development history"):
            train_mlb_model(self.db, trained_at=datetime(2026, 1, 15, tzinfo=UTC))

    def test_stored_artifact_round_trips(self):
        self._seed_synthetic_history()
        trained = train_mlb_model(
            self.db,
            trained_at=datetime(2026, 1, 15, tzinfo=UTC),
            minimum_development_games=20,
            minimum_validation_games=20,
            minimum_locked_test_games=20,
        )
        loaded = load_mlb_artifact(self.db)
        self.assertEqual(loaded.data_hash, trained.data_hash)
        self.assertAlmostEqual(loaded.probability(25), trained.probability(25), places=12)

    def test_model_run_is_mlb_moneyline_test_only_and_two_sided(self):
        self._seed_synthetic_history()
        artifact = train_mlb_model(
            self.db,
            trained_at=datetime(2026, 1, 15, tzinfo=UTC),
            minimum_development_games=20,
            minimum_validation_games=20,
            minimum_locked_test_games=20,
        )
        prediction = datetime(2026, 8, 16, 18, tzinfo=UTC)
        home_id, _home_name, stadium = self.teams[0]
        away_id = self.teams[1][0]
        kickoff = prediction + timedelta(hours=4)
        self.db.execute(
            """
            INSERT INTO games (game_id,source_game_id,sport,season,season_type,week,home_team_id,away_team_id,kickoff_time,stadium_id,status,created_at,updated_at)
            VALUES ('mlb_upcoming','999999','mlb',2026,'regular',0,?,?,?,?, 'scheduled',?,?)
            """,
            (home_id, away_id, kickoff.isoformat(), stadium, prediction.isoformat(), prediction.isoformat()),
        )
        self.db.commit()
        run_id = create_mlb_model_run(self.db, game_id="mlb_upcoming", prediction_time=prediction, artifact=artifact)
        row = self.db.execute("SELECT * FROM model_runs WHERE model_run_id=?", (run_id,)).fetchone()
        self.assertEqual(row["sport"], "mlb")
        self.assertEqual(row["market_key"], "moneylines")
        self.assertEqual(row["status"], "test_only")
        self.assertEqual(row["push_probability"], 0)
        self.assertAlmostEqual(row["home_cover_probability"] + row["away_cover_probability"], 1.0, places=9)
        self.assertEqual(row["oos_decision_count"], 0)

    def test_model_run_rejects_nfl_game(self):
        self._seed_synthetic_history()
        artifact = train_mlb_model(
            self.db,
            trained_at=datetime(2026, 1, 15, tzinfo=UTC),
            minimum_development_games=20,
            minimum_validation_games=20,
            minimum_locked_test_games=20,
        )
        # Seeded DB has no games; create an NFL game with real NFL teams.
        nfl = self.db.execute("SELECT team_id,primary_stadium_id FROM teams WHERE sport='nfl' ORDER BY team_id LIMIT 2").fetchall()
        when = datetime(2026, 9, 1, 20, tzinfo=UTC)
        self.db.execute(
            """INSERT INTO games (game_id,sport,season,season_type,week,home_team_id,away_team_id,kickoff_time,stadium_id,status,created_at,updated_at)
               VALUES ('nfl-x','nfl',2026,'regular',1,?,?,?,?, 'scheduled',?,?)""",
            (nfl[0]["team_id"], nfl[1]["team_id"], (when+timedelta(hours=4)).isoformat(), nfl[0]["primary_stadium_id"], when.isoformat(), when.isoformat()),
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "MLB game not found"):
            create_mlb_model_run(self.db, game_id="nfl-x", prediction_time=when, artifact=artifact)

    def test_phase4_tables_exist_without_breaking_shared_schema(self):
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("historical_mlb_games", tables)
        self.assertIn("mlb_model_artifacts", tables)
        self.assertIn("historical_training_games", tables)  # NFL remains intact


if __name__ == "__main__":
    unittest.main()
