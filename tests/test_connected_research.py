import copy
from datetime import datetime, timedelta, timezone
import unittest

from sports_analytics_pipeline.connected_research import (baseline_projection, completed_schedule,
    exact_event, deduplicate, attach_connected_research, inventory_candidates,
    ConnectedResearchCollector, tennis_inputs)
from sports_analytics_pipeline.value_models import shop_public_board
from sports_analytics_pipeline.deep_analysis import soccer_outcome_gaps
from sports_analytics_pipeline.revised_strategy import evaluate_research
from test_revised_strategy import evidence_candidate

NOW=datetime(2026,10,5,22,tzinfo=timezone.utc)


def row(sport='nhl',market='moneyline',line=None):
    return dict(sport=sport,home_name='Home Team',away_name='Away Team',selection_name='Home Team',
                participant='home',market=market,line=line,start_time=(NOW+timedelta(hours=2)).isoformat())


def history(sf=4,sa=1):
    teams={}
    for side in ('home','away'):
        teams[side]={'name':side.title()+' Team','observations':[dict(event_id=f'{side}:{i}',competition_phase='2',
            played_at=(NOW-timedelta(days=i+1)).isoformat(),collected_at=NOW.isoformat(),
            source_url='https://example.com/history',score_for=sf if side=='home' else sa,
            score_against=sa if side=='home' else sf) for i in range(8)]}
    return {'teams':teams,'snapshot':{'competition_phase':'2'},'missing':[]}


