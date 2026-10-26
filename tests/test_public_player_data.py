import copy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import unittest
from urllib.parse import parse_qs, urlsplit

from sports_analytics_pipeline.collectors.http import HttpResponse
from sports_analytics_pipeline.collectors.public_player_data import (
    PublicPlayerCollector, attach_player_data, parse_espn_log, parse_mlb_log,
    parse_nflverse_stats, number, stat_spec)
from sports_analytics_pipeline.value_models import prop_distribution

NOW = datetime(2026,10,5,12,tzinfo=timezone.utc)


def row(sport='nba',stat='points'):
    return {'event_id':'upcoming','sport':sport,'market':'player_prop','period':'full_game',
            'participant':'Player One','player_name':'Player One','espn_athlete_id':'1',
            'prop_stat':stat,'direction':'over','line':15.5,
            'start_time':(NOW+timedelta(hours=4)).isoformat()}


def log(season=2026,n=8):
    result = {'filters':[{'name':'season','value':str(season)}],
              'names':['minutes','points','fouls'],'events':{},'seasonTypes':[
                  {'displayName':f'{season} Regular Season','categories':[{'type':'event','events':[]}]}]}
    for i in range(n):
        event_id=f'{season}{i}'
        result['events'][event_id]={'gameDate':(NOW-timedelta(days=10+i+(2026-season)*100)).isoformat(),
                                    'opponent':{'displayName':'Opponent'},'atVs':'vs'}
        result['seasonTypes'][0]['categories'][0]['events'].append({'eventId':event_id,'stats':['30',str(20+i),'2']})
    return result


class FakeTransport:
    def __init__(self,fail_logs=False): self.urls=[];self.fail_logs=fail_logs
    def get(self,url):
        self.urls.append(url)
        if '/gamelog?' in url:
            year=int(parse_qs(urlsplit(url).query)['season'][0])
            data={} if self.fail_logs else log(year)
        elif url.endswith('/athletes/1'):
            data={'athlete':{'id':'1','displayName':'Player One','status':{'name':'Active'},'injuries':[]},'season':{'year':2026}}
        elif url.endswith('/teams'):
            data={'sports':[{'leagues':[{'teams':[{'team':{'id':'9','displayName':'Home Team'}}]}]}]}
        elif url.endswith('/teams/9/roster'):
            data={'athletes':[{'id':'1','fullName':'Player One'}]}
        else:
            return HttpResponse(404,b'{}',{},NOW)
        return HttpResponse(200,json.dumps(data).encode(),{},NOW)


