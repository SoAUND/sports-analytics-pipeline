"""Batch public structured inputs and explicitly experimental history projections.

No bookmaker probability enters a forecast. News, reported starters and season
averages never become confirmed availability or a validated advanced model.
"""
from copy import deepcopy
import csv
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from importlib.resources import files
import io
import json
from math import sqrt
from statistics import mean, pstdev
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .collectors.public_player_data import SITE, name, time, number, ROUTES
from .advanced_features import apply_advanced_features
from .public_inputs import attach_metric, espn_injury_out_count, espn_web_url, mlb_lineup_confirmed, nflverse_out_count, team_epa_per_play
from .experimental_calls import sport_route, BASE
from .collectors.soccer_mma_data import LEAGUES
from .collectors.http import CollectorHttpError

ERRORS=(CollectorHttpError, ValueError, KeyError, TypeError, ArithmeticError, OSError, StopIteration)
MISSING_MODEL_INPUTS=['Opponent-adjusted advanced features', 'Confirmed current availability and roles',
                    'Held-out calibration and forward model approval', 'Weather/venue and travel impact',
                    'Discipline and specific player-matchup effects']


def route(row):
    sport=row.get('sport')
    if sport in ROUTES:return '/'.join(ROUTES[sport])
    if sport=='cfb':return 'football/college-football'
    if sport=='basketball_nba_preseason':return 'basketball/nba'
    league=LEAGUES.get(row.get('provider_sport_key') or row.get('sport_key')) or LEAGUES.get(sport)
    return 'soccer/'+league if league else None


def inventory_candidates(inventory,*,accessible_books=()):
    """Connect retained spreads/totals; inventory is never auto-promoted to a pick."""
    output=[]
    for original in inventory:
        if original.get('market') not in {'spreads','totals'}:continue
        row=deepcopy(original);shopping=row.get('price_shopping',{})
        offers=shopping.get('quotes',[])
        own=[q for q in offers if q['book'] in accessible_books]
        offer=next(iter(own or offers),None)
        if not offer or not 1+100/300<=float(offer['decimal_odds'])<=4:continue
        provider=row['sport'];row['sport']=sport_route(provider) or provider
        row['provider_sport_key']=provider;row['period']='full_game'
        row['participant']=('over' if row['selection_name'].lower()=='over' else 'under') if row['market']=='totals' else 'home' if row['selection_name']==row['home_name'] else 'away'
        row.update(book_of_record=offer['book'],book_accessible=bool(own),
            price_scope='verified_execution' if own else 'public_reference',decimal_odds=offer['decimal_odds'],
            price_time=offer['price_time'],market_status='open',publication_decision='watchlist',confidence='Ungraded',
            candidate_id='board:'+sha256(json.dumps([row['event_id'],row['market'],row['line'],row['participant']],sort_keys=True).encode()).hexdigest()[:20],
            automatic_wagering=False,countable_oos=False,settlement_assumed=True,
            settlement_note='Provider market definition assumed; book settlement unreviewed',analysis_level=3)
        for q in row.get('quotes',[]):
            q.update(period=row['period'],participant=row['participant'],source_url=BASE+'/sports/'+provider+'/odds',settlement_assumed=True)
        output.append(row)
    return output


def team_names(team):
    return {name(team.get(k,'')) for k in ('displayName','name','shortDisplayName','abbreviation')}


def exact_event(events,row):
    matches=[]
    for event in events:
        for comp in event.get('competitions',[]):
            try:
                sides={c['homeAway']:c for c in comp['competitors']}
                if not all(name(row[k+'_name']) in team_names(sides[k]['team']) for k in ('home','away')):continue
                if abs(time(comp.get('date') or event['date'])-time(row['start_time']))>timedelta(minutes=10):continue
                matches.append((event,comp,sides))
            except ERRORS:continue
    if len(matches)!=1:raise ValueError('Current event identity/time did not match exactly once')
    return matches[0]


def score(value):
    return number(value.get('value',value.get('displayValue'))) if isinstance(value,dict) else number(value)


def completed_schedule(events,team_id,phase,as_of,url,collected):
    records=[]
    for event in events:
        try:
            comp=event['competitions'][0]
            season_type=event.get('seasonType',{}).get('type') or event.get('season',{}).get('type')
            if season_type!=phase or comp.get('status',{}).get('type',{}).get('completed') is not True:continue
            at=time(comp.get('date') or event['date'])
            if not as_of-timedelta(days=1095)<=at<=as_of-timedelta(hours=12):continue
            sides=comp['competitors'];own=next(c for c in sides if str(c['id'])==str(team_id))
            other=next(c for c in sides if str(c['id'])!=str(team_id))
            sf,sa=score(own['score']),score(other['score'])
            if sf<0 or sa<0 or not sf.is_integer() or not sa.is_integer():continue
            records.append({'event_id':'espn:'+str(event['id']),'team_id':str(team_id),
                'played_at':at.isoformat(),'competition_phase':str(phase),'season':event.get('season',{}).get('year'),
                'score_for':sf,'score_against':sa,'home_away':own.get('homeAway'),
                'opponent':other['team']['displayName'],'source_url':url,'collected_at':collected.isoformat()})
        except ERRORS:continue
    return records


