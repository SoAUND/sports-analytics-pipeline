from __future__ import annotations
from datetime import datetime, timedelta, timezone
import json
import unittest

from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.mlb_final_review import (
    build_mlb_final_review, record_manual_mlb_disposition, store_mlb_final_review,
)
from sports_analytics_pipeline.paper import record_paper_decision

UTC=timezone.utc
NOW=datetime(2026,8,16,20,0,tzinfo=UTC)

class MLBPhase10FinalReviewTests(unittest.TestCase):
    def setUp(self):
        self.db=connect(':memory:'); initialize_schema(self.db); seed_reference_data(self.db,include_mlb=True)
    def tearDown(self): self.db.close()

    def _game(self,i,sport='mlb'):
        home,away=('mlb_nyy','mlb_bos') if sport=='mlb' else ('BUF','MIA')
        stadium='yankee_stadium' if sport=='mlb' else self.db.execute('SELECT primary_stadium_id FROM teams WHERE team_id=?',(home,)).fetchone()[0]
        gid=f'{sport}_p10_{i}'
        self.db.execute("""INSERT INTO games(game_id,source_game_id,sport,season,season_type,week,home_team_id,away_team_id,kickoff_time,stadium_id,status,home_score,away_score,created_at,updated_at) VALUES(?,?,?,?, 'regular',0,?,?,?,?, 'final',5,2,?,?)""",
            (gid,gid,sport,2026,home,away,(NOW+timedelta(hours=5,minutes=i)).isoformat(),stadium,NOW.isoformat(),NOW.isoformat()))
        self.db.commit(); return self.db.execute('SELECT * FROM games WHERE game_id=?',(gid,)).fetchone()

    def _pass(self,i,sport='mlb'):
        game=self._game(i,sport)
        outcome={'market_key':'moneylines','decision':'pass','model_probability':0.5,'conservative_probability':0.48,'push_probability':0.0,'model_version':'mlb-v1','feature_version':'mlb-v1','data_version':'p10','gates':{},'supporting_reasons':[],'edge_loss_reasons':[],'source_notes':[],'pass_reason':'no reliable edge'}
        record_paper_decision(self.db,game=game,outcome=outcome,decision_time=NOW+timedelta(seconds=i))

    def _selection(self,i,win=True,clv=0.01,model_p=0.60):
        game=self._game(i)
        team,opp=game['home_team_id'],game['away_team_id']
        cur=self.db.execute("""INSERT INTO odds_snapshots(game_id,sportsbook_key,market_key,period,settlement_rule,outcome_team_id,opponent_team_id,spread_points,decimal_odds,source_updated_at,collected_at,market_status,is_accessible,is_consensus_eligible) VALUES(?,'hardrockbet','moneylines','full_game','standard',?,?,NULL,1.9,?,?, 'open',1,1)""",(game['game_id'],team,opp,NOW.isoformat(),NOW.isoformat()))
        oid=cur.lastrowid
        outcome={'market_key':'moneylines','decision':'test_only_selection','team_id':team,'sportsbook':'hardrockbet','odds_snapshot_id':oid,'decimal_odds':1.9,'checked_at':NOW.isoformat(),'model_probability':model_p,'conservative_probability':model_p-0.02,'push_probability':0.0,'break_even_probability':1/1.9,'probability_edge':0.06,'ev_per_dollar':0.08,'minimum_acceptable_decimal_odds':1.8,'pick_strength_score':80,'confidence':'strong','model_version':'mlb-v1','feature_version':'mlb-v1','data_version':'p10','gates':{},'supporting_reasons':[],'edge_loss_reasons':[],'source_notes':[],'pass_reason':None}
        result=record_paper_decision(self.db,game=game,outcome=outcome,decision_time=NOW+timedelta(seconds=i))
        pid=result['paper_decision_id']
        self.db.execute("""INSERT INTO paper_results(paper_decision_id,home_score,away_score,cover_result,push_status,entry_spread,entry_decimal_odds,closing_spread,closing_decimal_odds,closing_sportsbook_key,closing_snapshot_time,consensus_closing_probability,line_clv_points,exact_line_price_clv_probability,close_comparison,paper_profit_loss,settled_at) VALUES(?,5,2,?,0,NULL,1.9,NULL,1.85,'hardrockbet',?,NULL,NULL,?,?,?,?)""",
            (pid,'win' if win else 'loss',(NOW+timedelta(hours=4)).isoformat(),clv,'better' if clv>0 else 'worse',0.9 if win else -1.0,(NOW+timedelta(hours=6)).isoformat()))
        self.db.commit()

    def test_under_100_is_insufficient_sample(self):
        for i in range(99): self._pass(i)
        r=build_mlb_final_review(self.db)
        self.assertEqual(r['recommendation'],'insufficient_sample'); self.assertFalse(r['automatic_approval'])

    def test_nfl_never_counts_toward_final_mlb_review(self):
        for i in range(100): self._pass(i,sport='nfl')
        self.assertEqual(build_mlb_final_review(self.db)['evidence']['recorded_oos_decisions'],0)

    def test_100_passes_do_not_create_automatic_approval(self):
        for i in range(100): self._pass(i)
        r=build_mlb_final_review(self.db)
        self.assertNotEqual(r['recommendation'],'eligible_for_manual_approval_review')
        self.assertFalse(r['automatic_wagering'])

    def test_complete_evidence_can_only_become_eligible_for_manual_review(self):
        for i in range(100): self._selection(i,win=(i%2==0),clv=0.01)
        r=build_mlb_final_review(self.db,minimum_clv_samples=30,minimum_scored_samples=30)
        self.assertEqual(r['recommendation'],'eligible_for_manual_approval_review')
        self.assertTrue(r['manual_decision_required']); self.assertFalse(r['automatic_approval'])

    def test_negative_roi_and_clv_can_recommend_retire_without_authorizing_wagering(self):
        for i in range(100): self._selection(i,win=(i%4==0),clv=-0.02,model_p=0.70)
        r=build_mlb_final_review(self.db)
        self.assertEqual(r['recommendation'],'retire'); self.assertFalse(r['automatic_wagering'])

    def test_future_price_integrity_failure_forces_revise(self):
        for i in range(100): self._selection(i,win=(i%2==0),clv=0.01)
        row=self.db.execute("SELECT selected_odds_snapshot_id FROM paper_decisions WHERE decision_type='test_only_selection' LIMIT 1").fetchone()
        self.db.execute("UPDATE odds_snapshots SET source_updated_at=? WHERE odds_snapshot_id=?",((NOW+timedelta(days=1)).isoformat(),row[0])); self.db.commit()
        r=build_mlb_final_review(self.db)
        self.assertEqual(r['recommendation'],'revise'); self.assertGreater(r['integrity_audit']['future_price_leaks'],0)

    def test_model_vs_market_baseline_is_truthful_when_unavailable(self):
        for i in range(30): self._selection(i,win=(i%2==0))
        r=build_mlb_final_review(self.db,target=30,minimum_clv_samples=1,minimum_scored_samples=1)
        self.assertEqual(r['model_vs_market_baseline']['no_vig_market_baseline']['samples'],0)
        self.assertIsNone(r['model_vs_market_baseline']['model_brier_improvement'])

    def test_final_review_snapshot_round_trip_is_test_only(self):
        self._pass(1); report=build_mlb_final_review(self.db,target=1)
        rid=store_mlb_final_review(self.db,report=report,reviewed_at=NOW)
        row=self.db.execute('SELECT * FROM mlb_final_reviews WHERE final_review_id=?',(rid,)).fetchone()
        self.assertEqual((row['automatic_approval'],row['automatic_wagering']),(0,0)); self.assertEqual(json.loads(row['report_json'])['mode'],'test_only')

    def test_manual_approval_only_means_continued_test_only_use(self):
        self._pass(1); report=build_mlb_final_review(self.db,target=1); rid=store_mlb_final_review(self.db,report=report,reviewed_at=NOW)
        did=record_manual_mlb_disposition(self.db,final_review_id=rid,disposition='approve_for_continued_test_only_use',reason='Evidence reviewed',actor='manual reviewer',decided_at=NOW)
        row=self.db.execute('SELECT * FROM mlb_manual_dispositions WHERE disposition_id=?',(did,)).fetchone()
        self.assertEqual((row['real_money_authorized'],row['automatic_wagering']),(0,0))

    def test_manual_disposition_requires_reason_actor_and_valid_review(self):
        with self.assertRaises(ValueError): record_manual_mlb_disposition(self.db,final_review_id='missing',disposition='revise',reason='x',actor='y',decided_at=NOW)
        self._pass(1); rid=store_mlb_final_review(self.db,report=build_mlb_final_review(self.db,target=1),reviewed_at=NOW)
        with self.assertRaises(ValueError): record_manual_mlb_disposition(self.db,final_review_id=rid,disposition='revise',reason='',actor='reviewer',decided_at=NOW)

    def test_invalid_review_arguments_fail_closed(self):
        with self.assertRaises(ValueError): build_mlb_final_review(self.db,target=0)
        with self.assertRaises(ValueError): build_mlb_final_review(self.db,minimum_settled_share=0)
        with self.assertRaises(ValueError): store_mlb_final_review(self.db,report=build_mlb_final_review(self.db,target=1),reviewed_at=datetime(2026,8,16,12,0))

if __name__=='__main__': unittest.main()
