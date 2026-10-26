import copy
from datetime import datetime,timedelta,timezone
import json
import sqlite3
import unittest
from unittest.mock import patch
from sports_analytics_pipeline.collectors.http import HttpResponse,CollectorHttpError
from sports_analytics_pipeline.collectors.supplemental_research import SupplementAdapter,normalize_event,college_match
from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.revised_strategy import evaluate_research,research_card

NOW=datetime(2026,10,1,15,30,tzinfo=timezone.utc)
BOOKS=('hardrockbet','draftkings','fanduel','betmgm')

def event(sport='nhl'):
    return {'id':'fixture','sport_key':'icehockey_nhl' if sport=='nhl' else 'americanfootball_ncaaf',
        'home_team':'Home Team','away_team':'Away Team','commence_time':(NOW+timedelta(hours=2)).isoformat(),
        'bookmakers':[{'key':book,'last_update':NOW.isoformat(),'markets':[
            {'key':'h2h','outcomes':[{'name':'Home Team','price':2},{'name':'Away Team','price':1.9}]},
            {'key':'spreads','outcomes':[{'name':'Home Team','price':2,'point':-1.5},{'name':'Away Team','price':1.9,'point':1.5}]},
            {'key':'totals','outcomes':[{'name':'Over','price':2,'point':5.5},{'name':'Under','price':1.9,'point':5.5}]}]} for book in BOOKS]}

class Transport:
    def __init__(self,fail=False,remaining=500):self.calls=[];self.fail=fail;self.remaining=remaining
    def get(self,url,*,params=None,headers=None):
        self.calls.append((url,params,headers))
        if self.fail and url.endswith('/odds'):raise CollectorHttpError('failed')
        if url.endswith('/sports'):
            data=[{'key':'icehockey_nhl','active':True},{'key':'americanfootball_ncaaf','active':True}]
        elif url.endswith('/odds') or url.endswith('/events'):
            data=[event('cfb' if 'ncaaf' in url else 'nhl')]
        elif url.endswith('/games'):
            data=[{'id':17,'homeTeam':'Home Team','awayTeam':'Away Team','startDate':(NOW+timedelta(hours=2)).isoformat(),'startTimeTBD':False}]
        else:data=[{'team':'Home Team','statName':'rushingYards','statValue':100}]
        return HttpResponse(200,json.dumps(data).encode(),{'x-requests-remaining':str(self.remaining),'x-requests-last':'3' if url.endswith('/odds') else '0'},NOW)