class PublicPlayerTests(unittest.TestCase):
    def setUp(self): self.db=sqlite3.connect(':memory:')
    def tearDown(self): self.db.close()

    def test_live_schema_parsing_keeps_all_statistics_and_opportunities(self):
        observations,excluded=parse_espn_log(log(),row(),season=2026,source_url='https://example.test',collected_at=NOW)
        self.assertEqual(len(observations),8)
        self.assertEqual(excluded,[])
        self.assertEqual(observations[0]['value'],20)
        self.assertEqual(observations[0]['opportunity'],30)
        self.assertEqual(observations[0]['source_statistics']['fouls'],'2')
        self.assertEqual(observations[0]['role'],'unclassified')

    def test_three_seasons_automatic_and_cached_across_collectors(self):
        transport=FakeTransport()
        collector=PublicPlayerCollector(self.db,transport)
        data=collector.collect(row(),as_of=NOW)
        self.assertEqual(data['status'],'collected')
        self.assertEqual(len(data['observations']),24)
        self.assertEqual(collector.requests,4)
        self.assertFalse(data['profile']['future_role_confirmed'])
        self.assertEqual(data['profile']['reported_status']['name'],'Active')
        second=PublicPlayerCollector(self.db,transport)
        self.assertEqual(len(second.collect(row(),as_of=NOW)['observations']),24)
        self.assertEqual(second.requests,0)

    def test_missing_player_id_resolves_exact_event_roster(self):
        candidate=row();candidate.pop('espn_athlete_id');candidate.update(home_name='Home Team',away_name='Away Team')
        collector=PublicPlayerCollector(self.db,FakeTransport())
        self.assertEqual(collector.collect(candidate,as_of=NOW)['status'],'collected')
        self.assertEqual(collector.requests,6)

    def test_ambiguous_or_wrong_identity_not_assumed(self):
        candidate=row();candidate['player_name']='Different Player'
        collector=PublicPlayerCollector(self.db,FakeTransport())
        self.assertEqual(collector.collect(candidate,as_of=NOW)['status'],'missing')

    def test_request_budget_is_explicit_and_does_not_raise_from_collection(self):
        collector=PublicPlayerCollector(self.db,FakeTransport(),max_requests=1)
        data=collector.collect(row(),as_of=NOW)
        self.assertEqual(collector.requests,1)
        self.assertEqual(data['status'],'missing')
        self.assertTrue(any('deferred' in x.get('reason','') for x in data['attempts']))

    def test_future_games_and_zero_opportunity_are_recorded_exclusions(self):
        data=log()
        data['events']['20260']['gameDate']=NOW.isoformat()
        data['seasonTypes'][0]['categories'][0]['events'][1]['stats'][0]='0'
        records,excluded=parse_espn_log(data,row(),season=2026,source_url='https://example.test',collected_at=NOW)
        self.assertEqual(len(records),6)
        self.assertEqual(len(excluded),2)

    def test_phase_and_season_not_silently_mixed(self):
        data=log()
        data['seasonTypes'][0]['displayName']='2026 Preseason'
        records,_=parse_espn_log(data,row(),season=2026,source_url='https://example.test',collected_at=NOW)
        self.assertTrue(all(r['competition_phase']=='preseason' for r in records))
        with self.assertRaisesRegex(ValueError,'different season'):
            parse_espn_log(data,row(),season=2025,source_url='x',collected_at=NOW)

    def test_unit_conversions_and_stat_type_are_explicit(self):
        self.assertAlmostEqual(number('22:30',clock=True),22.5)
        self.assertEqual(number('6.2',innings=True),20)
        with self.assertRaises(ValueError): number('6.3',innings=True)
        with self.assertRaises(ValueError): stat_spec('mlb','strikeouts')
        with self.assertRaises(ValueError): stat_spec('soccer','shots')

    def test_collection_does_not_invent_role_matchup_or_replace_existing_model(self):
        candidate=row();candidate['model']={'version':'existing'}
        output=attach_player_data([candidate],PublicPlayerCollector(self.db,FakeTransport()),as_of=NOW)[0]
        self.assertEqual(output['public_player_data']['status'],'collected')
        self.assertEqual(output['public_player_data']['distribution_status'],'blocked')
        self.assertNotIn('prop_distribution_input',output)
        self.assertEqual(output['model'],candidate['model'])
        self.assertNotIn('public_player_data',candidate)

    def test_confirmed_context_connects_real_history_to_distribution_engine(self):
        evidence={'status':'verified','source_url':'https://example.test/confirmed','source_updated_at':NOW.isoformat(),'collected_at':NOW.isoformat()}
        candidate=row()
        candidate['prop_context']={'role':'starter','competition_phase':'regular_season',
            'comparable_role':True,'role_confirmed':True,'role_evidence':evidence,'matchup_evidence':evidence,
            'matchup_multiplier':1,'systematic_uncertainty':.1,
            'historical_role_event_ids':['espn:'+str(year)+str(i) for year in (2026,2025,2024) for i in range(8)]}
        output=attach_player_data([candidate],PublicPlayerCollector(self.db,FakeTransport()),as_of=NOW)[0]
        self.assertEqual(output['public_player_data']['distribution_status'],'prepared_unvalidated')
        self.assertEqual(output['public_player_data']['opportunity_forecast_method'],'empirical_recent_20_opportunities')
        computed=prop_distribution(output,as_of=NOW)
        self.assertEqual(computed['sample_size'],24)
        self.assertFalse(computed['model']['approved'])

    def test_period_prop_cannot_use_full_game_logs(self):
        candidate=row();candidate['period']='first_quarter'
        output=PublicPlayerCollector(self.db,FakeTransport()).collect(candidate,as_of=NOW)
        self.assertEqual(output['status'],'missing')
        self.assertIn('period-specific',output['attempts'][0]['reason'])

    def test_existing_distribution_input_preserved(self):
        candidate=row();candidate['prop_distribution_input']={'original':'input'}
        output=attach_player_data([candidate],PublicPlayerCollector(self.db,FakeTransport()),as_of=NOW)[0]
        self.assertEqual(output['prop_distribution_input'],candidate['prop_distribution_input'])

    def test_mlb_official_parser_checks_identity_and_plate_appearances(self):
        candidate=row('mlb','hits');candidate['mlbam_id']='7'
        payload={'stats':[{'group':{'displayName':'hitting'},'splits':[{'player':{'id':7,'fullName':'Player One'},
            'sport':{'id':1},'date':'2026-09-01','gameType':'R','season':'2026','game':{'gamePk':99},
            'stat':{'hits':2,'plateAppearances':5}}]}]}
        records,_=parse_mlb_log(payload,candidate,source_url='https://example.test',collected_at=NOW)
        self.assertEqual(records[0]['opportunity'],5)
        self.assertEqual(records[0]['source_statistics']['hits'],2)
        payload['stats'][0]['splits'][0]['player']['id']=8
        with self.assertRaises(ValueError): parse_mlb_log(payload,candidate,source_url='x',collected_at=NOW)

    def test_nflverse_fallback_retains_penalties_epa_and_negative_yards(self):
        candidate=row('nfl','rushing_yards')
        stats='player_id,game_id,season,season_type,rushing_yards,carries,penalties,rushing_epa\nGSIS,GAME,2026,REG,-2,3,1,-1.5\n'
        games='game_id,gameday,gametime,home_score,away_score,espn\nGAME,2026-09-01,13:00,20,10,99\n'
        records,_=parse_nflverse_stats(stats,games,candidate,gsis_id='GSIS',source_url='https://example.test',collected_at=NOW)
        self.assertEqual(records[0]['event_id'],'espn:99')
        self.assertEqual(records[0]['value'],-2)
        self.assertEqual(records[0]['source_statistics']['penalties'],'1')
        self.assertEqual(records[0]['source_statistics']['rushing_epa'],'-1.5')

    def test_nflverse_fallback_is_automatically_tried_after_missing_espn_columns(self):
        class FallbackTransport(FakeTransport):
            def get(self,url):
                if url.endswith('/players.csv'):
                    text='gsis_id,display_name,espn_id\nGSIS,Player One,1\n'
                elif url.endswith('/games.csv'):
                    text='game_id,gameday,gametime,home_score,away_score,espn\n'
                    text+=''.join(f'{year}{i},{year}-09-01,13:00,20,10,{year}{i}\n' for year in (2026,2025,2024) for i in range(8))
                elif '/stats_player_week_' in url:
                    year=int(url.rsplit('_',1)[1].split('.')[0])
                    text='player_id,game_id,season,season_type,rushing_yards,carries\n'
                    text+=''.join(f'GSIS,{year}{i},{year},REG,50,10\n' for i in range(8))
                else: return super().get(url)
                self.urls.append(url)
                return HttpResponse(200,text.encode(),{},NOW)
        transport=FallbackTransport()
        collector=PublicPlayerCollector(self.db,transport)
        data=collector.collect(row('nfl','rushing_yards'),as_of=NOW)
        self.assertEqual(data['status'],'collected')
        self.assertEqual(len(data['observations']),24)
        self.assertTrue(all('nflverse' in r['source_url'] for r in data['observations']))
        self.assertTrue(any(a['status']=='failed' for a in data['attempts']))
        self.assertTrue(any(a['status']=='parsed' for a in data['attempts']))

    def test_mlb_name_resolution_and_fallback_are_automatic(self):
        class FallbackTransport(FakeTransport):
            def get(self,url):
                if 'statsapi.mlb.com' not in url: return super().get(url)
                self.urls.append(url)
                if '/people/search' in url: data={'people':[{'id':7,'fullName':'Player One'}]}
                else:
                    year=int(parse_qs(urlsplit(url).query)['season'][0])
                    data={'stats':[{'group':{'displayName':'hitting'},'splits':[
                        {'player':{'id':7,'fullName':'Player One'},'sport':{'id':1},'date':f'{year}-09-01',
                         'gameType':'R','season':str(year),'game':{'gamePk':int(str(year)+str(i))},
                         'stat':{'totalBases':2,'plateAppearances':5}} for i in range(8)]}]}
                return HttpResponse(200,json.dumps(data).encode(),{},NOW)
        candidate=row('mlb','total_bases')
        data=PublicPlayerCollector(self.db,FallbackTransport()).collect(candidate,as_of=NOW)
        self.assertEqual(data['status'],'collected')
        self.assertEqual(len(data['observations']),24)
        self.assertTrue(all('statsapi.mlb.com' in r['source_url'] for r in data['observations']))
        self.assertTrue(all(r['opportunity_unit']=='plate_appearances' for r in data['observations']))

    def test_nfl_signed_distribution_keeps_legitimate_negative_yards(self):
        from test_value_models import candidate, distribution_input
        data=candidate();data['prop_distribution_input']=distribution_input([-2,10]*4)
        data['prop_distribution_input']['stat_support']='signed_integer'
        result=prop_distribution(data,as_of=time_now_for_value_tests())
        self.assertEqual(result['mean'],4)


def time_now_for_value_tests():
    from test_value_models import NOW
    return NOW
