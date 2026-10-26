"""Completed soccer match and UFC fight observations, separate from predictions."""
from copy import deepcopy
from datetime import timedelta
from statistics import mean, pstdev
from urllib.parse import urlsplit
import re
from .public_player_data import SITE, WEB, name, time, number
from .http import CollectorHttpError

LEAGUES = {'epl':'eng.1','soccer_epl':'eng.1','soccer_spain_la_liga':'esp.1',
 'soccer_germany_bundesliga':'ger.1','soccer_italy_serie_a':'ita.1',
 'soccer_france_ligue_one':'fra.1','soccer_usa_mls':'usa.1',
 'soccer_argentina_primera_division':'arg.1','soccer_spain_segunda_division':'esp.2'}
ERRORS = (CollectorHttpError, ValueError, KeyError, TypeError, ArithmeticError, OSError, StopIteration)


def soccer_league(row):
    league = LEAGUES.get(row.get('provider_sport_key') or row.get('sport_key')) or LEAGUES.get(row.get('sport'))
    explicit = row.get('espn_league')
    if explicit and re.fullmatch(r'[a-z]{2,8}\.[a-z0-9]{1,12}',str(explicit)):
        return str(explicit)
    if not league: raise ValueError('Soccer competition needs an exact ESPN league crosswalk')
    return league


def core_url(url):
    p = urlsplit(url)
    if p.hostname!='sports.core.api.espn.com' or not p.path.startswith('/v2/sports/mma/leagues/ufc/'):
        raise ValueError('Unexpected fight reference host or competition')
    return 'https://sports.core.api.espn.com'+p.path+('?' + p.query if p.query else '')


def numeric_stats(items):
    values={}
    for s in items:
        try:
            v=number(s.get('value',s.get('displayValue')))
            if v<0: continue
            values[s['name']]=v
        except (ValueError,KeyError,TypeError,ArithmeticError): continue
    return values


def soccer_match(payload, *, team_id, source_url, collected_at):
    header=payload['header']; comp=header['competitions'][0]
    if comp['status']['type'].get('completed') is not True: raise ValueError('Soccer match not completed')
    played=time(comp['date'])
    if played>collected_at-timedelta(hours=3): raise ValueError('Soccer completion too recent')
    # Extra-time and shootout scores cannot be treated as 90-minute market outcomes.
    if comp['status'].get('period',2)>2: raise ValueError('Extra-time/shootout match requires regulation score adapter')
    teams=comp['competitors']; own=next(x for x in teams if str(x['id'])==str(team_id))
    opp=next(x for x in teams if str(x['id'])!=str(team_id))
    box=next(x for x in payload['boxscore']['teams'] if str(x['team']['id'])==str(team_id))
    opponent_box=next(x for x in payload['boxscore']['teams'] if str(x['team']['id'])!=str(team_id))
    def score(x):
        s=x['score'];return number(s.get('value',s.get('displayValue')) if isinstance(s,dict) else s)
    players=[]
    for roster in payload.get('rosters',[]):
        if str(roster['team']['id'])!=str(team_id):continue
        for p in roster.get('roster',[]):
            stats=numeric_stats(p.get('stats',[]))
            if stats.get('appearances',0)<=0:continue
            players.append({'athlete_id':str(p['athlete']['id']),'name':p['athlete']['displayName'],
                'starter':p.get('starter') is True,'position':p.get('position',{}).get('name'),
                'subbed_in':p.get('subbedIn'),'subbed_out':p.get('subbedOut'),'statistics':stats,
                'minutes':stats.get('minutesPlayed')})
    return {'event_id':'espn:'+str(header['id']),'played_at':played.isoformat(),
        'competition':header.get('league',{}).get('slug'),'season':header.get('season',{}).get('year'),
        'team_id':str(team_id),'team_name':own['team']['displayName'],'opponent':opp['team']['displayName'],
        'home_away':own.get('homeAway'),'goals_for':score(own),'goals_against':score(opp),
        'statistics':numeric_stats(box['statistics']),'opponent_statistics':numeric_stats(opponent_box['statistics']),
        'players':players,'source_url':source_url,'collected_at':collected_at.isoformat()}