class ConnectedResearchCollector:
    """One shared cached collection session for every candidate and watchlist row."""
    def __init__(self,cache):self.cache=cache;self.memo={};self.failures={}

    def get(self,url,as_of,attempts,ttl=60):
        if url in self.failures:
            attempts.append({'source_url':url,'status':'unavailable','reason':str(self.failures[url]),'reused_failure':True})
            raise self.failures[url]
        try:
            payload,collected,cached=self.cache.get(url,as_of=as_of,ttl=ttl)
            attempts.append({'source_url':url,'status':'parsed','collected_at':collected.isoformat(),'cached':cached})
            return payload,collected
        except ERRORS as exc:
            self.failures[url]=exc
            attempts.append({'source_url':url,'status':'unavailable','reason':str(exc)})
            raise

    def collect(self,row,*,as_of):
        key=(row.get('sport'),row.get('home_name'),row.get('away_name'),row.get('start_time'))
        if key in self.memo:return deepcopy(self.memo[key])
        result={'status':'missing','attempts':[],'teams':{},'snapshot':{},'statistics':{},
                'missing':list(MISSING_MODEL_INPUTS),'automatic_wagering':False}
        attempts=result['attempts'];root=route(row)
        if root:
            try:
                u=SITE+root+'/scoreboard?'+urlencode({'dates':time(row['start_time']).astimezone(ZoneInfo('America/New_York')).strftime('%Y%m%d'),'limit':1000})
                try:
                    payload,collected=self.get(u,as_of,attempts,5)
                except ERRORS:
                    payload,collected=self.get(espn_web_url(u),as_of,attempts,5)
                    u=espn_web_url(u)
                event,comp,sides=exact_event(payload.get('events',[]),row)
                phase=event.get('season',{}).get('type') or event.get('seasonType',{}).get('type')
                if phase is None:raise ValueError('Current competition phase missing; cannot mix preseason and regular season')
                result['snapshot']={'provider_event_id':str(event['id']),'competition_phase':str(phase),
                    'status':comp.get('status'),'venue':comp.get('venue'),'competitors':comp['competitors'],
                    'source_url':u,'collected_at':collected.isoformat(),'verification_status':'reported'}
                # Preserve specific injuries/rosters/box statistics instead of headline context.
                summary=SITE+root+'/summary?event='+str(event['id'])
                try:
                    data,stamp=self.get(summary,as_of,attempts,5)
                    if str(data.get('header',{}).get('id'))!=str(event['id']):raise ValueError('Summary identity mismatch')
                    result['snapshot']['details']={k:data[k] for k in ('injuries','rosters','gameInfo','predictor') if k in data and k!='predictor'}
                    result['statistics']['boxscore']={'data':data.get('boxscore',{}),'source_url':summary,'collected_at':stamp.isoformat()}
                except ERRORS:pass
                for side in ('home','away'):
                    team=sides[side]['team'];identity=str(team['id']);history=[]
                    year=event.get('season',{}).get('year') or as_of.year
                    for season in (int(year),int(year)-1,int(year)-2):
                        url=SITE+root+'/teams/'+identity+'/schedule?'+urlencode({'season':season,'seasontype':phase})
                        try:
                            data,stamp=self.get(url,as_of,attempts,60 if season==int(year) else 1440)
                            if str(data.get('team',{}).get('id'))!=identity:raise ValueError('Schedule identity mismatch')
                            history.extend(completed_schedule(data.get('events',[]),identity,phase,as_of,url,stamp))
                        except ERRORS:continue
                    result['teams'][side]={'name':row[side+'_name'],'team_id':identity,
                        'observations':deduplicate(history),'competition_phase':str(phase)}
                    url=SITE+root+'/teams/'+identity+'/statistics'
                    try:
                        data,stamp=self.get(url,as_of,attempts)
                        result['statistics'][side]={'data':data,'source_url':url,'collected_at':stamp.isoformat(),
                            'verification_status':'reported_season_statistics; not opponent-adjusted features'}
                    except ERRORS:pass
            except ERRORS:pass
        # Independent official/nflverse fallbacks are attempted even after ESPN fails.
        if min((len(t['observations']) for t in result['teams'].values()),default=0)<8:
            try:
                if row.get('sport')=='nhl':self.nhl(row,result,as_of)
                elif row.get('sport')=='mlb':self.mlb(row,result,as_of)
                elif row.get('sport')=='nfl':self.nfl(row,result,as_of)
                elif row.get('sport') in {'nba','wnba','basketball_nba_preseason'}:self.nba(row,result,as_of)
                elif row.get('provider_sport_key',row.get('sport'))=='soccer_argentina_primera_division':self.argentina(row,result,as_of)
            except ERRORS:pass
        counts={s:len(result['teams'].get(s,{}).get('observations',[])) for s in ('home','away')}
        result['history_counts']=counts
        result['status']='collected' if all(counts.values()) else 'partial' if result['snapshot'] or any(counts.values()) else 'missing'
        if any(n<8 for n in counts.values()):result['missing'].append('Eight completed same-phase observations per side')
        self._attach_public_inputs(row,result,as_of)
        self.memo[key]=deepcopy(result)
        return result

    def _attach_public_inputs(self,row,result,as_of):
        """Attach parsed availability. A failed fetch stays on the attempt log."""
        phase=(result.get('snapshot') or {}).get('competition_phase')
        if not phase:return
        sport=row.get('sport')
        if sport=='nfl':
            season=as_of.year if as_of.month>=3 else as_of.year-1
            url=f'https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{season}.csv'
            try:
                data,collected=self.get(url,as_of,result['attempts'],60)
                text=data.get('csv') if isinstance(data,dict) else None
                if text is None:return
                snapshot=(result.get('snapshot') or {}).get('data') or {}
                week=snapshot.get('week')
                game_type={'1':'PRE','2':'REG','3':'POST'}.get(str(phase), str(phase))
                for side in ('home','away'):
                    abbrev=(result.get('teams') or {}).get(side,{}).get('team_id')
                    if not abbrev or week is None:continue
                    record,outs=nflverse_out_count(text,abbrev,week=week,game_type=game_type,phase=phase,as_of=as_of,source_url=url,collected_at=collected.isoformat())
                    team=result['teams'][side]
                    team['confirmed_out']=outs
                    attach_metric(team,'confirmed_out_count',record)
                stats_url=f'https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{season}.csv'
                try:
                    stats,stats_collected=self.get(stats_url,as_of,result['attempts'],60)
                    stats_text=stats.get('csv') if isinstance(stats,dict) else None
                    if stats_text and week is not None:
                        for side in ('home','away'):
                            abbrev=(result.get('teams') or {}).get(side,{}).get('team_id')
                            if not abbrev:continue
                            record=team_epa_per_play(stats_text,abbrev,week=week,game_type=game_type,phase=phase,source_url=stats_url,collected_at=stats_collected.isoformat())
                            attach_metric(result['teams'][side],'epa_per_play',record)
                except ERRORS as exc:
                    result['missing'].append('nflverse team EPA unavailable: '+str(exc))
            except ERRORS as exc:
                result['missing'].append('nflverse injury report unavailable: '+str(exc))
        elif sport in {'nba','wnba'}:
            league=ROUTES['wnba' if sport=='wnba' else 'nba']
            url=SITE+'/'.join(league)+'/injuries'
            try:
                payload,collected=self.get(url,as_of,result['attempts'],30)
            except ERRORS:
                try:
                    url=espn_web_url(url)
                    payload,collected=self.get(url,as_of,result['attempts'],30)
                except ERRORS as exc:
                    result['missing'].append('ESPN injury list unavailable: '+str(exc))
                    return
            for side in ('home','away'):
                team=result['teams'].get(side) or {}
                if not team:continue
                record,outs=espn_injury_out_count(payload,row[side+'_name'],phase=phase,as_of=as_of,source_url=url,collected_at=collected.isoformat())
                team['confirmed_out']=outs
                attach_metric(team,'confirmed_out_count',record)
        elif sport=='mlb':
            game_id=(result.get('snapshot') or {}).get('provider_event_id')
            if not game_id:return
            url=f'https://statsapi.mlb.com/api/v1/game/{game_id}/boxscore'
            try:
                payload,collected=self.get(url,as_of,result['attempts'],15)
                confirmed=mlb_lineup_confirmed(payload)
                result['snapshot']['lineups_confirmed']=confirmed
                result['snapshot']['lineup_source']=url
                result['snapshot']['lineup_collected_at']=collected.isoformat()
                if not confirmed:result['missing'].append('MLB official batting orders not published')
            except ERRORS as exc:
                result['missing'].append('MLB boxscore unavailable: '+str(exc))

    def nhl(self,row,result,as_of):
        a=result['attempts'];data,stamp=self.get('https://api-web.nhle.com/v1/standings/now',as_of,a,1440)
        teams=data['standings'];ids={}
        for side in ('home','away'):
            matches=[t for t in teams if name(t.get('teamName',{}).get('default',''))==name(row[side+'_name'])]
            if len(matches)!=1:raise ValueError('NHL team identity unresolved')
            ids[side]=matches[0]['teamAbbrev']['default']
        year=as_of.year if as_of.month>=7 else as_of.year-1
        schedules={};schedule_sources={}
        for side in ('home','away'):
            url=f'https://api-web.nhle.com/v1/club-schedule-season/{ids[side]}/{year}{year+1}'
            data,collected=self.get(url,as_of,a)
            schedules[side]=data['games'];schedule_sources[side]={'source_url':url,'collected_at':collected.isoformat()}
        matches=[g for g in schedules['home'] if g['homeTeam']['abbrev']==ids['home'] and g['awayTeam']['abbrev']==ids['away']
                 and abs(time(g['startTimeUTC'])-time(row['start_time']))<=timedelta(minutes=10)]
        if len(matches)!=1:raise ValueError('NHL current event identity/time unresolved')
        current=matches[0];phase=current['gameType']
        result['snapshot']={'provider_event_id':str(current['id']),'competition_phase':str(phase),'data':current,
                            **schedule_sources['home'],
                            'verification_status':'official_event_only; goalie/availability unconfirmed'}
        for side in ('home','away'):
            records=[]
            for season in (year,year-1,year-2):
                url=f'https://api-web.nhle.com/v1/club-schedule-season/{ids[side]}/{season}{season+1}'
                try:
                    data,collected=self.get(url,as_of,a,60 if season==year else 1440)
                    for g in data['games']:
                        if g.get('gameState') not in {'OFF','FINAL'} or g.get('gameType')!=phase:continue
                        played=time(g['startTimeUTC'])
                        if not as_of-timedelta(days=1095)<=played<=as_of-timedelta(hours=12):continue
                        own='homeTeam' if g['homeTeam']['abbrev']==ids[side] else 'awayTeam' if g['awayTeam']['abbrev']==ids[side] else None
                        if own is None:continue
                        other='awayTeam' if own=='homeTeam' else 'homeTeam'
                        records.append({'event_id':'nhl:'+str(g['id']),'team_id':ids[side],'played_at':played.isoformat(),
                            'competition_phase':str(phase),'season':season,'score_for':number(g[own]['score']),
                            'score_against':number(g[other]['score']),'home_away':'home' if own=='homeTeam' else 'away',
                            'opponent':g[other]['abbrev'],'source_url':url,'collected_at':collected.isoformat()})
                except ERRORS:continue
            result['teams'][side]={'name':row[side+'_name'],'team_id':ids[side],'competition_phase':str(phase),'observations':deduplicate(records)}

    def mlb(self,row,result,as_of):
        a=result['attempts'];data,stamp=self.get('https://statsapi.mlb.com/api/v1/teams?sportId=1',as_of,a,1440)
        ids={}
        for side in ('home','away'):
            matches=[t for t in data['teams'] if name(t['name'])==name(row[side+'_name'])]
            if len(matches)!=1:raise ValueError('MLB team identity unresolved')
            ids[side]=matches[0]['id']
        url='https://statsapi.mlb.com/api/v1/schedule?'+urlencode({'sportId':1,'date':time(row['start_time']).astimezone(ZoneInfo('America/New_York')).date().isoformat(),'hydrate':'probablePitcher,lineups,team'})
        data,stamp=self.get(url,as_of,a,5)
        matches=[g for d in data['dates'] for g in d['games'] if g['teams']['home']['team']['id']==ids['home']
                 and g['teams']['away']['team']['id']==ids['away'] and abs(time(g['gameDate'])-time(row['start_time']))<=timedelta(minutes=10)]
        if len(matches)!=1:raise ValueError('MLB event identity/time unresolved')
        game=matches[0];phase=game['gameType']
        result['snapshot']={'data':game,'provider_event_id':str(game['gamePk']),'competition_phase':phase,
                            'source_url':url,'collected_at':stamp.isoformat(),'verification_status':'official_event; probable pitcher is not confirmed'}
        for side in ('home','away'):
            records=[]
            for season in (as_of.year,as_of.year-1,as_of.year-2):
                url='https://statsapi.mlb.com/api/v1/schedule?'+urlencode({'sportId':1,'teamId':ids[side],'season':season,'gameType':phase})
                try:
                    data,collected=self.get(url,as_of,a,60 if season==as_of.year else 1440)
                    for d in data.get('dates',[]):
                        for g in d['games']:
                            if g.get('gameType')!=phase or g['status'].get('abstractGameState')!='Final':continue
                            played=time(g['gameDate'])
                            if not as_of-timedelta(days=1095)<=played<=as_of-timedelta(hours=12):continue
                            own='home' if g['teams']['home']['team']['id']==ids[side] else 'away' if g['teams']['away']['team']['id']==ids[side] else None
                            if own is None:continue
                            other='away' if own=='home' else 'home'
                            records.append({'event_id':'mlb:'+str(g['gamePk']),'played_at':played.isoformat(),'competition_phase':phase,'season':season,
                                'team_id':str(ids[side]),'score_for':number(g['teams'][own]['score']),'score_against':number(g['teams'][other]['score']),
                                'home_away':own,'opponent':g['teams'][other]['team']['name'],'source_url':url,'collected_at':collected.isoformat()})
                except ERRORS:continue
            result['teams'][side]={'name':row[side+'_name'],'team_id':str(ids[side]),'competition_phase':phase,'observations':deduplicate(records)}

    def nba(self,row,result,as_of):
        """Official current-season schedule fallback; no fabricated prior-year data.

        ESPN is attempted by the shared collector first. This official CDN is the
        second source. A failure stays on the attempt log and is not relabeled collected.
        """
        league='wnba' if row.get('sport')=='wnba' else 'nba'
        url=f'https://cdn.{league}.com/static/json/staticData/scheduleLeagueV2.json'
        try:
            data,collected=self.get(url,as_of,result['attempts'],60)
        except ERRORS as exc:
            result['missing'].append('Official '+league.upper()+' CDN schedule unavailable: '+str(exc))
            raise
        games=[g for d in data['leagueSchedule']['gameDates'] for g in d['games']]
        def team_label(g,side):
            team=g[side+'Team'];return team.get('teamCity','')+' '+team.get('teamName','')
        current=[g for g in games if all(name(team_label(g,side))==name(row[side+'_name']) for side in ('home','away'))
                 and abs(time(g['gameDateTimeUTC'])-time(row['start_time']))<=timedelta(minutes=10)]
        if len(current)!=1:raise ValueError('Official NBA schedule event identity/time unresolved')
        game=current[0];phase=str(game['gameId'])[:3]
        if phase not in ({'101','102','104'} if league=='wnba' else {'001','002','004'}):raise ValueError('Unreviewed NBA competition phase')
        result['snapshot']={'data':game,'provider_event_id':str(game['gameId']),'competition_phase':phase,
            'source_url':url,'collected_at':collected.isoformat(),'verification_status':'official_schedule_only; current roles unconfirmed'}
        for side in ('home','away'):
            identity=game[side+'Team']['teamId'];records=[]
            for previous in games:
                try:
                    if previous.get('gameStatus')!=3 or str(previous['gameId'])[:3]!=phase:continue
                    played=time(previous['gameDateTimeUTC'])
                    if not as_of-timedelta(days=1095)<=played<=as_of-timedelta(hours=12):continue
                    own=next(s for s in ('home','away') if previous[s+'Team']['teamId']==identity)
                    other='away' if own=='home' else 'home'
                    records.append({'event_id':league+':'+str(previous['gameId']),'team_id':str(identity),'played_at':played.isoformat(),
                        'competition_phase':phase,'score_for':number(previous[own+'Team']['score']),
                        'score_against':number(previous[other+'Team']['score']),'home_away':own,
                        'opponent':team_label(previous,other),'source_url':url,'collected_at':collected.isoformat()})
                except ERRORS:continue
            result['teams'][side]={'name':row[side+'_name'],'team_id':str(identity),'competition_phase':phase,'observations':deduplicate(records)}

    def argentina(self,row,result,as_of):
        """Public league results fallback; never assert a current lineup or phase."""
        url='https://www.football-data.co.uk/new/ARG.csv'
        data,collected=self.get(url,as_of,result['attempts'],60)
        games=list(csv.DictReader(io.StringIO(data['csv'])))
        for side in ('home','away'):
            records=[]
            for g in games:
                try:
                    played=datetime.strptime(g['Date'],'%d/%m/%Y').replace(tzinfo=timezone.utc)
                    if not as_of-timedelta(days=1095)<=played<=as_of-timedelta(days=2):continue
                    own='home' if name(g['Home'])==name(row[side+'_name']) else 'away' if name(g['Away'])==name(row[side+'_name']) else None
                    if own is None:continue
                    sf,sa=number(g['HG' if own=='home' else 'AG']),number(g['AG' if own=='home' else 'HG'])
                    if sf<0 or sa<0 or not sf.is_integer() or not sa.is_integer():continue
                    records.append({'event_id':'football-data:'+g['Date']+':'+g['Home']+':'+g['Away'],
                        'played_at':played.isoformat(),'competition_phase':'unverified_league_phase',
                        'season':g.get('Season'),'score_for':sf,'score_against':sa,'home_away':own,
                        'opponent':g['Away' if own=='home' else 'Home'],'source_url':url,
                        'collected_at':collected.isoformat(),'date_kind':'calendar match date; two-day completion embargo'})
                except ERRORS:continue
            result['teams'][side]={'name':row[side+'_name'],'observations':deduplicate(records),'competition_phase':'unverified_league_phase'}
        result['missing'].append('Official current Argentine match/competition phase, xG and lineups; CSV results alone do not verify these')

    def nfl(self,row,result,as_of):
        directory=json.loads(files('sports_analytics_pipeline').joinpath('data/nfl_teams.json').read_text())
        ids={s:next(t['team_id'] for t in directory if name(t['official_name'])==name(row[s+'_name'])) for s in ('home','away')}
        url='https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv'
        data,collected=self.get(url,as_of,result['attempts'])
        games=list(csv.DictReader(io.StringIO(data['csv'])))
        matches=[g for g in games if g['home_team']==ids['home'] and g['away_team']==ids['away'] and g['gameday']==time(row['start_time']).astimezone(ZoneInfo('America/New_York')).date().isoformat()]
        if len(matches)!=1:raise ValueError('NFL current event/phase identity unresolved')
        phase=matches[0]['game_type']
        result['snapshot']={'data':matches[0],'competition_phase':phase,'source_url':url,'collected_at':collected.isoformat(),'verification_status':'schedule_only; roles/availability unconfirmed'}
        for side in ('home','away'):
            records=[]
            for g in games:
                try:
                    if g['game_type']!=phase or ids[side] not in (g['home_team'],g['away_team']):continue
                    at=time(g['gameday']+'T12:00:00Z')
                    if not as_of-timedelta(days=1095)<=at<=as_of-timedelta(days=1):continue
                    own='home' if g['home_team']==ids[side] else 'away';other='away' if own=='home' else 'home'
                    records.append({'event_id':'nflverse:'+g['game_id'],'team_id':ids[side],'played_at':at.isoformat(),'competition_phase':phase,
                        'season':g['season'],'score_for':number(g[own+'_score']),'score_against':number(g[other+'_score']),
                        'home_away':own,'opponent':g[other+'_team'],'source_url':url,'collected_at':collected.isoformat()})
                except ERRORS:continue
            result['teams'][side]={'name':row[side+'_name'],'team_id':ids[side],'competition_phase':phase,'observations':deduplicate(records)}


