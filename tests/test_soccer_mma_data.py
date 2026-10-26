import unittest
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from sports_analytics_pipeline.collectors.soccer_mma_data import soccer_match,mma_fight,core_url,consistency,attach_soccer_mma
from sports_analytics_pipeline.research_cadence import best_available,delivery_cards
NOW=datetime(2026,10,5,16,tzinfo=timezone.utc)

def soccer():
 return {'header':{'id':'123','season':{'year':2026},'competitions':[{'date':'2026-10-01T15:00Z','status':{'type':{'completed':True},'period':2},'competitors':[{'id':'1','team':{'displayName':'A'},'score':'2','homeAway':'home'},{'id':'2','team':{'displayName':'B'},'score':'0'}]}]},'boxscore':{'teams':[{'team':{'id':'1'},'statistics':[{'name':'foulsCommitted','displayValue':'14'}]},{'team':{'id':'2'},'statistics':[{'name':'redCards','displayValue':'1'}]}]},'rosters':[{'team':{'id':'1'},'roster':[{'athlete':{'id':'99','displayName':'P'},'starter':False,'subbedIn':True,'stats':[{'name':'appearances','value':1},{'name':'totalShots','value':0}]}]}]}

def fight():
 c={'id':'123','date':'2026-10-01T15:00Z','competitors':[{'id':'1','winner':True},{'id':'2','winner':False}], 'format':{'regulation':{'clock':300,'periods':3}}}
 st={'type':{'completed':True},'clock':87,'period':2,'result':{'name':'kotko'}}
 a={'splits':{'categories':[{'stats':[{'name':'sigStrikesLanded','value':20},{'name':'takedownsLanded','value':2}]}]}}
 b={'splits':{'categories':[{'stats':[{'name':'sigStrikesLanded','value':10}]}]}}
 return c,st,a,b

class Sources(unittest.TestCase):
 def test_soccer_preserves_zero_shots_but_does_not_invent_minutes(self):
  r=soccer_match(soccer(),team_id='1',source_url='https://source',collected_at=NOW)
  self.assertEqual(r['goals_for'],2);self.assertEqual(r['statistics']['foulsCommitted'],14)
  self.assertEqual(r['players'][0]['statistics']['totalShots'],0);self.assertIsNone(r['players'][0]['minutes'])
 def test_soccer_excludes_live_extra_time_and_wrong_team(self):
  for change in ['live','extra','identity']:
   d=soccer();c=d['header']['competitions'][0]
   if change=='live':c['status']['type']['completed']=False
   if change=='extra':c['status']['period']=4
   with self.assertRaises((ValueError,StopIteration)):
    soccer_match(d,team_id='3' if change=='identity' else '1',source_url='https://source',collected_at=NOW)
 def test_fight_duration_uses_partial_final_round_and_opponent(self):
  r=mma_fight(*fight(),athlete_id='1',source_url='https://source',collected_at=NOW)
  self.assertEqual(r['duration_seconds'],387)
  self.assertAlmostEqual(r['significant_strike_differential_per_minute'],10/(387/60))
  self.assertEqual(r['statistics']['takedownsLanded'],2)
 def test_fight_live_or_wrong_statistics_excluded(self):
  c,s,a,b=fight();s['type']['completed']=False
  with self.assertRaises(ValueError):mma_fight(c,s,a,b,athlete_id='1',source_url='x',collected_at=NOW)
  s['type']['completed']=True;a['$ref']='http://sports.core.api.espn.com/v2/sports/mma/leagues/ufc/events/10/competitions/123/competitors/2/statistics'
  with self.assertRaises(ValueError):mma_fight(c,s,a,b,athlete_id='1',source_url='x',collected_at=NOW)
 def test_reference_host_and_ufc_competition_restricted(self):
  for u in ['https://attacker.test/v2/sports/mma/leagues/ufc/events/1','https://sports.core.api.espn.com/v2/sports/mma/leagues/bellator/events/1']:
   with self.assertRaises(ValueError):core_url(u)
 def test_consistency_shows_variation_and_small_sample(self):
  x=consistency([{'goals':0},{'goals':4}],['goals'])['goals']
  self.assertEqual(x['n'],2);self.assertEqual(x['stddev'],2)
 def test_attach_preserves_independent_model_and_other_sports(self):
  class C:
   def collect(self,row,as_of):return {'observations':[],'status':'missing'}
  rows=[{'sport':'epl','model':{'probability':'.6'}},{'sport':'nfl'}]
  result=attach_soccer_mma(rows,C(),as_of=NOW)
  self.assertEqual(result[0]['model'],rows[0]['model']);self.assertNotIn('sport_history',rows[0]);self.assertNotIn('sport_history',result[1])

 def test_soccer_prop_normalization_checks_player_and_does_not_infer_minutes(self):
  from sports_analytics_pipeline.collectors.soccer_mma_data import player_history
  r=soccer_match(soccer(),team_id='1',source_url='https://source',collected_at=NOW);r['competition']='eng.1'
  row={'sport':'epl','participant':'P','prop_stat':'shots','period':'full_game','sport_history':{'observations':[r]}}
  result=player_history(row)
  self.assertEqual(result['observations'][0]['value'],0)
  self.assertEqual(result['observations'][0]['opportunity_unit'],'appearances')
  self.assertEqual(player_history({**row,'espn_athlete_id':'wrong'})['status'],'missing')
 def test_mma_prop_normalization_and_wrong_period(self):
  from sports_analytics_pipeline.collectors.soccer_mma_data import player_history
  r=mma_fight(*fight(),athlete_id='1',source_url='https://source',collected_at=NOW)
  row={'sport':'mma','participant':'Fighter','prop_stat':'significant_strikes','period':'full_fight',
    'sport_history':{'observations':[r],'profiles':[{'id':'1','displayName':'Fighter'}]}}
  result=player_history(row)
  self.assertEqual(result['observations'][0]['value'],20)
  self.assertEqual(result['observations'][0]['opportunity_unit'],'fight_minutes')
  self.assertEqual(player_history({**row,'period':'round_1'})['status'],'missing')
 def test_collector_retains_one_team_when_other_identity_missing(self):
  from sports_analytics_pipeline.collectors.soccer_mma_data import SoccerMmaCollector
  class Cache:
   def get(self,url,as_of,ttl):
    if url.endswith('/teams'):
     return {'sports':[{'leagues':[{'teams':[{'team':{'id':'1','displayName':'A'}}]}]}]},NOW,False
    if '/schedule' in url:
     return {'team':{'id':'1'},'events':[{'id':'123','date':'2026-10-01T15:00Z','competitions':[{'status':{'type':{'completed':True}}}]}]},NOW,False
    return soccer(),NOW,False
  d=SoccerMmaCollector(Cache()).collect({'sport':'epl','home_name':'A','away_name':'Unknown'},as_of=NOW)
  self.assertEqual(len(d['observations']),1)
  self.assertTrue(any(a['status']=='failed' for a in d['attempts']))