def mma_fight(comp,status,stats,opponent_stats, *, athlete_id,source_url,collected_at):
    if status['type'].get('completed') is not True:raise ValueError('Fight not completed')
    played=time(comp['date'])
    if played>collected_at-timedelta(hours=6):raise ValueError('Fight completion too recent')
    own=next(c for c in comp['competitors'] if str(c['id'])==str(athlete_id))
    opp=next(c for c in comp['competitors'] if str(c['id'])!=str(athlete_id))
    clock=number(status['clock']);rounds=int(status['period']);length=number(comp['format']['regulation']['clock'])
    if not 1<=rounds<=int(comp['format']['regulation']['periods']) or not 0<=clock<=length:
        raise ValueError('Fight duration/round format invalid')
    duration=(rounds-1)*length+clock
    if duration<=0:raise ValueError('No positive fight duration')
    def flatten(data,expected):
        ref=data.get('$ref','')
        if ref and ('/competitions/'+str(comp['id'])+'/competitors/'+str(expected)+'/statistics') not in ref:
            raise ValueError('Fight statistic competitor identity mismatch')
        return numeric_stats([s for c in data['splits']['categories'] for s in c['stats']])
    values=flatten(stats,athlete_id);opponent=flatten(opponent_stats,opp['id'])
    if 'sigStrikesLanded' not in values or 'sigStrikesLanded' not in opponent:
        raise ValueError('Missing significant-strike observation')
    return {'event_id':'espn:'+str(comp['id']),'played_at':played.isoformat(),'competition':'ufc',
        'athlete_id':str(athlete_id),'opponent_id':str(opp['id']),'win':own.get('winner'),
        'weight_class':comp.get('type',{}).get('text'),'scheduled_rounds':int(comp['format']['regulation']['periods']),
        'duration_seconds':duration,'finish_method':status.get('result',{}).get('name'),
        'statistics':values,'opponent_statistics':opponent,
        'significant_strike_differential_per_minute':(values['sigStrikesLanded']-opponent['sigStrikesLanded'])/(duration/60),
        'source_url':source_url,'collected_at':collected_at.isoformat()}


def consistency(records,fields):
    result={}
    for field in fields:
        xs=[r[field] for r in records if field in r]
        if xs:result[field]={'n':len(xs),'mean':mean(xs),'stddev':pstdev(xs),'min':min(xs),'max':max(xs)}
    return result