def deduplicate(records):
    unique={}
    for record in records:
        key=record['event_id']
        if key in unique and any(unique[key].get(k)!=record.get(k) for k in ('score_for','score_against','competition_phase')):
            raise ValueError('Conflicting completed history')
        unique[key]=record
    return sorted(unique.values(),key=lambda r:(r['played_at'],r['event_id']))[-20:]


def baseline_projection(row,history,*,as_of):
    """Experimental empirical score scenarios; scenario count is NOT sample size.

    Results are not opponent-adjusted, not a substitute for advanced features,
    and cannot confer model approval or official-pick eligibility.
    """
    if row.get('model'):return None
    if row.get('market') not in {'moneyline','spreads','totals','three_way_moneyline'}:raise ValueError('No team-history model for this market')
    sides={s:history['teams'].get(s,{}) for s in ('home','away')}
    if not all(sides[s].get('observations') for s in sides):raise ValueError('No matched completed history; see recorded source attempts and event identity checks')
    if not all(name(sides[s].get('name'))==name(row[s+'_name']) for s in sides):raise ValueError('Projection team identity mismatch')
    h,a=(sides[s].get('observations',[]) for s in ('home','away'))
    if min(len(h),len(a))<8:raise ValueError('Need eight completed same-phase games per side')
    if not history.get('snapshot',{}).get('competition_phase'):raise ValueError('Current competition phase/event remains unverified; historical results alone cannot resolve it')
    phases={r['competition_phase'] for r in h+a}
    if len(phases)!=1 or phases!={str(history['snapshot']['competition_phase'])}:raise ValueError('Mixed or mismatched competition phases')
    if any(time(r['played_at'])>=as_of-timedelta(hours=12) or time(r['collected_at'])>as_of for r in h+a):raise ValueError('Future/live history unavailable for projection')
    scenarios=[(round((x['score_for']+y['score_against'])/2),round((y['score_for']+x['score_against'])/2)) for x in h for y in a]
    soccer=row.get('sport')=='epl' or str(row.get('sport','')).startswith('soccer_')
    side=row.get('participant') or ('home' if row['selection_name']==row['home_name'] else 'away' if row['selection_name']==row['away_name'] else 'draw')
    market=row['market'];line=float(row.get('line') or 0);wins=pushes=0;outcomes={'home':0,'draw':0,'away':0}
    for hs,aws in scenarios:
        role='home' if hs>aws else 'away' if hs<aws else 'draw';outcomes[role]+=1
        if market in {'moneyline','three_way_moneyline'}:
            if soccer:wins+=int(role==side)
            elif row.get('sport') in {'nfl','cfb'}:
                wins+=int(role==side);pushes+=int(role=='draw')
            else:wins+=int(role==side)+(.5 if role=='draw' else 0)
        else:
            value=hs+aws if market=='totals' else hs-aws if side=='home' else aws-hs
            boundary=line if market=='totals' else -line
            wins+=int(value<boundary if row.get('selection_name','').lower()=='under' and market=='totals' else value>boundary)
            pushes+=int(value==boundary)
    n=len(scenarios);p=wins/n;push=pushes/n
    if push>=1:raise ValueError('All baseline scenarios push; no usable probability')
    effective=min(len(h),len(a));conditional=p/(1-push)
    z=1.96;den=1+z*z/effective
    lower=(conditional+z*z/(2*effective)-z*sqrt(conditional*(1-conditional)/effective+z*z/(4*effective*effective)))/den
    uncertainty=min(1,conditional-lower+.15)
    digest=sha256(json.dumps(h+a,sort_keys=True).encode()).hexdigest()[:16]
    projection={'probability':p,'push_probability':push,'uncertainty':uncertainty,
        'version':'public-score-scenarios-experimental-v1-'+digest,'prediction_time':as_of.isoformat(),
        'approved':False,'oos_count':0,'calibration':'unreviewed','experimental':True,
        'uncertainty_method':'Wilson sampling allowance using actual team games, plus 0.15 allowance for unmodeled matchup/availability; not calibrated',
        'sample_size_per_side':{'home':len(h),'away':len(a)},'scenario_count':n,
        'projected_home_score':mean(x[0] for x in scenarios),'projected_away_score':mean(x[1] for x in scenarios),
        'projected_margin':mean(x-y for x,y in scenarios),'projected_total':mean(x+y for x,y in scenarios),
        'outcome_probabilities':{k:v/n for k,v in outcomes.items()} if soccer else None,
        'sources':sorted({r['source_url'] for r in h+a}),
        'limitations':list(MISSING_MODEL_INPUTS)+['Score-history scenario baseline; not an opponent-adjusted advanced model', 'Rounded score scenarios; tied non-football games split 50/50; football ties require book push settlement verification'],'automatic_wagering':False}
    return apply_advanced_features(projection, history, row, as_of=as_of)


