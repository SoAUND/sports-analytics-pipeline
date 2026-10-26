from datetime import datetime, timedelta, timezone
import io
import json
import sqlite3
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from scripts.run_multisport_forward_archive import _archived_result_due, _require_archive_mode, _sports, main

UTC = timezone.utc

class _ArchiveSettings:
    def __init__(self, mode):
        self.bot_mode = mode
        self.database_path = ':memory:'
        self.http_timeout_seconds = 1
        self.http_retry_attempts = 1
        self.market_consensus_bookmakers = ()
        self.accessible_bookmakers = ('draftkings',)

    def require_odds_api_key(self):
        return 'test-key'

class ForwardArchiveTests(unittest.TestCase):
    def test_sport_parser_is_bounded(self):
        self.assertEqual(_sports('nba,wta,nba'), ('nba','wta'))
        with self.assertRaises(Exception):
            _sports('nba,nfl')

    def test_only_pregame_snapshots_trigger_result_poll(self):
        db=sqlite3.connect(':memory:')
        db.executescript('''
          CREATE TABLE multisport_events(event_id TEXT PRIMARY KEY,sport TEXT,commence_time TEXT);
          CREATE TABLE multisport_odds_snapshots(odds_snapshot_id INTEGER PRIMARY KEY,event_id TEXT,collected_at TEXT);
          CREATE TABLE multisport_results(event_id TEXT PRIMARY KEY);
        ''')
        now=datetime(2026,8,29,20,0,tzinfo=UTC)
        start=now-timedelta(hours=4)
        db.execute("INSERT INTO multisport_events VALUES ('e','nba',?)",(start.isoformat(),))
        db.execute("INSERT INTO multisport_odds_snapshots(event_id,collected_at) VALUES ('e',?)",((start+timedelta(minutes=1)).isoformat(),))
        self.assertFalse(_archived_result_due(db,sport='nba',as_of=now))
        db.execute("INSERT INTO multisport_odds_snapshots(event_id,collected_at) VALUES ('e',?)",((start-timedelta(hours=1)).isoformat(),))
        self.assertTrue(_archived_result_due(db,sport='nba',as_of=now))

    def test_research_mode_is_accepted_and_test_only_still_works(self):
        for mode in ('research', 'test_only'):
            _require_archive_mode(mode)
            collector = type('Collector', (), {'collect_sports_catalog': lambda self: ()})()
            with patch('scripts.run_multisport_forward_archive.Settings.load', return_value=_ArchiveSettings(mode)), \
                 patch('scripts.run_multisport_forward_archive.bootstrap'), \
                 patch('scripts.run_multisport_forward_archive._status', return_value={}), \
                 patch('scripts.run_multisport_forward_archive.UrllibTransport'), \
                 patch('scripts.run_multisport_forward_archive.MultiSportOddsCollector', return_value=collector), \
                 patch('scripts.run_multisport_forward_archive.collect_sport', return_value={'stored': 0}) as collect, \
                 patch('scripts.run_multisport_forward_archive._collect_archived_results', return_value={'stored': 0}), \
                 patch('sys.argv', ['run_multisport_forward_archive.py', '--sports', 'nba']):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = main()
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload['status'], 'complete')
            self.assertFalse(payload['automatic_wagering'])
            self.assertFalse(payload['paper_evaluation'])
            self.assertFalse(payload['countable_oos'])
            collect.assert_called_once()
        with self.assertRaises(SystemExit) as rejected:
            _require_archive_mode('live')
        self.assertEqual(str(rejected.exception), 'Unsupported research mode')

if __name__ == '__main__':
    unittest.main()
