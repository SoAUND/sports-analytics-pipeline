import copy
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from sports_analytics_pipeline.collectors.http import HttpResponse
from sports_analytics_pipeline.experimental_calls import market_calls, collect_calls
from sports_analytics_pipeline.revised_strategy import research_card, evaluate_ticket

NOW=datetime(2026,10,1,23,tzinfo=timezone.utc)


def event():
    return {'id':'event-one','sport_key':'americanfootball_nfl','commence_time':(NOW+timedelta(hours=1)).isoformat(),
        'home_team':'Home','away_team':'Away','bookmakers':[{'key':'hardrockbet_fl','markets':[{'key':'h2h','last_update':NOW.isoformat(),
        'outcomes':[{'name':'Home','price':1.6},{'name':'Away','price':2.5}]}]}]}


class ExperimentalCallsTests(unittest.TestCase):
    def test_missing_model_and_lineup_do_not_block_call(self):
        row=market_calls([event()],as_of=NOW,accessible_books=('hardrockbet_fl',))[0]
        self.assertEqual(row['publication_decision'],'watchlist')
        self.assertEqual(row['analysis_level'],3)
        self.assertEqual(row['first_blocker'],'level_4_advanced_research')
        self.assertEqual(row['selection_name'],'Home')
        self.assertNotIn('model_probability',row)
        self.assertNotIn('ev_per_dollar',row)
        self.assertFalse(row['countable_oos'])

    def test_stale_future_or_started_prices_do_not_create_calls(self):
        for change in ('stale','future','started'):
            item=event()
            if change=='started':item['commence_time']=NOW.isoformat()
            else:item['bookmakers'][0]['markets'][0]['last_update']=(NOW+timedelta(minutes=1) if change=='future' else NOW-timedelta(minutes=61)).isoformat()
            self.assertEqual(market_calls([item],as_of=NOW),[])

    def test_wrong_participant_or_duplicate_outcome_is_rejected(self):
        for name in ('Unrelated','Home'):
            item=event();item['bookmakers'][0]['markets'][0]['outcomes'][1]['name']=name
            self.assertEqual(market_calls([item],as_of=NOW),[])

    def test_next_day_event_is_not_called_today(self):
        item=event();item['commence_time']=(NOW+timedelta(hours=8)).isoformat()
        self.assertEqual(market_calls([item],as_of=NOW),[])

    def test_reference_book_is_disclosed(self):
        row=market_calls([event()],as_of=NOW,accessible_books=('bovada',))[0]
        self.assertFalse(row['book_accessible'])
        card=research_card([row],title='LIVE RESEARCH CHECK',as_of=NOW)
        self.assertIn('No official picks today.',card)
        self.assertIn('Watchlist: 1',card)
        self.assertIn('No official picks today.',card)
        self.assertIn('Watchlist: 1',card)
        self.assertEqual(evaluate_ticket([row])['decision'],'pass')

    def test_three_way_soccer_requires_draw_quote(self):
        item=event();item['sport_key']='soccer_epl'
        self.assertEqual(market_calls([item],as_of=NOW),[])
        item['bookmakers'][0]['markets'][0]['outcomes'].append({'name':'Draw','price':4})
        self.assertEqual(len(market_calls([item],as_of=NOW)),1)

    def test_cache_preserves_quotes_and_reserves_before_network(self):
        db=sqlite3.connect(':memory:')
        class Transport:
            calls=0
            def get(self,url,**kw):
                self.calls+=1
                if url.endswith('/odds'):
                    self.assert_reserved=db.execute('SELECT COUNT(*) FROM experimental_call_requests').fetchone()[0]
                    return HttpResponse(200,json.dumps([event()]).encode(),{},NOW)
                return HttpResponse(200,b'[]',{'x-requests-remaining':'470'},NOW)
        t=Transport();kw=dict(api_key='fixture-key',books=('hardrockbet_fl',),accessible_books=('hardrockbet_fl',))
        first,_=collect_calls(db,t,as_of=NOW,**kw)
        second,_=collect_calls(db,t,as_of=NOW+timedelta(minutes=10),**kw)
        self.assertEqual(t.calls,2)
        self.assertEqual(t.assert_reserved,1)
        self.assertEqual(first[0]['price_time'],second[0]['price_time'])

    def test_prior_daily_and_monthly_requests_do_not_block_fetch(self):
        db=sqlite3.connect(':memory:')
        class Transport:
            calls=0
            def get(self,url,**kw):
                self.calls+=1
                return HttpResponse(200,b'[]',{'x-requests-remaining':'470'},NOW)
        collect_calls(db,Transport(),api_key=None,books=(),accessible_books=(),as_of=NOW)
        db.executemany('INSERT INTO experimental_call_requests VALUES (?,?)',[('2026-10-01','reserved')]*61);db.commit()
        transport=Transport()
        rows,warnings=collect_calls(db,transport,api_key='fixture-key',books=('bovada',),accessible_books=(),as_of=NOW)
        self.assertEqual(rows,[])
        self.assertEqual(transport.calls,2)
        self.assertFalse(any('budget reached' in warning for warning in warnings))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM experimental_call_requests').fetchone()[0],62)

    def test_provider_credit_reserve_is_preserved(self):
        db=sqlite3.connect(':memory:')
        class Transport:
            calls=0
            def get(self,url,**kw):
                self.calls+=1
                if url.endswith('/odds'):raise AssertionError('Reserve must block paid odds request')
                return HttpResponse(200,b'[]',{'x-requests-remaining':'50'},NOW)
        transport=Transport()
        rows,warnings=collect_calls(db,transport,api_key='fixture-key',books=('bovada',),accessible_books=(),as_of=NOW)
        self.assertEqual(rows,[])
        self.assertIn('credit reserve reached',warnings[0])
        self.assertEqual(transport.calls,1)


if __name__=='__main__':unittest.main()
