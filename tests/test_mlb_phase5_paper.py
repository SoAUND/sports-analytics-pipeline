from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from sports_analytics_pipeline.collectors.base import MappingTeamResolver
from sports_analytics_pipeline.collectors.mlb_official import MappingStadiumResolver, parse_mlb_schedule
from sports_analytics_pipeline.collectors.storage import CollectorRepository
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import alias_mapping, seed_reference_data, stadium_alias_mapping
from sports_analytics_pipeline.paper import paper_report, record_paper_decision, settle_paper_decisions

UTC = timezone.utc
NOW = datetime(2026, 8, 16, 16, 0, tzinfo=UTC)


class MLBPhase5PaperTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)
        seed_reference_data(self.db, include_mlb=True)
        self.teams = MappingTeamResolver(alias_mapping(self.db, "mlb"))
        self.stadiums = MappingStadiumResolver(stadium_alias_mapping(self.db, "mlb"))

    def tearDown(self):
        self.db.close()

    def _schedule_payload(self, *, final=False, home_score=None, away_score=None):
        home = {"team": {"name": "New York Yankees"}}
        away = {"team": {"name": "Boston Red Sox"}}
        if home_score is not None:
            home["score"] = home_score
        if away_score is not None:
            away["score"] = away_score
        return {"dates": [{"date": "2026-08-16", "games": [{
            "gamePk": 990001,
            "season": "2026",
            "gameDate": "2026-08-16T20:00:00Z",
            "status": {"abstractGameState": "Final" if final else "Preview", "detailedState": "Final" if final else "Scheduled"},
            "venue": {"name": "Yankee Stadium"},
            "teams": {"home": home, "away": away},
        }]}]}

    def _store_game(self, *, game_id="mlb_paper", sport="mlb", status="scheduled", home_score=None, away_score=None):
        home = "mlb_nyy" if sport == "mlb" else "BUF"
        away = "mlb_bos" if sport == "mlb" else "MIA"
        stadium = "yankee_stadium" if sport == "mlb" else self.db.execute(
            "SELECT primary_stadium_id FROM teams WHERE team_id=?", (home,)
        ).fetchone()[0]
        
        suffix = int(game_id.rsplit("_", 1)[-1]) if game_id.startswith("mlb_count_") else (sum(map(ord, game_id)) % 10000)
        kickoff = NOW + timedelta(hours=4, seconds=suffix)
        self.db.execute(
            """INSERT INTO games
               (game_id,source_game_id,sport,season,season_type,week,home_team_id,away_team_id,
                kickoff_time,stadium_id,status,home_score,away_score,created_at,updated_at)
               VALUES (?,?,?,?, 'regular',0,?,?,?,?,?,?,?,?,?)""",
            (game_id, game_id, sport, 2026, home, away, kickoff.isoformat(), stadium, status,
             home_score, away_score, NOW.isoformat(), NOW.isoformat()),
        )
        self.db.commit()
        return self.db.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()

    def _paper_outcome(self, *, team="mlb_nyy", odds=1.8, decision="test_only_selection", odds_snapshot_id=None):
        return {
            "market_key": "moneylines",
            "decision": decision,
            "team_id": team if decision == "test_only_selection" else None,
            "sportsbook": "hardrockbet",
            "odds_snapshot_id": odds_snapshot_id if decision == "test_only_selection" else None,
            "decimal_odds": odds if decision == "test_only_selection" else None,
            "checked_at": NOW.isoformat(),
            "model_probability": 0.62,
            "conservative_probability": 0.60,
            "push_probability": 0.0,
            "break_even_probability": 1 / odds if decision == "test_only_selection" else None,
            "probability_edge": 0.044444 if decision == "test_only_selection" else None,
            "ev_per_dollar": 0.08 if decision == "test_only_selection" else None,
            "minimum_acceptable_decimal_odds": 1.75,
            "pick_strength_score": 82,
            "confidence": "strong",
            "model_version": "mlb-elo-logit-v1",
            "feature_version": "mlb-elo-v1",
            "data_version": "phase5-test",
            "gates": {},
            "supporting_reasons": [],
            "edge_loss_reasons": [],
            "source_notes": [],
            "pass_reason": "missing context" if decision == "pass" else None,
        }

    def _entry_quote(self, game_id="mlb_paper", *, team="mlb_nyy", odds=1.8, updated=None):
        updated = updated or NOW
        opponent = "mlb_bos" if team == "mlb_nyy" else "mlb_nyy"
        cursor = self.db.execute(
            """INSERT INTO odds_snapshots
               (game_id,sportsbook_key,market_key,period,settlement_rule,outcome_team_id,
                opponent_team_id,spread_points,decimal_odds,source_updated_at,collected_at,
                market_status,is_accessible,is_consensus_eligible)
               VALUES (?, 'hardrockbet', 'moneylines', 'full_game', 'standard', ?, ?, NULL, ?, ?, ?,
                       'open', 1, 1)""",
            (game_id, team, opponent, odds, updated.isoformat(), updated.isoformat()),
        )
        self.db.commit()
        return cursor.lastrowid

    def test_final_schedule_parser_captures_scores(self):
        batch = parse_mlb_schedule(
            self._schedule_payload(final=True, home_score=6, away_score=3),
            team_resolver=self.teams,
            stadium_resolver=self.stadiums,
            collected_at=NOW + timedelta(hours=8),
            expected_date="2026-08-16",
        )
        game = batch.games[0]
        self.assertEqual(game.status, "final")
        self.assertEqual((game.home_score, game.away_score), (6, 3))

    def test_schedule_storage_updates_final_scores(self):
        repo = CollectorRepository(self.db)
        scheduled = parse_mlb_schedule(
            self._schedule_payload(), team_resolver=self.teams, stadium_resolver=self.stadiums,
            collected_at=NOW, expected_date="2026-08-16",
        )
        repo.persist_mlb_schedule(scheduled)
        final = parse_mlb_schedule(
            self._schedule_payload(final=True, home_score=7, away_score=2),
            team_resolver=self.teams, stadium_resolver=self.stadiums,
            collected_at=NOW + timedelta(hours=8), expected_date="2026-08-16",
        )
        repo.persist_mlb_schedule(final)
        row = self.db.execute("SELECT status,home_score,away_score FROM games WHERE game_id='mlb_990001'").fetchone()
        self.assertEqual((row["status"], row["home_score"], row["away_score"]), ("final", 7, 2))

    def test_first_mlb_paper_decision_is_immutable(self):
        game = self._store_game()
        entry_id = self._entry_quote()
        first = record_paper_decision(self.db, game=game, outcome=self._paper_outcome(odds_snapshot_id=entry_id), decision_time=NOW)
        second = record_paper_decision(self.db, game=game, outcome=self._paper_outcome(team="mlb_bos", odds=2.1, odds_snapshot_id=entry_id), decision_time=NOW + timedelta(minutes=5))
        self.assertEqual(first["status"], "recorded")
        self.assertEqual(second["status"], "already_recorded")
        row = self.db.execute("SELECT selected_team_id FROM paper_decisions WHERE game_id='mlb_paper'").fetchone()
        self.assertEqual(row["selected_team_id"], "mlb_nyy")

    def test_mlb_regular_decision_counts_toward_mlb_only(self):
        game = self._store_game()
        entry_id = self._entry_quote()
        record_paper_decision(self.db, game=game, outcome=self._paper_outcome(odds_snapshot_id=entry_id), decision_time=NOW)
        self.assertEqual(paper_report(self.db, sport="mlb")["recorded_regular_season_decisions"], 1)
        self.assertEqual(paper_report(self.db, sport="nfl")["recorded_regular_season_decisions"], 0)

    def test_mlb_pass_counts_as_decision_but_not_selection(self):
        game = self._store_game()
        record_paper_decision(self.db, game=game, outcome=self._paper_outcome(decision="pass"), decision_time=NOW)
        report = paper_report(self.db, sport="mlb")
        self.assertEqual(report["recorded_regular_season_decisions"], 1)
        self.assertEqual(report["passes"], 1)
        self.assertEqual(report["selections"], 0)

    def test_mlb_moneyline_win_settles_with_unit_profit(self):
        game = self._store_game(status="final", home_score=5, away_score=2)
        entry_id = self._entry_quote()
        record_paper_decision(self.db, game=game, outcome=self._paper_outcome(odds=1.8, odds_snapshot_id=entry_id), decision_time=NOW)
        result = settle_paper_decisions(self.db, as_of=NOW + timedelta(hours=6), sport="mlb")
        self.assertEqual(result["newly_settled"], 1)
        row = self.db.execute("SELECT cover_result,paper_profit_loss FROM paper_results").fetchone()
        self.assertEqual(row["cover_result"], "win")
        self.assertAlmostEqual(row["paper_profit_loss"], 0.8)

    def test_mlb_moneyline_loss_settles_minus_one_unit(self):
        game = self._store_game(status="final", home_score=1, away_score=4)
        entry_id = self._entry_quote()
        record_paper_decision(self.db, game=game, outcome=self._paper_outcome(odds_snapshot_id=entry_id), decision_time=NOW)
        settle_paper_decisions(self.db, as_of=NOW + timedelta(hours=6), sport="mlb")
        row = self.db.execute("SELECT cover_result,paper_profit_loss FROM paper_results").fetchone()
        self.assertEqual((row["cover_result"], row["paper_profit_loss"]), ("loss", -1.0))

    def test_mlb_scoped_settlement_does_not_settle_nfl(self):
        mlb = self._store_game(game_id="mlb_final", status="final", home_score=5, away_score=2)
        entry_id = self._entry_quote(game_id="mlb_final")
        record_paper_decision(self.db, game=mlb, outcome=self._paper_outcome(odds_snapshot_id=entry_id), decision_time=NOW)
        nfl = self._store_game(game_id="nfl_final", sport="nfl", status="final", home_score=24, away_score=17)
        record_paper_decision(self.db, game=nfl, outcome=self._paper_outcome(team="BUF", decision="pass"), decision_time=NOW)
        result = settle_paper_decisions(self.db, as_of=NOW + timedelta(hours=6), sport="mlb")
        self.assertEqual(result["newly_settled"], 1)
        ids = {r[0] for r in self.db.execute("SELECT pd.game_id FROM paper_results pr JOIN paper_decisions pd ON pd.paper_decision_id=pr.paper_decision_id")}
        self.assertEqual(ids, {"mlb_final"})

    def test_mlb_report_reaches_review_required_at_100(self):
        for i in range(100):
            game = self._store_game(game_id=f"mlb_count_{i}")
            record_paper_decision(self.db, game=game, outcome=self._paper_outcome(decision="pass"), decision_time=NOW + timedelta(seconds=i))
        report = paper_report(self.db, sport="mlb")
        self.assertEqual(report["recorded_regular_season_decisions"], 100)
        self.assertEqual(report["remaining"], 0)
        self.assertEqual(report["approval_status"], "review_required")
        self.assertIn("does not approve", report["notice"])

    def test_settlement_rejects_unknown_sport(self):
        with self.assertRaisesRegex(ValueError, "Unsupported sport"):
            settle_paper_decisions(self.db, sport="nba")


if __name__ == "__main__":
    unittest.main()
