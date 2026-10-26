import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from sports_analytics_pipeline.calculations import remove_vig
from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import alias_mapping, seed_reference_data, stadium_alias_mapping
from sports_analytics_pipeline.game_matching import strict_game_identity_match
from sports_analytics_pipeline.mlb import ODDS_API_PROVIDER_MARKET_KEY, ODDS_API_SPORT_KEY, INTERNAL_MARKET_KEY
from sports_analytics_pipeline.market_matching import (
    MoneylineQuote,
    match_two_sided_moneyline,
    moneyline_quote_is_fresh,
)
from sports_analytics_pipeline.paper import paper_report, record_paper_decision


UTC = timezone.utc


class MLBPhase1Tests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)
        seed_reference_data(self.db, include_mlb=True)

    def tearDown(self):
        self.db.close()

    def test_odds_api_mlb_keys_are_declared_without_live_collection(self):
        self.assertEqual(ODDS_API_SPORT_KEY, "baseball_mlb")
        self.assertEqual(ODDS_API_PROVIDER_MARKET_KEY, "h2h")
        self.assertEqual(INTERNAL_MARKET_KEY, "moneylines")

    def test_required_mlb_sources_are_seeded(self):
        required = {
            "mlb_injuries", "mlb_lineups", "mlb_transactions",
            "mlb_probable_pitchers", "baseball_savant", "fangraphs",
            "baseball_reference", "the_odds_api",
        }
        found = {
            row[0] for row in self.db.execute(
                "SELECT source_key FROM sources WHERE source_key IN (%s)"
                % ",".join("?" for _ in required),
                tuple(required),
            )
        }
        self.assertEqual(found, required)

    def test_exactly_30_current_mlb_teams_and_venues(self):
        teams = self.db.execute("SELECT * FROM teams WHERE sport = 'mlb'").fetchall()
        venues = self.db.execute(
            """SELECT DISTINCT s.stadium_id FROM stadiums s
               JOIN teams t ON t.primary_stadium_id = s.stadium_id
               WHERE t.sport = 'mlb'"""
        ).fetchall()
        self.assertEqual(len(teams), 30)
        self.assertEqual(len(venues), 30)

    def test_2026_changed_ballparks_are_current(self):
        expected = {
            "mlb_ath": ("Athletics", "Sutter Health Park"),
            "mlb_cws": ("Chicago White Sox", "Rate Field"),
            "mlb_hou": ("Houston Astros", "Daikin Park"),
            "mlb_tb": ("Tampa Bay Rays", "Tropicana Field"),
        }
        for team_id, (team_name, park_name) in expected.items():
            row = self.db.execute(
                """SELECT t.official_name, s.official_name AS park
                   FROM teams t JOIN stadiums s ON s.stadium_id=t.primary_stadium_id
                   WHERE t.team_id=? AND t.sport='mlb'""",
                (team_id,),
            ).fetchone()
            self.assertEqual((row["official_name"], row["park"]), (team_name, park_name))

    def test_aliases_are_sport_scoped_and_unknown_alias_rejected(self):
        nfl = alias_mapping(self.db, "nfl")
        mlb = alias_mapping(self.db, "mlb")
        self.assertEqual(nfl["Giants"], "NYG")
        self.assertEqual(mlb["Giants"], "mlb_sf")
        self.assertEqual(mlb["Yankees"], "mlb_nyy")
        self.assertNotIn("Some Unknown Baseball Team", mlb)
        self.assertNotIn("Yankees", nfl)

    def test_mlb_stadium_alias_mapping_is_exact_sport_set(self):
        venues = stadium_alias_mapping(self.db, "mlb")
        # The map contains all 30 official current venues plus reviewed provider
        # and former sponsor names that refer to those same controlled records.
        current_ids = {
            row["primary_stadium_id"]
            for row in self.db.execute("SELECT primary_stadium_id FROM teams WHERE sport='mlb'")
        }
        self.assertEqual(len(current_ids), 30)
        self.assertTrue(set(venues.values()).issubset(current_ids))
        self.assertEqual(venues["Sutter Health Park"], "sutter_health_park")
        self.assertEqual(venues["Minute Maid Park"], "daikin_park")
        self.assertEqual(venues["Oriole Park"], "camden_yards")
        self.assertNotIn("Reliant Stadium", venues)

    def _insert_game(self, game_id: str, sport: str, home: str, away: str, stadium: str):
        self.db.execute(
            """INSERT INTO games (
                 game_id, sport, season, season_type, week, home_team_id, away_team_id,
                 kickoff_time, stadium_id, neutral_site, status, created_at, updated_at
               ) VALUES (?, ?, 2026, 'regular', 0, ?, ?, ?, ?, 0, 'scheduled', ?, ?)""",
            (
                game_id,
                sport,
                home,
                away,
                "2026-08-16T20:00:00+00:00",
                stadium,
                "2026-08-15T20:00:00+00:00",
                "2026-08-15T20:00:00+00:00",
            ),
        )
        self.db.commit()
        return self.db.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()

    def test_strict_game_identity_is_sport_aware(self):
        self._insert_game("mlb-test", "mlb", "mlb_nyy", "mlb_bos", "yankee_stadium")
        match = strict_game_identity_match(
            self.db,
            sport="mlb",
            season=2026,
            home_team_id="mlb_nyy",
            away_team_id="mlb_bos",
            kickoff_time="2026-08-16T20:00:00+00:00",
            stadium_id="yankee_stadium",
        )
        self.assertEqual(match["game_id"], "mlb-test")
        self.assertIsNone(
            strict_game_identity_match(
                self.db,
                sport="nfl",
                season=2026,
                home_team_id="mlb_nyy",
                away_team_id="mlb_bos",
                kickoff_time="2026-08-16T20:00:00+00:00",
                stadium_id="yankee_stadium",
            )
        )

    def _moneylines(self, **overrides):
        now = datetime(2026, 8, 15, 22, 0, tzinfo=UTC)
        base = dict(
            event_id="event-1",
            snapshot_id="book-snapshot-1",
            bookmaker_key="hardrockbet",
            market_key="moneylines",
            period="full_game",
            settlement_rule="full_game_including_extra_innings",
            source_updated_at=now,
            collected_at=now,
            status="open",
        )
        base.update(overrides)
        first = MoneylineQuote(
            team_id="mlb_nyy",
            opponent_team_id="mlb_bos",
            decimal_odds=Decimal("1.80"),
            **base,
        )
        second_base = dict(base)
        second_base.update(overrides.get("second", {})) if isinstance(overrides.get("second"), dict) else None
        second_base.pop("second", None)
        second = MoneylineQuote(
            team_id="mlb_bos",
            opponent_team_id="mlb_nyy",
            decimal_odds=Decimal("2.10"),
            **second_base,
        )
        return first, second

    def test_moneyline_pair_requires_same_snapshot_and_supports_no_vig(self):
        first, second = self._moneylines()
        pair = match_two_sided_moneyline(first, second)
        probs = remove_vig([pair[0].decimal_odds, pair[1].decimal_odds])
        self.assertAlmostEqual(float(sum(probs)), 1.0, places=12)

    def test_moneyline_rejects_mismatched_snapshot_book_period_or_missing_side(self):
        first, second = self._moneylines()
        for field, value in (
            ("snapshot_id", "other-snapshot"),
            ("bookmaker_key", "other-book"),
            ("period", "first_five"),
        ):
            changed = MoneylineQuote(**{**second.__dict__, field: value})
            with self.assertRaises(Exception):
                match_two_sided_moneyline(first, changed)
        with self.assertRaises(Exception):
            remove_vig([first.decimal_odds])

    def test_moneyline_freshness_rejects_stale_and_future_prices(self):
        first, _ = self._moneylines()
        self.assertTrue(moneyline_quote_is_fresh(first, first.collected_at + timedelta(minutes=30)))
        self.assertFalse(moneyline_quote_is_fresh(first, first.collected_at + timedelta(minutes=61)))
        self.assertFalse(moneyline_quote_is_fresh(first, first.collected_at - timedelta(minutes=1)))

    def test_shared_odds_table_accepts_moneyline_only_with_null_spread(self):
        self._insert_game("mlb-odds", "mlb", "mlb_nyy", "mlb_bos", "yankee_stadium")
        self.db.execute(
            """INSERT INTO odds_snapshots (
              game_id, sportsbook_key, market_key, period, settlement_rule,
              outcome_team_id, opponent_team_id, spread_points, decimal_odds,
              american_odds, source_updated_at, collected_at, market_status
            ) VALUES (?, 'hardrockbet', 'moneylines', 'full_game', ?, ?, ?, NULL, 1.8,
                      -125, ?, ?, 'open')""",
            (
                "mlb-odds",
                "full_game_including_extra_innings",
                "mlb_nyy",
                "mlb_bos",
                "2026-08-15T22:00:00+00:00",
                "2026-08-15T22:00:00+00:00",
            ),
        )
        with self.assertRaises(Exception):
            self.db.execute(
                """INSERT INTO odds_snapshots (
                  game_id, sportsbook_key, market_key, period, settlement_rule,
                  outcome_team_id, opponent_team_id, spread_points, decimal_odds,
                  source_updated_at, collected_at, market_status
                ) VALUES (?, 'bad', 'moneylines', 'full_game', ?, ?, ?, 1.5, 2.0, ?, ?, 'open')""",
                (
                    "mlb-odds",
                    "full_game_including_extra_innings",
                    "mlb_bos",
                    "mlb_nyy",
                    "2026-08-15T22:00:00+00:00",
                    "2026-08-15T22:00:00+00:00",
                ),
            )

    def test_player_identity_and_future_mlb_context_exist(self):
        self.db.execute(
            """INSERT INTO players(player_id, sport, player_name, primary_team_id, position, throws)
               VALUES ('mlb_pitcher_1','mlb','Test Pitcher','mlb_nyy','P','R')"""
        )
        self._insert_game("mlb-context", "mlb", "mlb_nyy", "mlb_bos", "yankee_stadium")
        self.db.execute(
            """INSERT INTO mlb_game_context(
                 game_id, probable_home_pitcher_id, home_lineup_status,
                 home_relievers_used_1d_json, home_starter_recent_pitch_count,
                 home_offense_vs_lhp, home_recent_5, home_rest_days, travel_notes, collected_at
               ) VALUES (?, ?, 'projected', '[]', 92, 0.321, 0.600, 1, 'none', ?)""",
            ("mlb-context", "mlb_pitcher_1", "2026-08-15T22:00:00+00:00"),
        )
        row = self.db.execute("SELECT * FROM mlb_game_context WHERE game_id='mlb-context'").fetchone()
        self.assertEqual(row["probable_home_pitcher_id"], "mlb_pitcher_1")
        self.assertEqual(row["home_starter_recent_pitch_count"], 92)

    def test_nfl_and_mlb_100_decision_progress_are_separate(self):
        nfl_game = self._insert_game("nfl-count", "nfl", "BUF", "MIA", "highmark_stadium_2026")
        mlb_game = self._insert_game("mlb-count", "mlb", "mlb_nyy", "mlb_bos", "yankee_stadium")
        decision_time = datetime(2026, 8, 15, 22, 0, tzinfo=UTC)
        record_paper_decision(
            self.db,
            game=nfl_game,
            outcome={"decision": "pass", "pass_reason": "test", "market_key": "spreads"},
            decision_time=decision_time,
        )
        record_paper_decision(
            self.db,
            game=mlb_game,
            outcome={"decision": "pass", "pass_reason": "test", "market_key": "moneylines"},
            decision_time=decision_time,
        )
        self.assertEqual(paper_report(self.db, sport="nfl")["recorded_regular_season_decisions"], 1)
        self.assertEqual(paper_report(self.db, sport="mlb")["recorded_regular_season_decisions"], 1)

    def test_mlb_uses_existing_test_only_runtime_gate(self):
        with patch.dict("os.environ", {"BOT_MODE": "test_only"}, clear=True):
            self.assertEqual(Settings.load(env_file="does-not-exist").bot_mode, "test_only")
        with patch.dict("os.environ", {"BOT_MODE": "official"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.load(env_file="does-not-exist")


if __name__ == "__main__":
    unittest.main()