class SoccerMmaCollector:
    def __init__(self,cache, *, history_limit=12):
        self.cache=cache;self.history_limit=min(20,max(1,history_limit))
    def fetch(self,url,as_of,ttl=1440):return self.cache.get(url,as_of=as_of,ttl=ttl)
    def collect_soccer(self,row,as_of):
        league=soccer_league(row);root=SITE+'soccer/'+league
        listing,_,_=self.fetch(root+'/teams',as_of)
        teams=[item['team'] for s in listing.get('sports',[]) for l in s.get('leagues',[]) for item in l.get('teams',[])]
        output=[];attempts=[]
        for expected in (row.get('home_name'),row.get('away_name')):
            try:
                matches=[t for t in teams if name(expected) in {name(t.get(k,'')) for k in ('displayName','name','shortDisplayName')} and expected]
                if len(matches)!=1:
                    attempts.append({'source':'soccer_identity','status':'failed','reason':'Soccer event-team identity not uniquely resolved: '+str(expected)})
                    continue
                team=matches[0];url=root+'/teams/'+str(team['id'])+'/schedule'
                schedule,_,_=self.fetch(url,as_of,60)
                if str(schedule['team']['id'])!=str(team['id']):raise ValueError('Soccer schedule team mismatch')
                events=[e for e in schedule.get('events',[]) if time(e['date'])<as_of-timedelta(hours=3)
                    and e.get('competitions',[{}])[0].get('status',{}).get('type',{}).get('completed') is True]
                for e in sorted(events,key=lambda e:e['date'],reverse=True)[:self.history_limit]:
                    u=root+'/summary?event='+str(e['id'])
                    try:
                        payload,collected,cached=self.fetch(u,as_of)
                        if str(payload['header']['id'])!=str(e['id']):raise ValueError('Soccer summary identity mismatch')
                        record=soccer_match(payload,team_id=team['id'],source_url=u,collected_at=min(collected,as_of))
                        record['collected_at']=collected.isoformat()
                        record['competition']=league
                        output.append(record)
                        attempts.append({'source_url':u,'status':'parsed','cached':cached})
                    except ERRORS as exc:attempts.append({'source_url':u,'status':'failed','reason':str(exc)})
            except ERRORS as exc:
                attempts.append({'source':'identity_or_history','status':'failed','reason':str(exc)})
        return output,attempts
    def collect_mma(self,row,as_of):
        output=[];attempts=[];profiles=[]
        ids=row.get('espn_fighter_ids',{})
        scoreboard=None
        for expected in (row.get('home_name'),row.get('away_name')):
            try:
                aid=str(ids.get(expected) or (row.get('espn_athlete_id') if name(expected)==name(row.get('participant','')) else '') or '')
                if not aid:
                    if scoreboard is None:
                        u=SITE+'mma/ufc/scoreboard?dates='+as_of.strftime('%Y')+'0101-'+as_of.strftime('%Y')+'1231&limit=1000'
                        scoreboard,_,_=self.fetch(u,as_of,60)
                    found={str(c['id']) for e in scoreboard.get('events',[]) for fight in e.get('competitions',[]) for c in fight['competitors'] if name(c.get('athlete',{}).get('displayName',''))==name(expected) and expected}
                    if len(found)!=1:raise ValueError('UFC fighter identity not uniquely resolved: '+str(expected))
                    aid=next(iter(found))
                if not aid.isdigit():raise ValueError('Invalid UFC athlete ID')
                u=WEB+'mma/ufc/athletes/'+aid
                profile,collected,_=self.fetch(u,as_of,60)
                if str(profile['athlete']['id'])!=aid or name(profile['athlete']['displayName'])!=name(expected):raise ValueError('UFC fighter profile mismatch')
                profiles.append({**{k:profile['athlete'].get(k) for k in ('id','displayName','stance','displayReach','displayHeight','weightClass','displayFightingStyle')},'source_url':u,'collected_at':collected.isoformat()})
                logu='https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc/athletes/'+aid+'/eventlog?limit=100'
                log,_,_=self.fetch(logu,as_of,60)
                items=[i for i in log['events']['items'] if i.get('played') is True]
                for item in items[:self.history_limit]:
                    try:
                        u=core_url(item['competition']['$ref']);comp,collected,_=self.fetch(u,as_of)
                        if str(comp['id'])!=urlsplit(u).path.rsplit('/',1)[-1]:raise ValueError('UFC competition identity mismatch')
                        if time(comp['date'])<as_of-timedelta(days=1095):continue
                        status,_,_=self.fetch(core_url(comp['status']['$ref']),as_of)
                        own=next(c for c in comp['competitors'] if str(c['id'])==aid)
                        opp=next(c for c in comp['competitors'] if str(c['id'])!=aid)
                        stats,stats_at,_=self.fetch(core_url(own['statistics']['$ref']),as_of)
                        other,other_at,_=self.fetch(core_url(opp['statistics']['$ref']),as_of)
                        record=mma_fight(comp,status,stats,other,athlete_id=aid,source_url=u,collected_at=min(collected,as_of))
                        record['collected_at']=max(collected,stats_at,other_at).isoformat()
                        output.append(record)
                        attempts.append({'source_url':u,'status':'parsed'})
                    except ERRORS as exc:attempts.append({'source':'ufc_fight','status':'failed','reason':str(exc)})
            except ERRORS as exc:
                attempts.append({'source':'identity_or_history','status':'failed','reason':str(exc)})
        return output,attempts,profiles
    def collect(self,row, *, as_of):
        records=[];attempts=[];profiles=[]
        soccer=row.get('sport')=='epl' or str(row.get('sport','')).startswith('soccer')
        try:
            if soccer:records,attempts=self.collect_soccer(row,as_of)
            else:records,attempts,profiles=self.collect_mma(row,as_of)
        except ERRORS as exc:attempts.append({'source':'soccer' if soccer else 'ufc','status':'failed','reason':str(exc)})
        records=list({(r['event_id'],r.get('team_id',r.get('athlete_id'))):r for r in records}.values())
        return {'status':'collected' if records else 'missing','observations':records,'attempts':attempts,'profiles':profiles,
          'consistency':{str(identity):consistency([r for r in records if r.get('team_id',r.get('athlete_id'))==identity],
              ('goals_for','goals_against') if soccer else ('duration_seconds','significant_strike_differential_per_minute'))
              for identity in {r.get('team_id',r.get('athlete_id')) for r in records}},
          'missing':['xG/non-penalty xG and pressing adapter','Confirmed upcoming lineup and minutes','Opponent-adjusted predictive validation'] if soccer else
              ['Official weigh-ins and late replacements','Foul/point-deduction history','Opponent-style-adjusted predictive validation'],
          'player_consistency':{aid:{'appearances':len(ps),'start_fraction':mean(int(p['starter']) for p in ps),
              'statistics':consistency([p['statistics'] for p in ps],('totalShots','shotsOnTarget','totalGoals','foulsCommitted','yellowCards'))}
              for aid in {p['athlete_id'] for r in records for p in r.get('players',[])}
              for ps in [[p for r in records for p in r.get('players',[]) if p['athlete_id']==aid]]},
          'model_status':'observations_only_unvalidated','automatic_wagering':False}


