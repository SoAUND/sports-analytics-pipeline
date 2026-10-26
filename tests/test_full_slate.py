import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from sports_analytics_pipeline.collectors.http import HttpResponse
from sports_analytics_pipeline.full_slate import collect_full_slate
from sports_analytics_pipeline.experimental_calls import market_calls
from sports_analytics_pipeline.research_cadence import prepare_daily_jobs, run_due, pending_messages

NOW=datetime(2026,10,4,4,5,tzinfo=timezone.utc)

def event(i,key='americanfootball_nfl'):
    return {'id':str(i),'sport_key':key,'commence_time':(NOW+timedelta(hours=12)).isoformat(),
        'home_team':'Home '+str(i),'away_team':'Away '+str(i),'bookmakers':[{'key':'hardrockbet_fl','markets':[
            {'key':'h2h','last_update':NOW.isoformat(),'outcomes':[{'name':'Home '+str(i),'price':1.6},{'name':'Away '+str(i),'price':2.5}]}]}]}

class Transport:
    def __init__(self,remaining=470,fail=False):self.urls=[];self.remaining=remaining;self.fail=fail
    def get(self,url,**kw):
        self.urls.append(url)
        if url.endswith('/sports'):
            data=[{'key':'americanfootball_nfl','active':True},{'key':'soccer_unknown','active':True}]
        elif 'soccer_unknown' in url:
            if self.fail:return HttpResponse(503,b'{}',{},NOW)
            data=[event(99,'soccer_unknown')]
        else:data=[event(i) for i in range(14)]
        for e in data:
            if e.get('sport_key','').startswith('soccer_'):
                e['bookmakers'][0]['markets'][0]['outcomes'].append({'name':'Draw','price':4})
        return HttpResponse(200,json.dumps(data).encode(),{'x-requests-remaining':str(self.remaining)},NOW)

class FullSlateTests(unittest.TestCase):
    def collect(self,t,preview=False):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        calls,audit=collect_full_slate(db,t,api_key='fixture',books=('hardrockbet_fl',),accessible_books=('hardrockbet_fl',),as_of=NOW,preview=preview)
        return db,calls,audit

    def test_all_nfl_events_and_unknown_leagues_discovered(self):
        _,calls,audit=self.collect(Transport())
        self.assertEqual(audit['total_events'],15)
        self.assertEqual(len(calls),15)
        self.assertIn('downstream projection and eligibility checks required',audit['events'][-1]['evaluation_status'])

    def test_preview_is_not_limited_by_credit_reserve_and_has_no_picks(self):
        t=Transport(remaining=0);db,calls,audit=self.collect(t,True)
        self.assertFalse(calls);self.assertEqual(audit['total_events'],15)
        self.assertFalse(any(url.endswith('/odds') for url in t.urls))
        prepare_daily_jobs(db,NOW);run_due(db,NOW,{'slate_events':audit['events'],'slate_coverage':audit})
        card=pending_messages(db)[0]['message']
        self.assertIn('Home 13',card);self.assertIn('Home 99',card);self.assertIn('No picks posted',card)

    def test_odds_reserve_does_not_hide_discovered_events(self):
        _,calls,audit=self.collect(Transport(remaining=52))
        self.assertFalse(calls);self.assertEqual(audit['total_events'],15)
        self.assertIn('reserve',audit['leagues'][0]['status'])

    def test_source_failure_is_explicit(self):
        _,_,audit=self.collect(Transport(fail=True),True)
        self.assertEqual(audit['leagues'][1]['http_status'],503)
        self.assertEqual(audit['leagues'][1]['status'],'Provider request failed')

    def test_no_five_call_cap(self):
        self.assertEqual(len(market_calls([event(i) for i in range(14)],as_of=NOW)),14)

    def test_preview_cache_can_be_reused_by_evaluation(self):
        db,_,_=self.collect(Transport(),True)
        calls,audit=collect_full_slate(db,Transport(),api_key='fixture',books=('hardrockbet_fl',),accessible_books=('hardrockbet_fl',),as_of=NOW+timedelta(minutes=1))
        self.assertEqual(len(calls),15)
        self.assertEqual(audit['total_events'],15)

    def test_filters_other_dates_and_started_games(self):
        items=[event(1),event(2),event(3)]
        items[1]['commence_time']=(NOW+timedelta(days=1)).isoformat()
        items[2]['commence_time']=NOW.isoformat()
        self.assertEqual(len(market_calls(items,as_of=NOW)),1)