class ConnectedResearchTests(unittest.TestCase):
    def test_price_does_not_change_forecast_or_claim_validation(self):
        r=row();r['decimal_odds']=2
        a=baseline_projection(r,history(),as_of=NOW)
        r['decimal_odds']=4;b=baseline_projection(r,history(),as_of=NOW)
        self.assertEqual(a,b);self.assertFalse(a['approved']);self.assertEqual(a['oos_count'],0)
        self.assertEqual(a['scenario_count'],64);self.assertEqual(a['sample_size_per_side']['home'],8)
        self.assertGreater(a['uncertainty'],.15)

    def test_future_and_mixed_phase_history_rejected(self):
        for field,value in [('played_at',NOW.isoformat()),('competition_phase','1'),('collected_at',(NOW+timedelta(seconds=1)).isoformat())]:
            h=history();h['teams']['home']['observations'][0][field]=value
            with self.assertRaises(ValueError):baseline_projection(row(),h,as_of=NOW)

    def test_soccer_draw_is_separate_outcome(self):
        p=baseline_projection(row('epl','three_way_moneyline'),history(1,1),as_of=NOW)
        self.assertEqual(p['outcome_probabilities'],dict(home=0,draw=1,away=0))
        self.assertEqual(p['probability'],0);self.assertEqual(p['push_probability'],0)

    def test_soccer_totals_are_not_mischecked_as_moneylines(self):
        r=row('epl','totals',2.5);r['selection_name']='Over';r['participant']='over'
        r['model']=baseline_projection(r,history(2,1),as_of=NOW)
        self.assertEqual(soccer_outcome_gaps(r),())

    def test_football_ties_are_pushes_not_half_wins(self):
        with self.assertRaisesRegex(ValueError,'All baseline scenarios push'):
            baseline_projection(row('nfl'),history(7,7),as_of=NOW)

    def test_integer_spread_explicit_push(self):
        with self.assertRaisesRegex(ValueError,'All baseline scenarios push'):
            baseline_projection(row(market='spreads',line=-3),history(),as_of=NOW)

    def test_conflicting_duplicate_history_fails(self):
        a=history()['teams']['home']['observations'][0];b={**a,'score_for':100}
        with self.assertRaises(ValueError):deduplicate([a,b])

    def test_exact_event_requires_time_and_both_teams(self):
        r=row();c={'date':r['start_time'],'competitors':[{'homeAway':s,'team':{'displayName':r[s+'_name']}} for s in ('home','away')]}
        e={'date':r['start_time'],'competitions':[c]}
        self.assertEqual(exact_event([e],r)[0],e)
        with self.assertRaises(ValueError):exact_event([e,e],r)
        c['date']=(NOW+timedelta(hours=3)).isoformat()
        with self.assertRaises(ValueError):exact_event([e],r)

    def test_completed_schedule_excludes_live_other_phase(self):
        c={'date':(NOW-timedelta(days=2)).isoformat(),'status':{'type':{'completed':True}},
           'competitors':[{'id':'1','score':4,'homeAway':'home','team':{'displayName':'Home Team'}},
                          {'id':'2','score':1,'homeAway':'away','team':{'displayName':'Away Team'}}]}
        e={'id':'1','season':{'type':2,'year':2026},'competitions':[c]}
        args=('1',2,NOW,'https://example.com',NOW)
        self.assertEqual(len(completed_schedule([e],*args)),1)
        c['status']['type']['completed']=False
        self.assertEqual(completed_schedule([e],*args),[])

    def test_spread_total_quotes_survive_board_inventory(self):
        event={'id':'x','sport_key':'icehockey_nhl','home_team':'Home Team','away_team':'Away Team',
               'commence_time':row()['start_time'],'bookmakers':[{'key':'pinnacle','last_update':NOW.isoformat(),
                'markets':[{'key':'totals','outcomes':[{'name':'Over','point':5.5,'price':2},{'name':'Under','point':5.5,'price':2}]}]}]}
        inventory=shop_public_board([event],as_of=NOW)
        rows=inventory_candidates(inventory,accessible_books=['pinnacle'])
        self.assertEqual(len(rows),2);self.assertTrue(rows[0]['quotes'])
        self.assertEqual(rows[0]['period'],'full_game');self.assertEqual(rows[0]['participant'],'over')
        self.assertEqual(rows[0]['quotes'][0]['participant'],'over')
        self.assertTrue(rows[0]['settlement_assumed'])

    def test_failed_primary_still_attempts_official_fallback(self):
        class Cache:
            def get(self,*args,**kwargs):raise ValueError('403 blocked')
        class Collector(ConnectedResearchCollector):
            def nhl(self,r,result,as_of):result['fallback_attempted']=True
        result=Collector(Cache()).collect(row(),as_of=NOW)
        self.assertTrue(result['fallback_attempted']);self.assertEqual(result['attempts'][0]['status'],'unavailable')

    def test_existing_model_preserved_and_input_not_mutated(self):
        class Collector:
            def collect(self,*args,**kwargs):return history()
        r=row();r['model']={'version':'existing'};original=copy.deepcopy(r)
        output=attach_connected_research([r],Collector(),as_of=NOW)[0]
        self.assertEqual(r,original);self.assertEqual(output['model'],r['model'])
        self.assertEqual(output['projection_connection']['status'],'existing_model_preserved')

    def test_experimental_forecast_audited_without_approval(self):
        r=evidence_candidate();r['model']=baseline_projection(row(),history(),as_of=NOW)
        r['model']['prediction_time']=NOW.isoformat();r['price_time']=NOW.isoformat();r['start_time']=(NOW+timedelta(hours=2)).isoformat()
        result=evaluate_research(r,as_of=NOW)
        self.assertEqual(result['full_evaluation']['qualifications']['model_eligibility']['status'],'failed')
        self.assertNotEqual(result['publication_decision'],'official_pick')
        self.assertEqual(result['full_evaluation']['qualifications_attempted'],12)

    def test_late_night_utc_event_queries_its_eastern_date(self):
        class Cache:
            def __init__(self):self.urls=[]
            def get(self,url,**kwargs):
                self.urls.append(url)
                if '/teams?' in url:return {'teams':[{'id':1,'name':'Home Team'},{'id':2,'name':'Away Team'}]},NOW,False
                raise ValueError('source unavailable')
        cache=Cache();collector=ConnectedResearchCollector(cache)
        r=row('mlb');r['start_time']='2026-10-06T00:00:00+00:00'
        result=collector.collect(r,as_of=NOW)
        self.assertTrue(any('dates=20261005' in url for url in cache.urls))
        self.assertTrue(any('date=2026-10-05' in url for url in cache.urls))

    def test_official_nba_fallback_keeps_phase_and_completed_scores(self):
        def game(i,at,status=3,prefix='001'):
            return {'gameId':prefix+str(i),'gameDateTimeUTC':at.isoformat(),'gameStatus':status,
                    'homeTeam':{'teamId':1,'teamCity':'Home','teamName':'Team','score':100},
                    'awayTeam':{'teamId':2,'teamCity':'Away','teamName':'Team','score':95}}
        games=[game(0,NOW+timedelta(hours=2),1),game(1,NOW-timedelta(days=2)),
               game(2,NOW-timedelta(days=3),prefix='002'),game(3,NOW-timedelta(hours=2))]
        class Cache:
            def get(self,url,**kwargs):
                if 'cdn.nba.com' in url:return {'leagueSchedule':{'gameDates':[{'games':games}]}},NOW,False
                raise ValueError('ESPN unavailable')
        data=ConnectedResearchCollector(Cache()).collect(row('basketball_nba_preseason'),as_of=NOW)
        self.assertEqual(data['history_counts'],{'home':1,'away':1})
        self.assertEqual(data['snapshot']['competition_phase'],'001')
        self.assertTrue(any('cdn.nba.com' in a['source_url'] and a['status']=='parsed' for a in data['attempts']))

    def test_failed_url_is_not_repeated_across_events(self):
        class Cache:
            def __init__(self):self.calls=0
            def get(self,*args,**kwargs):self.calls+=1;raise ValueError('429 rate limited')
        cache=Cache();collector=ConnectedResearchCollector(cache)
        for _ in range(2):
            with self.assertRaises(ValueError):collector.get('https://example.com',NOW,[])
        self.assertEqual(cache.calls,1)

    def test_argentine_completed_results_do_not_invent_current_phase(self):
        body='Date,Home,Away,HG,AG,Season\n'+''.join(f'{i:02d}/09/2026,Home Team,Away Team,2,1,2026\n' for i in range(1,10))
        class Cache:
            def get(self,url,**kwargs):
                if url.endswith('ARG.csv'):return {'csv':body},NOW,False
                raise ValueError('ESPN unavailable')
        r=row('soccer_argentina_primera_division');data=ConnectedResearchCollector(Cache()).collect(r,as_of=NOW)
        self.assertEqual(data['history_counts'],{'home':9,'away':9})
        with self.assertRaisesRegex(ValueError,'Current competition phase'):
            baseline_projection(r,data,as_of=NOW)

    def test_wnba_official_schedule_uses_its_own_host_and_phase(self):
        current={'gameId':'10226001','gameDateTimeUTC':row()['start_time'],'gameStatus':1,
            'homeTeam':{'teamId':1,'teamCity':'Home','teamName':'Team','score':0},
            'awayTeam':{'teamId':2,'teamCity':'Away','teamName':'Team','score':0}}
        class Cache:
            def get(self,url,**kwargs):
                if 'cdn.wnba.com' in url:return {'leagueSchedule':{'gameDates':[{'games':[current]}]}},NOW,False
                raise ValueError('ESPN unavailable')
        result=ConnectedResearchCollector(Cache()).collect(row('wnba'),as_of=NOW)
        self.assertEqual(result['snapshot']['competition_phase'],'102')
        self.assertIn('cdn.wnba.com',result['snapshot']['source_url'])

    def test_tennis_tournament_embargo_and_retirement_exclusion(self):
        fields='tourney_id,tourney_date,tourney_level,score,surface,match_num,winner_name,loser_name,w_ace,w_df,w_svpt,w_1stWon,w_2ndWon,w_SvGms,w_bpFaced,w_bpSaved,l_ace,l_df,l_svpt,l_1stWon,l_2ndWon,l_SvGms,l_bpFaced,l_bpSaved\n'
        good='x,20260901,A,6-4 6-4,Hard,1,Home Team,Away Team,4,1,40,20,10,10,4,3,1,2,40,15,10,10,4,2\n'
        future=good.replace('20260901','20261001').replace(',1,Home',',2,Home')
        retired=good.replace('6-4 6-4','RET').replace(',1,Home',',3,Home')
        class Collector:
            def get(self,*args):return {'csv':fields+good+future+retired},NOW
        r=row('atp');data=tennis_inputs(r,Collector(),as_of=NOW)
        self.assertEqual(data['status'],'collected')
        self.assertEqual(len(data['players']['home']['observations']),1)
        # Archive overlaps must not multiply observations.
        self.assertTrue(all(x['event_id']=='x:1' for x in data['players']['home']['observations']))
        self.assertEqual(data['players']['home']['observations'][0]['statistics']['hold_fraction'],.9)

if __name__=='__main__':unittest.main()