class DailySelections(unittest.TestCase):
 def row(self,i=1,**kw):
  r={'event_id':str(i),'sport':'nfl','market':'moneyline','selection_name':'Team '+str(i),'home_name':'Team '+str(i),'away_name':'Other', 'start_time':(NOW+timedelta(hours=2)).isoformat(),'price_time':NOW.isoformat(),'decimal_odds':'2.1','market_status':'open','publication_decision':'watchlist','matching_book_count':3}
  return {**r,**kw}
 def test_shows_best_three_without_promoting_watchlist(self):
  rows=[self.row(i) for i in range(26)];s=delivery_cards(rows,title='test',as_of=NOW,include_details=False)[0]
  self.assertIn('BEST OF THE DAY',s);self.assertIn('Team 0 moneyline',s);self.assertEqual(s.count('Strength: '),3)
  self.assertIn('Edge: unverified',s);self.assertIn('No official picks today.',s)
  self.assertTrue(all(r['publication_decision']=='watchlist' for r in rows))
 def test_started_stale_suspended_and_known_negative_edges_excluded(self):
  rows=[self.row(1,start_time=NOW.isoformat()),self.row(2,price_time=(NOW-timedelta(hours=2)).isoformat()),self.row(3,market_status='suspended'),self.row(4,probability_edge='-.01'),self.row(5,ev_per_dollar='-.1')]
  self.assertEqual([r['event_id'] for r in best_available(rows,as_of=NOW)],['4','5'])
 def test_recorded_edge_ranked_without_using_favorite_probability(self):
  a=self.row(1,reference_probability=.9,research_score=60);b=self.row(2,probability_edge='.02',ev_per_dollar='.03',research_score=70)
  self.assertEqual(best_available([a,b],as_of=NOW)[0]['event_id'],'2')
 def test_one_selection_per_event_and_today_only(self):
  a=self.row(1);b=self.row(1,selection_name='Other');c=self.row(2,start_time=(NOW+timedelta(days=1)).isoformat())
  self.assertEqual(len(best_available([a,b,c],as_of=NOW)),1)
if __name__=='__main__':unittest.main()
