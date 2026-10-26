from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import unittest

from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.mlb_operations import (
    OperationStep,
    build_daily_mlb_plan,
    execute_daily_mlb_operations,
    latest_mlb_operations_summary,
)

UTC=timezone.utc
NOW=datetime(2026,8,16,20,0,tzinfo=UTC)


class MLBPhase7OperationsTests(unittest.TestCase):
    def setUp(self):
        self.db=connect(":memory:")
        initialize_schema(self.db)

    def tearDown(self): self.db.close()

    def test_phase7_table_exists(self):
        tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("mlb_operations_runs",tables)

    def test_default_plan_orders_existing_pipeline(self):
        names=[s.name for s in build_daily_mlb_plan(date="2026-08-16")]
        self.assertEqual(names,["collect","model","evaluate_track","settle","report","review"])

    def test_plan_can_skip_collection_and_settlement(self):
        names=[s.name for s in build_daily_mlb_plan(date="2026-08-16",skip_collection=True,skip_settlement=True)]
        self.assertEqual(names,["model","evaluate_track","report","review"])

    def test_invalid_hours_fail_closed(self):
        with self.assertRaisesRegex(ValueError,"between 1 and 168"):
            build_daily_mlb_plan(date="2026-08-16",hours=0)

    def test_non_test_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError,"supported research mode"):
            execute_daily_mlb_operations(self.db,project_root=Path("."),operation_date="2026-08-16",bot_mode="live",steps=(),now=NOW)

    def test_successful_run_records_all_steps_and_no_wagering(self):
        def ok(command,cwd): return subprocess.CompletedProcess(command,0,"ok","")
        steps=(OperationStep("collect",("x",)),OperationStep("review",("y",)))
        out=execute_daily_mlb_operations(self.db,project_root=Path("."),operation_date="2026-08-16",bot_mode="test_only",steps=steps,runner=ok,now=NOW)
        self.assertEqual(out["status"],"succeeded")
        self.assertFalse(out["automatic_wagering"])
        self.assertFalse(out["automatic_model_approval"])
        self.assertEqual([x["name"] for x in out["steps"]],["collect","review"])

    def test_required_failure_stops_later_steps(self):
        called=[]
        def runner(command,cwd):
            called.append(command[0])
            return subprocess.CompletedProcess(command,1 if command[0]=="bad" else 0,"","boom" if command[0]=="bad" else "")
        steps=(OperationStep("collect",("ok",)),OperationStep("model",("bad",)),OperationStep("review",("never",)))
        out=execute_daily_mlb_operations(self.db,project_root=Path("."),operation_date="2026-08-16",bot_mode="test_only",steps=steps,runner=runner,now=NOW)
        self.assertEqual(out["status"],"failed")
        self.assertEqual(out["failed_step"],"model")
        self.assertEqual(called,["ok","bad"])

    def test_failed_run_is_persisted_for_diagnosis(self):
        def fail(command,cwd): return subprocess.CompletedProcess(command,2,"","error")
        out=execute_daily_mlb_operations(self.db,project_root=Path("."),operation_date="2026-08-16",bot_mode="test_only",steps=(OperationStep("collect",("x",)),),runner=fail,now=NOW)
        row=self.db.execute("SELECT * FROM mlb_operations_runs WHERE run_id=?",(out["run_id"],)).fetchone()
        self.assertEqual(row["status"],"failed")
        self.assertEqual(row["failed_step"],"collect")

    def test_latest_summary_returns_last_run(self):
        def ok(command,cwd): return subprocess.CompletedProcess(command,0,"ok","")
        out=execute_daily_mlb_operations(self.db,project_root=Path("."),operation_date="2026-08-16",bot_mode="test_only",steps=(OperationStep("review",("x",)),),runner=ok,now=NOW)
        latest=latest_mlb_operations_summary(self.db)
        self.assertEqual(latest["run_id"],out["run_id"])
        self.assertEqual(latest["status"],"succeeded")

    def test_latest_summary_without_runs_is_none(self):
        self.assertIsNone(latest_mlb_operations_summary(self.db))

    def test_naive_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError,"time zone"):
            execute_daily_mlb_operations(self.db,project_root=Path("."),operation_date="2026-08-16",bot_mode="test_only",steps=(),now=datetime(2026,8,16,12,0))


if __name__=="__main__": unittest.main()