class SupplementalTests(unittest.TestCase):
    def setUp(self):self.db=sqlite3.connect(':memory:');self.transport=Transport()
    def tearDown(self):self.db.close()
    def adapter(self,**kwargs):return SupplementAdapter(self.db,self.transport,odds_key='new-odds',cfbd_key='new-cfbd',books=BOOKS,**kwargs)

    def test_new_credentials_are_isolated_from_existing_keys(self):
        with patch.dict('os.environ',{'THE_ODDS_API_KEY':'old','CFBD_API_KEY':'old-cfb','NHL_CFB_ODDS_API_KEY':'new','CFBD_2_API_KEY':'new-cfb'},clear=True):
            s=Settings.load('missing.env')
            self.assertEqual((s.odds_api_key,s.cfbd_api_key,s.nhl_cfb_odds_api_key,s.cfbd_2_api_key),('old','old-cfb','new','new-cfb'))
        with patch.dict('os.environ',{'THE_ODDS_API_KEY':'old','CFBD_API_KEY':'old-cfb'},clear=True):
            s=Settings.load('missing.env');self.assertIsNone(s.nhl_cfb_odds_api_key);self.assertIsNone(s.cfbd_2_api_key)

    def test_florida_profile_maps_live_book_lists_without_changing_credentials(self):
        with patch.dict('os.environ',{'PRIMARY_BOOKMAKER_KEY':'hardrockbet_fl','ACCESSIBLE_BOOKMAKERS':'hardrockbet','MARKET_CONSENSUS_BOOKMAKERS':'hardrockbet,draftkings'},clear=True):
            settings=Settings.load('missing.env')
            self.assertEqual(settings.accessible_bookmakers,('hardrockbet_fl',))
            self.assertEqual(settings.market_consensus_bookmakers,('hardrockbet_fl','draftkings'))

    def test_manual_connection_check_never_fetches_paid_odds(self):
        checks=self.adapter().check_connections(now=NOW)
        self.assertTrue(all(v=='connection succeeded' for v in checks.values()))
        self.assertFalse(any(url.endswith('/odds') for url,_,_ in self.transport.calls))
        self.assertEqual(sum('collegefootballdata' in url for url,_,_ in self.transport.calls),1)

    def test_three_markets_exact_quotes_and_missing_model_stay_pass(self):
        rows=normalize_event(event(),sport='nhl',collected_at=NOW.isoformat(),allowed_books=BOOKS)
        self.assertEqual({r['market'] for r in rows},{'moneyline','spreads','totals'})
        self.assertTrue(all(len(r['quotes'])==4 for r in rows))
        self.assertTrue(all(r['settlement_rule']=='' for r in rows))
        results=[evaluate_research(r,as_of=NOW) for r in rows]
        self.assertTrue(all(r['publication_decision'] in {'pass','official_pick','conditional_pick','watchlist','experimental'} for r in results))
        self.assertIn('No official picks today',research_card(results,as_of=NOW))

    def test_crossed_spread_lines_do_not_count_as_matching(self):
        data=event();data['bookmakers'][1]['markets'][1]['outcomes'][0]['point']=-2.5
        data['bookmakers'][1]['markets'][1]['outcomes'][1]['point']=2.5
        rows=normalize_event(data,sport='nhl',collected_at=NOW.isoformat(),allowed_books=BOOKS)
        selected=next(r for r in rows if r['market']=='spreads' and r['line']==-1.5 and r['book_of_record']=='hardrockbet')
        self.assertEqual(len(selected['quotes']),3)

    def test_bad_prices_and_three_way_market_do_not_become_two_way(self):
        data=event();data['bookmakers']=data['bookmakers'][:1]
        data['bookmakers'][0]['markets']=[{'key':'h2h','outcomes':[{'name':'Home Team','price':2},{'name':'Away Team','price':2},{'name':'Draw','price':3}]}]
        rows=normalize_event(data,sport='nhl',collected_at=NOW.isoformat(),allowed_books=BOOKS)
        self.assertEqual(rows[0]['market'],'watch')
        data['bookmakers'][0]['markets'][0]['outcomes']=data['bookmakers'][0]['markets'][0]['outcomes'][:2]
        data['bookmakers'][0]['markets'][0]['outcomes'][0]['price']='NaN'
        self.assertEqual(normalize_event(data,sport='nhl',collected_at=NOW.isoformat(),allowed_books=BOOKS)[0]['market'],'watch')

    def test_cached_requests_preserve_original_collection_time(self):
        a=self.adapter();first=a.collect(now=NOW);second=a.collect(now=NOW+timedelta(minutes=20))
        self.assertEqual(sum(url.endswith('/odds') for url,_,_ in self.transport.calls),2)
        self.assertEqual(sum('collegefootballdata' in url for url,_,_ in self.transport.calls),2)
        self.assertEqual(first['candidates'][0]['quotes'][0]['collected_at'],second['candidates'][0]['quotes'][0]['collected_at'])
        self.assertTrue(any(r.get('college_data',{}).get('matched') for r in first['candidates']))

    def test_daily_quota_and_monthly_pacing_block_extra_reads(self):
        a=self.adapter(daily_cap=3);result=a.collect(now=NOW)
        self.assertEqual(sum(url.endswith('/odds') for url,_,_ in self.transport.calls),1)
        self.assertTrue(any('quota' in w for w in result['coverage_warnings']))
        result=a.collect(now=NOW+timedelta(hours=1))
        self.assertEqual(sum(url.endswith('/odds') for url,_,_ in self.transport.calls),1)

    def test_provider_remaining_balance_blocks_paid_calls(self):
        self.transport.remaining=52
        result=self.adapter().collect(now=NOW)
        self.assertFalse(any(url.endswith('/odds') for url,_,_ in self.transport.calls))
        self.assertTrue(result['coverage_warnings'])

    def test_overnight_uses_free_events_and_skips_cfbd(self):
        self.adapter().collect(now=NOW,slate_only=True)
        self.assertEqual(sum(url.endswith('/events') for url,_,_ in self.transport.calls),2)
        self.assertFalse(any(url.endswith('/odds') or 'collegefootballdata' in url for url,_,_ in self.transport.calls))
        self.assertEqual(self.db.execute('SELECT sum(cost) FROM supplement_requests').fetchone()[0],0)

    def test_failed_calls_keep_reserved_budget_and_never_log_keys(self):
        self.transport.fail=True
        result=self.adapter().collect(now=NOW)
        self.assertEqual(self.db.execute("SELECT sum(cost) FROM supplement_requests WHERE provider='odds'").fetchone()[0],6)
        dump=' '.join(str(r) for r in self.db.iterdump())
        self.assertNotIn('new-odds',dump);self.assertNotIn('new-cfbd',dump)
        self.assertNotIn('new-odds',json.dumps(result))

    def test_ambiguous_or_tbd_college_game_is_not_matched(self):
        row={'home_name':'Home Team','away_name':'Away Team','start_time':NOW.isoformat()}
        game={'homeTeam':'Home Team','awayTeam':'Away Team','startDate':NOW.isoformat(),'startTimeTBD':False}
        self.assertTrue(college_match(row,{'games':{'rows':[game]}})['matched'])
        self.assertFalse(college_match(row,{'games':{'rows':[game,copy.deepcopy(game)]}})['matched'])
        game['startTimeTBD']=True
        self.assertFalse(college_match(row,{'games':{'rows':[game]}})['matched'])

    def test_workflow_reads_new_names_and_collects_before_publish(self):
        from pathlib import Path
        workflow=Path('.github/workflows/revised-research.yml').read_text()
        self.assertIn('secrets.NHL_CFB_ODDS_API_KEY',workflow);self.assertIn('secrets.CFBD_2_API_KEY',workflow)
        self.assertLess(workflow.index('collect_supplemental_research.py'),workflow.index('Run due ET jobs only'))

    def test_budget_and_book_overrides_are_bounded(self):
        with self.assertRaises(ValueError):self.adapter(monthly_cap=500)
        with self.assertRaises(ValueError):SupplementAdapter(self.db,self.transport,books=tuple(str(n) for n in range(11)))
