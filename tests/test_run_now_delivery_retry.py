"""On-demand research must not become only an old outbox delivery retry."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('run_now_delivery_retry', ROOT / 'scripts/run_revised_research.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunNowDeliveryRetryTests(unittest.TestCase):
    def run_fixture(self, *, run_now=True, prior_manual=False):
        now = datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'state.sqlite3'
            source = root / 'input.json'
            source.write_text(json.dumps({'candidates': []}))
            db = sqlite3.connect(database)
            runner.prepare_daily_jobs(db, now)
            db.execute('UPDATE research_jobs SET completed_at=?', (now.isoformat(),))
            db.execute('INSERT INTO research_outbox VALUES(?,?,?,NULL)',
                       ('old-report', 'OLD PENDING REPORT', now.isoformat()))
            if prior_manual:
                runner.prepare_manual_job(db, now.replace(hour=19))
            db.commit()
            db.close()
            calls = []

            def evaluate(connection, as_of, payload):
                connection.execute('CREATE TABLE IF NOT EXISTS research_parlay_audit (run_at TEXT PRIMARY KEY, result_json TEXT NOT NULL)')
                jobs = runner.due_jobs(connection, as_of)
                calls.extend(dict(job) for job in jobs)
                connection.execute('UPDATE research_jobs SET completed_at=? WHERE completed_at IS NULL',
                                   (as_of.isoformat(),))
                connection.commit()
                return []

            args = ['run_revised_research.py', '--database', str(database), '--input', str(source),
                    '--supplemental-input', str(root / 'absent.json'), '--as-of', now.isoformat()]
            if run_now:
                args.append('--run-now')
            original = Path.cwd()
            try:
                os.chdir(root)
                with patch.object(runner.sys, 'argv', args), patch.object(runner, 'run_due', side_effect=evaluate), \
                     patch.dict(os.environ, {'SAP_RESEARCH_CANDIDATES_URL': '', 'SAP_RESEARCH_RESULTS_DUE': ''}), \
                     contextlib.redirect_stdout(io.StringIO()):
                    runner.main()
            finally:
                os.chdir(original)
            db = sqlite3.connect(database)
            old = db.execute('SELECT delivered_at FROM research_outbox WHERE message_key=?', ('old-report',)).fetchone()
            db.close()
            self.assertEqual(old, (None,), 'Unsent prior reports must remain durable')
            return calls, now

    def test_pending_report_does_not_skip_fresh_research(self):
        calls, now = self.run_fixture()
        self.assertEqual([job['job_key'] for job in calls], ['manual:' + now.isoformat()])

    def test_prior_due_manual_does_not_replace_new_requested_scan(self):
        calls, now = self.run_fixture(prior_manual=True)
        self.assertEqual(len(calls), 2)
        self.assertIn('manual:' + now.isoformat(), [job['job_key'] for job in calls])

    def test_normal_delivery_retry_does_not_create_manual_scan(self):
        calls, _ = self.run_fixture(run_now=False)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
