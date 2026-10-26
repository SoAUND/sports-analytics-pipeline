from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import unittest

from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.mlb_reliability import (
    build_mlb_reliability_health,
    build_reliable_operations_command,
    execute_reliable_mlb_operations,
    latest_mlb_reliability_summary,
    settlement_recheck_count,
)

UTC=timezone.utc
NOW=datetime(2026,8,16,20,0,tzinfo=UTC)
DATE="2026-08-16"

class MLBPhase9ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.db=connect(":memory:"); initialize_schema(self.db); seed_reference_data(self.db,include_mlb=True)
    def tearDown(self): self.db.close()

    def test_phase9_table_exists(self):
        tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("mlb_reliability_runs",tables)

    def test_reliable_command_uses_existing_operations_layer(self):
        cmd=build_reliable_operations_command(operation_date=DATE,hours=24)
        self.assertIn("scripts/run_mlb_operations.py",cmd)
        self.assertIn(DATE,cmd)

    def test_non_test_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError,"supported research mode"):
            execute_reliable_mlb_operations(self.db,project_root=Path('.'),operation_date=DATE,bot_mode='live',command=('x',),now=NOW)

    def test_failed_attempt_is_retried_and_success_is_persisted(self):
        calls=[]
        def runner(command,cwd):
            calls.append(1); code=1 if len(calls)==1 else 0
            return subprocess.CompletedProcess(command,code,"ok" if code==0 else "","temporary" if code else "")
        out=execute_reliable_mlb_operations(self.db,project_root=Path('.'),operation_date=DATE,bot_mode='test_only',command=('x',),runner=runner,max_attempts=3,now=NOW)
        self.assertEqual(out['status'],'succeeded'); self.assertEqual(out['attempts'],2)
        self.assertFalse(out['automatic_wagering']); self.assertFalse(out['automatic_model_approval'])

    def test_retry_limit_fails_closed(self):
        def fail(command,cwd): return subprocess.CompletedProcess(command,2,"","still failing")
        out=execute_reliable_mlb_operations(self.db,project_root=Path('.'),operation_date=DATE,bot_mode='test_only',command=('x',),runner=fail,max_attempts=2,now=NOW)
        self.assertEqual(out['status'],'failed'); self.assertEqual(out['attempts'],2)

    def test_successful_date_is_not_run_twice(self):
        def ok(command,cwd): return subprocess.CompletedProcess(command,0,"ok","")
        first=execute_reliable_mlb_operations(self.db,project_root=Path('.'),operation_date=DATE,bot_mode='test_only',command=('x',),runner=ok,now=NOW)
        second=execute_reliable_mlb_operations(self.db,project_root=Path('.'),operation_date=DATE,bot_mode='test_only',command=('x',),runner=ok,now=NOW+timedelta(minutes=5))
        self.assertEqual(first['status'],'succeeded'); self.assertEqual(second['status'],'skipped_duplicate')
        self.assertEqual(second['prior_run_id'],first['reliability_run_id'])

    def test_active_same_date_lock_blocks_overlap(self):
        self.db.execute("INSERT INTO mlb_reliability_runs(reliability_run_id,operation_date,mode,status,started_at) VALUES('r1',?,'test_only','running',?)",(DATE,NOW.isoformat())); self.db.commit()
        with self.assertRaisesRegex(RuntimeError,"already active"):
            execute_reliable_mlb_operations(self.db,project_root=Path('.'),operation_date=DATE,bot_mode='test_only',command=('x',),runner=lambda c,w: subprocess.CompletedProcess(c,0,'',''),now=NOW+timedelta(minutes=10))

    def test_stale_running_lock_is_abandoned_and_recovered(self):
        old=NOW-timedelta(hours=4)
        self.db.execute("INSERT INTO mlb_reliability_runs(reliability_run_id,operation_date,mode,status,started_at) VALUES('old',?,'test_only','running',?)",(DATE,old.isoformat())); self.db.commit()
        out=execute_reliable_mlb_operations(self.db,project_root=Path('.'),operation_date=DATE,bot_mode='test_only',command=('x',),runner=lambda c,w: subprocess.CompletedProcess(c,0,'ok',''),now=NOW)
        status=self.db.execute("SELECT status FROM mlb_reliability_runs WHERE reliability_run_id='old'").fetchone()[0]
        self.assertEqual(status,'abandoned'); self.assertEqual(out['status'],'succeeded')

    def test_final_unsettled_selection_is_flagged_for_recheck(self):
        t=NOW.isoformat()
        self.db.execute("""INSERT INTO games(game_id,source_game_id,sport,season,season_type,week,home_team_id,away_team_id,kickoff_time,stadium_id,status,created_at,updated_at)
            VALUES('g','g','mlb',2026,'regular',0,'mlb_ari','mlb_atl',?,'chase_field','final',?,?)""",(t,t,t))
        self.db.execute("""INSERT INTO odds_snapshots(game_id,sportsbook_key,market_key,period,settlement_rule,outcome_team_id,opponent_team_id,spread_points,decimal_odds,source_updated_at,collected_at,market_status,is_accessible,is_consensus_eligible)
            VALUES('g','testbook','moneylines','full_game','standard','mlb_ari','mlb_atl',NULL,1.91,?,?,'open',1,1)""",(t,t))
        oid=self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.db.execute("""INSERT INTO paper_decisions(paper_decision_id,game_id,season_type,market_key,period,decision_type,selected_team_id,selected_odds_snapshot_id,rule_version,gate_results_json,countable_oos,decision_time,created_at)
            VALUES('p','g','regular','moneylines','full_game','test_only_selection','mlb_ari',?,'rules','{}',1,?,?)""",(oid,t,t)); self.db.commit()
        self.assertEqual(settlement_recheck_count(self.db),1)
        self.assertIn('final_test_selections_need_settlement_recheck',build_mlb_reliability_health(self.db)['warnings'])

    def test_health_detects_failed_operations_and_blocked_live_run(self):
        t=NOW.isoformat()
        self.db.execute("INSERT INTO mlb_operations_runs(run_id,operation_date,mode,status,started_at,failed_step,summary_json) VALUES('o',?,'test_only','failed',?,'collect','{}')",(DATE,t))
        self.db.execute("INSERT INTO mlb_live_paper_runs(live_run_id,operation_date,mode,status,scheduled_games,ready_games,blocked_games,paper_decisions,report_json,recorded_at) VALUES('l',?,'test_only','blocked',1,0,1,0,'{}',?)",(DATE,t)); self.db.commit()
        h=build_mlb_reliability_health(self.db)
        self.assertEqual(h['status'],'attention_required'); self.assertIn('latest_operations_run_failed',h['warnings']); self.assertIn('latest_live_paper_run_blocked',h['warnings'])

    def test_latest_summary_round_trip_and_invalid_inputs_fail_closed(self):
        self.assertIsNone(latest_mlb_reliability_summary(self.db))
        out=execute_reliable_mlb_operations(self.db,project_root=Path('.'),operation_date=DATE,bot_mode='test_only',command=('x',),runner=lambda c,w: subprocess.CompletedProcess(c,0,'ok',''),now=NOW)
        self.assertEqual(latest_mlb_reliability_summary(self.db)['reliability_run_id'],out['reliability_run_id'])
        with self.assertRaisesRegex(ValueError,'YYYY-MM-DD'): build_reliable_operations_command(operation_date='08/16/2026')
        with self.assertRaisesRegex(ValueError,'between 1 and 5'):
            execute_reliable_mlb_operations(self.db,project_root=Path('.'),operation_date='2026-08-17',bot_mode='test_only',command=('x',),max_attempts=0,now=NOW)

if __name__=='__main__': unittest.main()

