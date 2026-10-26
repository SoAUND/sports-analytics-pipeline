import sqlite3
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from sports_analytics_pipeline.research_cadence import prepare_daily_jobs, prepare_manual_job, run_due, pending_messages, delivery_cards


class ManualResearchTests(unittest.TestCase):
    def test_midnight_local_schedule_and_pending_migration(self):
        for month in (10,12):
            db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
            now=datetime(2026,month,2,17,tzinfo=timezone.utc)
            prepare_daily_jobs(db,now)
            row=db.execute("SELECT * FROM research_jobs WHERE kind='overnight'").fetchone()
            from sports_analytics_pipeline.revised_strategy import ET,timestamp
            self.assertEqual(timestamp(row['due_at']).astimezone(ET).hour,0)
            midday=db.execute("SELECT due_at FROM research_jobs WHERE kind='midday'").fetchone()[0]
            self.assertEqual(timestamp(midday).astimezone(ET).hour,10)
            self.assertEqual(timestamp(midday).astimezone(ET).minute,0)
            db.execute('UPDATE research_jobs SET due_at=? WHERE job_key=?',(now.isoformat(),row['job_key']));db.commit()
            prepare_daily_jobs(db,now)
            self.assertEqual(db.execute('SELECT due_at FROM research_jobs WHERE job_key=?',(row['job_key'],)).fetchone()[0],row['due_at'])
            db.close()

    def test_delayed_midday_generates_fresh_card_once_not_missed_notice(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        now=datetime(2026,10,2,19,14,tzinfo=timezone.utc)
        prepare_daily_jobs(db,now)
        db.execute("UPDATE research_jobs SET completed_at=? WHERE kind='overnight'",(now.isoformat(),));db.commit()
        run_due(db,now,{'candidates':[]})
        message=pending_messages(db)[0]['message']
        self.assertIn('MIDDAY CHECK — DELAYED',message)
        self.assertIn('03:14 PM ET',message)
        self.assertNotIn('MIDDAY WINDOW MISSED',message)
        prepare_daily_jobs(db,now);run_due(db,now,{'candidates':[]})
        self.assertEqual(len(pending_messages(db)),1)
        db.close()

    def test_picks_are_separate_from_game_specific_details(self):
        row={'sport':'wta','home_name':'Clara Tauson','away_name':'Polina Kudermetova',
             'start_time':'2026-10-02T06:00:00Z','selection_name':'Clara Tauson',
             'market':'moneyline','decimal_odds':'1.58','period':'full_match',
             'publication_decision':'experimental_call','honesty_label':'Market-based lean',
             'book_of_record':'pinnacle','price_time':'2026-10-02T04:04:00Z','book_accessible':False}
        cards=delivery_cards([row,{**row,'selection_name':'Other selection'}],title='LIVE RESEARCH CHECK',as_of=datetime(2026,10,2,4,4,tzinfo=timezone.utc))
        self.assertEqual(len(cards),2)
        self.assertIn('No official picks today',cards[0])
        self.assertNotIn('Clara Tauson moneyline',cards[0])
        self.assertNotIn('Other selection moneyline',cards[0])
        self.assertNotIn('independent win-probability estimate',cards[0])
        self.assertNotIn('Label:',cards[0])
        self.assertNotIn('Book:',cards[0])
        self.assertEqual(len([line for line in cards[0].splitlines() if line[:1].isdigit()]),0)
        self.assertIn('RESEARCH DETAILS | WTA | Polina Kudermetova vs Clara Tauson',cards[1])
        self.assertIn('WATCHLIST — NOT PICKS',cards[1])
        self.assertIn('Selection: Other selection',cards[1])
        self.assertIn('Clara Tauson',cards[1])
        self.assertNotIn('Tip-30:',cards[1])

    def test_pick_summary_is_delivered_after_all_research(self):
        self.db.execute('INSERT INTO research_outbox VALUES(?,?,?,NULL)',('manual:test','PICKS — Experimental',self.now.isoformat()))
        self.db.execute('INSERT INTO research_outbox VALUES(?,?,?,NULL)',('manual:test:details:001','RESEARCH DETAILS | WTA | Player A vs Player B',self.now.isoformat()))
        self.db.commit()
        messages=pending_messages(self.db)
        self.assertTrue(messages[0]['message'].startswith('RESEARCH DETAILS'))
        self.assertTrue(messages[-1]['message'].startswith('PICKS'))

    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        self.now=datetime(2026,10,1,23,40,tzinfo=timezone.utc)
        prepare_daily_jobs(self.db,self.now)
        self.db.execute('UPDATE research_jobs SET completed_at=?',(self.now.isoformat(),))
        self.db.commit()

    def test_manual_review_does_not_reset_daily_jobs(self):
        before=[tuple(r) for r in self.db.execute('SELECT * FROM research_jobs')]
        prepare_manual_job(self.db,self.now)
        after=[tuple(r) for r in self.db.execute("SELECT * FROM research_jobs WHERE kind!='manual'")]
        self.assertEqual(before,after)

    def test_repeated_same_request_has_one_message(self):
        key=prepare_manual_job(self.db,self.now)
        prepare_manual_job(self.db,self.now)
        run_due(self.db,self.now,{'candidates':[]})
        run_due(self.db,self.now,{'candidates':[]})
        messages=pending_messages(self.db)
        self.assertEqual(len(messages),1)
        self.assertEqual(messages[0]['message_key'],key)
        self.assertIn('LIVE RESEARCH CHECK',messages[0]['message'])
        self.assertIn('No official picks today',messages[0]['message'])
        self.assertNotIn('MIDDAY WINDOW MISSED',messages[0]['message'])

    def test_future_daily_jobs_are_not_consumed_by_manual_check(self):
        tomorrow=datetime(2026,10,2,23,40,tzinfo=timezone.utc)
        prepare_daily_jobs(self.db,tomorrow)
        prepare_manual_job(self.db,self.now)
        run_due(self.db,self.now,{'candidates':[]})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM research_jobs WHERE kind!='manual' AND completed_at IS NULL").fetchone()[0],2)

    def test_sender_includes_required_run_url(self):
        import importlib.util
        from pathlib import Path
        path=Path(__file__).resolve().parents[1]/'scripts/run_revised_research.py'
        spec=importlib.util.spec_from_file_location('manual_research_script',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.dict('os.environ',{'GITHUB_REPOSITORY':'OWNER/sports-analytics-pipeline','GITHUB_RUN_ID':'123'}), patch.object(module.subprocess,'run') as run, patch.object(module.Path,'read_text',return_value='Short card'):
            module.send_card(Path('card.txt'))
        args=run.call_args.args[0]
        self.assertEqual(args[args.index('--run-url')+1],'https://github.com/OWNER/sports-analytics-pipeline/actions/runs/123')
        self.assertTrue(run.call_args.kwargs['check'])


if __name__=='__main__':unittest.main()