def attach_connected_research(rows,collector,*,as_of):
    output=[]
    for original in rows:
        row=deepcopy(original)
        if route(row):
            history=collector.collect(row,as_of=as_of);row['connected_research']=history
            cutoff=max(as_of,datetime.now(timezone.utc))
            row['projection_connection']={'status':'existing_model_preserved' if row.get('model') else 'blocked','missing':history['missing']}
            try:
                model=baseline_projection(row,history,as_of=cutoff)
                if model:
                    row['model']=model;row['participant']=row.get('participant') or ('home' if row['selection_name']==row['home_name'] else 'away' if row['selection_name']==row['away_name'] else 'draw')
                    for quote in row.get('quotes',[]):quote['participant']=row['participant']
                    row['projection_connection']={'status':'experimental_projection','missing':model['limitations']}
            except ERRORS as exc:row['projection_connection']['reason']=str(exc)
            row['team_consistency_observations']={s:{'n':len(t['observations']),
                'scoring_mean':mean(r['score_for'] for r in t['observations']),
                'scoring_stddev':pstdev(r['score_for'] for r in t['observations'])} for s,t in history['teams'].items() if t['observations']}
        elif row.get('sport')=='mma':
            row['projection_connection']={'status':'blocked','reason':'Insufficient matched recent UFC fights or specific style/availability evidence'}
            try:
                model=fighter_projection(row,as_of=as_of)
                if model and not row.get('model'):
                    row['model']=model;row['projection_connection']={'status':'experimental_projection','missing':model['limitations']}
            except ERRORS as exc:row['projection_connection']['reason']=str(exc)
        elif row.get('sport') in {'atp','wta'}:
            data=tennis_inputs(row,collector,as_of=as_of);row['connected_research']=data
            row['projection_connection']={'status':'blocked','reason':'Historical serve/hold observations collected where available; current surface/availability and validated forecast still required'}
        output.append(row)
    return output


