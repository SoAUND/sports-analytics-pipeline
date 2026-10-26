import unittest
from datetime import datetime, timezone
from scripts.run_all_sports_autopilot import event_gate, route, requested_game

class AutopilotTests(unittest.TestCase):
    def test_started_game_cannot_be_pregame_pick(self):
        result=event_gate({'sport_key':'americanfootball_nfl','commence_time':'2026-09-10T00:20:00Z'},datetime(2026,9,10,0,21,tzinfo=timezone.utc))
        self.assertIn('pregame window closed',result)

    def test_unknown_sport_fails_closed(self):
        result=event_gate({'sport_key':'icehockey_nhl','commence_time':'2026-10-01T00:00:00Z'},datetime(2026,9,10,tzinfo=timezone.utc))
        self.assertIn('not implemented',result)

    def test_existing_routes_preserved(self):
        self.assertEqual(route('americanfootball_nfl'),'nfl')
        self.assertEqual(route('baseball_mlb'),'mlb')
        self.assertEqual(route('tennis_atp_us_open'),'atp')
        self.assertEqual(route('soccer_epl'),'epl')

    def test_scoreboard_live_state_blocks_future_timestamp(self):
        class Response:
            def json(self):
                return {'events':[{'name':'New England Patriots at Seattle Seahawks','date':'2026-09-10T01:00:00Z','competitions':[{'competitors':[{'team':{'abbreviation':'NE','displayName':'Patriots'},'score':'0'},{'team':{'abbreviation':'SEA','displayName':'Seahawks'},'score':'0'}],'status':{'type':{'state':'in','description':'In Progress'}}}]}]}
        class Transport:
            def get(self,*args,**kwargs): return Response()
        result=requested_game(Transport(),datetime(2026,9,10,tzinfo=timezone.utc))
        self.assertEqual(result['decision'],'PASS')
        self.assertFalse(result['countable_oos'])
        self.assertIsNone(result['official_pick'])

    def test_international_nfl_venues_resolve_without_home_team(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from sports_analytics_pipeline.data.db import connect, initialize_schema
        from sports_analytics_pipeline.data.seed import seed_reference_data, stadium_alias_mapping
        with TemporaryDirectory() as folder:
            db=connect(Path(folder)/'test.sqlite3')
            initialize_schema(db)
            seed_reference_data(db)
            aliases=stadium_alias_mapping(db)
            for name in ('Melbourne Cricket Ground','Tottenham Hotspur Stadium','Wembley Stadium','Stade de France','Estadio Banorte'):
                self.assertIn(name,aliases)
                self.assertIsNotNone(db.execute('SELECT stadium_id FROM stadiums WHERE stadium_id=?',(aliases[name],)).fetchone())
            db.close()

    def test_scope_excludes_unapproved_and_futures(self):
        from scripts.run_all_sports_autopilot import approved_catalog
        rows=[{'key': k} for k in ('americanfootball_nfl','baseball_mlb','basketball_nba','basketball_wnba','soccer_epl','mma_mixed_martial_arts','tennis_atp_us_open','tennis_wta_us_open','icehockey_nhl','politics_us_presidential_election','soccer_spain_la_liga')]
        rows.append({'key':'basketball_nba','has_outrights':True})
        result=approved_catalog(rows)
        self.assertEqual(len(result),8)
        self.assertTrue(all(not row.get('has_outrights') for row in result))

    def test_message_does_not_repeat_one_off_game_or_unapproved_catalog(self):
        from scripts.run_all_sports_autopilot import render_message
        text=render_message({'checked_at':'now','requested_game':{'game':'Patriots at Seahawks'},'blocked_sports':['CFL','Politics'],'nfl_evaluation':{'games_found':0,'qualification_status':'NO_QUALIFYING_PICK'}})
        self.assertIn('NO_GAMES_DUE',text)
        for unwanted in ('Patriots','Seahawks','CFL','Politics','NO_QUALIFYING_PICK','177'):
            self.assertNotIn(unwanted,text)
