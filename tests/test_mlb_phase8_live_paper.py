from __future__ import annotations
from datetime import datetime, timezone, timedelta
import unittest

from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.mlb_live_paper import (
    build_live_paper_readiness,
    latest_live_paper_run,
    model_eligible_game_ids_from_report,
    store_live_paper_run,
)

UTC=timezone.utc
NOW=datetime(2026,8,16,20,0,tzinfo=UTC)
DATE="2026-08-16"
GAME="mlb_live_test"

class MLBPhase8LivePaperTests(unittest.TestCase):
    def setUp(self):
        self.db=connect(":memory:")
        initialize_schema(self.db)
        seed_reference_data(self.db,include_mlb=True)

    def tearDown(self): self.db.close()

    def _game(self):
        t=NOW.isoformat()
        self.db.execute("""
          INSERT INTO games(game_id,source_game_id,sport,season,season_type,week,home_team_id,away_team_id,kickoff_time,stadium_id,status,created_at,updated_at)
          VALUES(?,?,'mlb',2026,'regular',0,'mlb_ari','mlb_atl',?,'chase_field','scheduled',?,?)
        """,(GAME,"live_test",datetime(2026,8,16,23,0,tzinfo=UTC).isoformat(),t,t))
        self.db.commit()

    def _players_and_context(self,confirmed=True):
        for pid,team,name in (("p_home","mlb_ari","Home Pitcher"),("p_away","mlb_atl","Away Pitcher")):
            self.db.execute("INSERT INTO players(player_id,sport,player_name,primary_team_id,position,verified_on) VALUES(?,'mlb',?,?, 'P','2026-08-16')",(pid,name,team))
        self.db.execute("""
          INSERT INTO mlb_game_context(game_id,confirmed_home_pitcher_id,confirmed_away_pitcher_id,home_lineup_status,away_lineup_status,collected_at)
          VALUES(?,?,?,?,?,?)
        """,(GAME,"p_home" if confirmed else None,"p_away" if confirmed else None,"confirmed" if confirmed else "projected","confirmed" if confirmed else "projected",NOW.isoformat()))
        self.db.commit()

    def _official_checks(self,missing=None):
        missing=missing or set()
        source_map={"injuries":"mlb_injuries","lineups":"mlb_lineups","transactions":"mlb_transactions","probable_pitchers":"mlb_probable_pitchers"}
        for check,source in source_map.items():
            if check in missing: continue
            self.db.execute("""
              INSERT INTO source_checks(game_id,source_key,check_type,access_status,checked_at,source_url)
              VALUES(?,?,?,'confirmed',?,?)
            """,(GAME,source,check,NOW.isoformat(),"https://example.invalid/"+check))
        self.db.commit()

    def _odds(self,age_minutes=5):
        stamp=(NOW-timedelta(minutes=age_minutes)).isoformat()
        for team,opp,price in (("mlb_ari","mlb_atl",1.91),("mlb_atl","mlb_ari",2.02)):
            self.db.execute("""
              INSERT INTO odds_snapshots(game_id,sportsbook_key,market_key,period,settlement_rule,outcome_team_id,opponent_team_id,spread_points,decimal_odds,source_updated_at,collected_at,market_status,is_accessible,is_consensus_eligible)
              VALUES(?,'testbook','moneylines','full_game','standard',?,?,NULL,?,?,?,'open',1,1)
            """,(GAME,team,opp,price,stamp,stamp))
        self.db.commit()

    def _consensus_only_odds(self, age_minutes=5):
        stamp=(NOW-timedelta(minutes=age_minutes)).isoformat()
        for team,opp,price in (("mlb_ari","mlb_atl",1.91),("mlb_atl","mlb_ari",2.02)):
            self.db.execute("""
              INSERT INTO odds_snapshots(game_id,sportsbook_key,market_key,period,settlement_rule,outcome_team_id,opponent_team_id,spread_points,decimal_odds,source_updated_at,collected_at,market_status,is_accessible,is_consensus_eligible)
              VALUES(?,'consensusbook','moneylines','full_game','standard',?,?,NULL,?,?,?,'open',0,1)
            """,(GAME,team,opp,price,stamp,stamp))
        self.db.commit()

    def _reviewed_context(self,complete=True):
        self.db.execute("""
          INSERT INTO evaluation_contexts(evaluation_context_id,game_id,as_of,research_complete,market_match_confirmed,material_information_complete,information_quality,conflict_status,exposure_status,stability_status,manual_approval_status,created_at)
          VALUES('ctx',?,?,?,1,?,'full_official',?,'clear','robust','pending',?)
        """,(GAME,NOW.isoformat(),1 if complete else 0,1 if complete else 0,'clear' if complete else 'conflict',NOW.isoformat()))
        self.db.commit()

    def _material_context_waiting_for_accessible_price(self):
        self.db.execute("""
          INSERT INTO evaluation_contexts(evaluation_context_id,game_id,as_of,research_complete,market_match_confirmed,material_information_complete,information_quality,conflict_status,exposure_status,stability_status,manual_approval_status,created_at)
          VALUES('ctx',?,?,0,0,1,'full_official','clear','clear','robust','pending',?)
        """,(GAME,NOW.isoformat(),NOW.isoformat()))
        self.db.commit()

    def _fully_ready(self):
        self._game(); self._players_and_context(); self._official_checks(); self._odds(); self._reviewed_context()

    def test_phase8_table_exists(self):
        tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("mlb_live_paper_runs",tables)

    def test_no_games_is_truthfully_reported(self):
        out=build_live_paper_readiness(self.db,operation_date=DATE,as_of=NOW)
        self.assertEqual(out["scheduled_regular_season_games"],0)
        run=store_live_paper_run(self.db,report=out,recorded_at=NOW)
        row=self.db.execute("SELECT status FROM mlb_live_paper_runs WHERE live_run_id=?",(run,)).fetchone()
        self.assertEqual(row["status"],"no_games")

    def test_complete_current_game_is_ready(self):
        self._fully_ready()
        out=build_live_paper_readiness(self.db,operation_date=DATE,as_of=NOW)
        self.assertEqual(out["ready_games"],1)
        self.assertEqual(out["blocked_games"],0)
        self.assertEqual(out["games"][0]["blockers"],[])
        self.assertFalse(out["automatic_wagering"])
        self.assertFalse(out["official_pick_enabled"])

    def test_consensus_only_game_is_ready_without_accessible_bookmaker(self):
        self._game(); self._players_and_context(); self._official_checks()
        self._consensus_only_odds()
        self._reviewed_context()
        out=build_live_paper_readiness(self.db,operation_date=DATE,as_of=NOW)
        game=out["games"][0]
        self.assertEqual(game["status"],"ready")
        self.assertEqual(out["ready_games"],1)
        self.assertEqual(out["blocked_games"],0)
        self.assertEqual(out["model_ready_price_verification_required_games"],0)
        self.assertEqual(game["fresh_consensus_bookmakers"],["consensusbook"])
        self.assertEqual(game["fresh_trustworthy_bookmakers"],["consensusbook"])
        self.assertFalse(game["accessible_price_verification_required"])
        self.assertTrue(game["bookmaker_coverage_warnings"])
        self.assertEqual(model_eligible_game_ids_from_report(out),(GAME,))

    def test_eastern_operation_date_includes_evening_games_after_utc_midnight(self):
        self._game()
        self.db.execute(
            "UPDATE games SET kickoff_time=? WHERE game_id=?",
            (datetime(2026,8,17,2,0,tzinfo=UTC).isoformat(),GAME),
        )
        self.db.commit()
        out=build_live_paper_readiness(self.db,operation_date=DATE,as_of=NOW)
        self.assertEqual(out["scheduled_regular_season_games"],1)

    def test_stale_odds_block_game(self):
        self._game(); self._players_and_context(); self._official_checks(); self._odds(age_minutes=90); self._reviewed_context()
        out=build_live_paper_readiness(self.db,operation_date=DATE,as_of=NOW,stale_after_minutes=20)
        self.assertIn("no_fresh_trustworthy_two_sided_moneyline",out["games"][0]["blockers"])

    def test_missing_official_check_is_named(self):
        self._game(); self._players_and_context(); self._official_checks({"lineups"}); self._odds(); self._reviewed_context()
        out=build_live_paper_readiness(self.db,operation_date=DATE,as_of=NOW)
        self.assertIn("lineups",out["games"][0]["missing_official_checks"])

    def test_unconfirmed_starters_and_lineups_block(self):
        self._game(); self._players_and_context(False); self._official_checks(); self._odds(); self._reviewed_context()
        blockers=build_live_paper_readiness(self.db,operation_date=DATE,as_of=NOW)["games"][0]["blockers"]
        self.assertIn("starting_pitchers_unconfirmed",blockers)
        self.assertIn("lineups_unconfirmed",blockers)

    def test_incomplete_reviewed_research_blocks(self):
        self._game(); self._players_and_context(); self._official_checks(); self._odds(); self._reviewed_context(False)
        blockers=build_live_paper_readiness(self.db,operation_date=DATE,as_of=NOW)["games"][0]["blockers"]
        self.assertIn("reviewed_research_context_incomplete",blockers)

    def test_database_prevents_duplicate_paper_decisions(self):
        self._fully_ready()
        t=NOW.isoformat()
        self.db.execute("""
          INSERT INTO paper_decisions(paper_decision_id,game_id,season_type,market_key,period,decision_type,rule_version,gate_results_json,countable_oos,decision_time,created_at)
          VALUES('pd1',?,'regular','moneylines','full_game','pass','mlb-rules','{}',1,?,?)
        """,(GAME,t,t))
        with self.assertRaisesRegex(Exception,"UNIQUE constraint failed"):
            self.db.execute("""
              INSERT INTO paper_decisions(paper_decision_id,game_id,season_type,market_key,period,decision_type,rule_version,gate_results_json,countable_oos,decision_time,created_at)
              VALUES('pd2',?,'regular','moneylines','full_game','pass','mlb-rules','{}',1,?,?)
            """,(GAME,t,t))
        self.db.rollback()

    def test_store_and_latest_round_trip(self):
        self._fully_ready()
        report=build_live_paper_readiness(self.db,operation_date=DATE,as_of=NOW)
        run_id=store_live_paper_run(self.db,report=report,recorded_at=NOW)
        latest=latest_live_paper_run(self.db)
        self.assertEqual(latest["live_run_id"],run_id)
        self.assertEqual(latest["status"],"ready")
        self.assertEqual(latest["ready_games"],1)

    def test_latest_without_runs_is_none(self):
        self.assertIsNone(latest_live_paper_run(self.db))

    def test_invalid_date_and_freshness_fail_closed(self):
        with self.assertRaisesRegex(ValueError,"YYYY-MM-DD"):
            build_live_paper_readiness(self.db,operation_date="08/16/2026",as_of=NOW)
        with self.assertRaisesRegex(ValueError,"positive"):
            build_live_paper_readiness(self.db,operation_date=DATE,as_of=NOW,stale_after_minutes=0)

if __name__=="__main__": unittest.main()