def fighter_projection(row,*,as_of):
    if row.get('model'):return None
    history=row.get('sport_history',{});profiles=history.get('profiles',[]);records=history.get('observations',[])
    rates={};counts={}
    for side in ('home','away'):
        ids={str(p['id']) for p in profiles if name(p.get('displayName'))==name(row[side+'_name'])}
        if len(ids)!=1:raise ValueError('Fighter profile identity not resolved')
        eligible=[r for r in records if str(r.get('athlete_id')) in ids and r.get('competition')=='ufc'
                  and r.get('win') in (True,False) and isinstance(r.get('win'),bool)
                  and as_of-timedelta(days=1095)<=time(r['played_at'])<=as_of-timedelta(hours=6)
                  and time(r['collected_at'])<=as_of]
        if len(eligible)<8:raise ValueError('Eight same-competition recent completed fights per fighter required')
        counts[side]=len(eligible);rates[side]=mean(int(r['win']) for r in eligible)
    home=(rates['home']+1-rates['away'])/2
    selected='home' if row['selection_name']==row['home_name'] else 'away' if row['selection_name']==row['away_name'] else None
    if selected is None:raise ValueError('Fighter selected side mismatch')
    uncertainty=min(1,1.96*.5/sqrt(min(counts.values()))+.15)
    return {'probability':home if selected=='home' else 1-home,'push_probability':0,'uncertainty':uncertainty,
        'version':'ufc-recent-results-experimental-v1','prediction_time':as_of.isoformat(),
        'approved':False,'oos_count':0,'calibration':'unreviewed','experimental':True,
        'uncertainty_method':'Actual fight-count sampling allowance plus 0.15 unmodeled style/availability allowance; not calibrated',
        'sample_size_per_side':counts,'sources':sorted({r['source_url'] for r in records}),
        'limitations':['Style-adjusted striking/wrestling model','Official weigh-ins and late replacements','Forward validation'],'automatic_wagering':False}