def attach_soccer_mma(candidates,collector, *, as_of):
    rows=[]
    for original in candidates:
        row=deepcopy(original)
        if row.get('sport')=='mma' or row.get('sport')=='epl' or str(row.get('sport','')).startswith('soccer'):
            row['sport_history']=collector.collect(row,as_of=as_of)
        rows.append(row)
    return rows


def player_history(row):
    """Normalize exact observations for the existing opportunity-distribution engine."""
    data=row.get('sport_history',{});records=[];key=name(row.get('prop_stat',''))
    soccer=row.get('sport')=='epl' or str(row.get('sport','')).startswith('soccer')
    aliases={'shots':'totalShots','shotsontarget':'shotsOnTarget','goals':'totalGoals','assists':'goalAssists','fouls':'foulsCommitted','yellowcards':'yellowCards','saves':'saves'} if soccer else {
        'significantstrikes':'sigStrikesLanded','significantstrikeslanded':'sigStrikesLanded','takedowns':'takedownsLanded','takedownslanded':'takedownsLanded','knockdowns':'knockDowns','fightdurationseconds':'duration_seconds'}
    field=aliases.get(key)
    expected=row.get('player_name') or row.get('participant')
    if field and row.get('period')==('full_game' if soccer else 'full_fight'):
        for r in data.get('observations',[]):
            if soccer:
                ps=[p for p in r['players'] if name(p['name'])==name(expected) and (not row.get('espn_athlete_id') or str(p['athlete_id'])==str(row['espn_athlete_id']))]
                if len(ps)!=1 or field not in ps[0]['statistics']:continue
                p=ps[0];value=p['statistics'][field];volume=p.get('minutes') or 1
                unit='minutes' if p.get('minutes') else 'appearances'
                phase='regular_season' if r['competition'] in {'eng.1','esp.1','ger.1','ita.1','fra.1','esp.2','arg.1'} else 'unknown'
            else:
                profiles=[p for p in data.get('profiles',[]) if str(p.get('id'))==r['athlete_id'] and name(p.get('displayName'))==name(expected)]
                if len(profiles)!=1:continue
                value=r['duration_seconds'] if field=='duration_seconds' else r['statistics'].get(field)
                if value is None:continue
                volume=1 if field=='duration_seconds' else r['duration_seconds']/60
                unit='fights' if field=='duration_seconds' else 'fight_minutes';phase='ufc'
            records.append({'event_id':r['event_id'],'played_at':r['played_at'],'season':str(time(r['played_at']).year),
                'competition':row['sport'],'competition_phase':phase,'participant':row['participant'],
                'prop_stat':row['prop_stat'],'period':row['period'],'role':'unclassified','value':value,
                'opportunity':volume,'opportunity_unit':unit,'opponent':r.get('opponent',r.get('opponent_id')),
                'source_url':r['source_url'],'collected_at':r['collected_at'],'source_statistics':p['statistics'] if soccer else r.get('statistics',{})})
    return {'status':'collected' if records else 'missing','observations':records,'attempts':data.get('attempts',[]),
            'missing':[] if records else ['Exact player/period/statistic history mapping'],
            'future_opportunities_are_estimates':True,'minutes_not_inferred_from_start':True}