def tennis_inputs(row,collector,*,as_of):
    """Recent main-tour serve/return observations; no assumed surface or validation."""
    result={'status':'missing','attempts':[],'players':{},'missing':['Confirmed current surface/conditions','Injury/retirement risk','Surface/style-adjusted validated model']}
    tour=row['sport'];matches=[]
    # Explicit maintained public archive. A missing/stale archive is reported, not fabricated.
    for year in (as_of.year,as_of.year-1,as_of.year-2):
        url=f'https://raw.githubusercontent.com/Aneeshers/tennis-sackmann-archive/main/{tour}/{tour}_matches_{year}.csv'
        try:
            data,collected=collector.get(url,as_of,result['attempts'],60 if year==as_of.year else 1440)
            for game in csv.DictReader(io.StringIO(data['csv'])):
                try:
                    played=datetime.strptime(game['tourney_date'],'%Y%m%d').replace(tzinfo=timezone.utc)
                    # Tournament date is not match time: embargo the entire maximum two-week tournament.
                    if not as_of-timedelta(days=1095)<=played<=as_of-timedelta(days=15):continue
                    if game['tourney_level'] not in {'A','G','M','F','P'} or any(s in game.get('score','').upper() for s in ('RET','W/O','DEF')):continue
                    matches.append((game,url,collected,played))
                except ERRORS:continue
        except ERRORS:continue
    for side in ('home','away'):
        records=[]
        for game,url,collected,played in matches:
            own='w' if name(game['winner_name'])==name(row[side+'_name']) else 'l' if name(game['loser_name'])==name(row[side+'_name']) else None
            if own is None:continue
            other='l' if own=='w' else 'w';values={}
            for field in ('ace','df','svpt','1stIn','1stWon','2ndWon','SvGms','bpSaved','bpFaced'):
                try:values[field]=number(game[own+'_'+field])
                except ERRORS:continue
            if values.get('svpt',0)>0 and all(k in values for k in ('1stWon','2ndWon')):
                values['serve_points_won_fraction']=(values['1stWon']+values['2ndWon'])/values['svpt']
            if values.get('SvGms',0)>0 and all(k in values for k in ('bpFaced','bpSaved')):
                values['hold_fraction']=1-(values['bpFaced']-values['bpSaved'])/values['SvGms']
            records.append({'event_id':game['tourney_id']+':'+game['match_num'],'played_at':played.isoformat(),'surface':game.get('surface'),
                'winner':own=='w','statistics':values,'opponent':game['loser_name'] if own=='w' else game['winner_name'],
                'source_url':url,'collected_at':collected.isoformat(),'date_kind':'tournament_start; 15-day completion embargo'})
        unique={r['event_id']:r for r in records}
        result['players'][side]={'name':row[side+'_name'],'observations':sorted(unique.values(),key=lambda r:r['played_at'])[-40:]}
    result['status']='collected' if all(p['observations'] for p in result['players'].values()) else 'missing'
    return result
