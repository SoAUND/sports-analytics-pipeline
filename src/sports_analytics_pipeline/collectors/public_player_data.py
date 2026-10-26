"""Automatic free player game logs. Read-only; no subscriptions or API keys.

Actual observations are separate from forecasts and confirmed future roles.
ESPN's public website responses are not a guaranteed contracted API.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import csv
import io
import re
import unicodedata
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .http import CollectorHttpError

ROUTES = {'nba': ('basketball', 'nba'), 'wnba': ('basketball', 'wnba'),
          'nfl': ('football', 'nfl'), 'mlb': ('baseball', 'mlb'), 'nhl': ('hockey', 'nhl')}
WEB = 'https://site.web.api.espn.com/apis/common/v3/sports/'
SITE = 'https://site.api.espn.com/apis/site/v2/sports/'


def time(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp requires timezone')
    return parsed.astimezone(timezone.utc)


def name(value):
    text = unicodedata.normalize('NFKD', str(value)).encode('ascii', 'ignore').decode()
    return re.sub('[^a-z0-9]', '', text.lower())


def number(value, *, clock=False, innings=False):
    text = str(value).strip().replace(',', '')
    if innings:
        whole, _, remainder = text.partition('.')
        if remainder not in {'', '0', '1', '2'}:
            raise ValueError('Invalid baseball innings notation')
        return float(int(whole)*3 + int(remainder or 0))
    if clock and ':' in text:
        minute, second = text.split(':')
        if not 0 <= int(second) < 60:
            raise ValueError('Invalid playing-time clock')
        return int(minute) + int(second)/60
    value = Decimal(text)
    if not value.is_finite():
        raise ValueError('Non-finite statistic')
    return float(value)


def stat_spec(sport, statistic):
    """Explicit aliases prevent pitcher/batter strikeouts and unit confusion."""
    key = name(statistic)
    if sport in {'nba', 'wnba'}:
        fields = {'points':['points'], 'rebounds':['totalRebounds'], 'assists':['assists'],
                  'steals':['steals'], 'blocks':['blocks'], 'turnovers':['turnovers'], 'fouls':['fouls'],
                  'pointsreboundsassists':['points','totalRebounds','assists'],
                  'pointsassists':['points','assists'], 'pointsrebounds':['points','totalRebounds'],
                  'reboundsassists':['totalRebounds','assists']}
        if key in fields:
            return fields[key], 'minutes', 'minutes', True, False
    if sport == 'nfl':
        fields = {'passingyards':('passingYards','passingAttempts','pass_attempts'),
                  'passingtds':('passingTouchdowns','passingAttempts','pass_attempts'),
                  'passingtouchdowns':('passingTouchdowns','passingAttempts','pass_attempts'),
                  'rushingyards':('rushingYards','rushingAttempts','carries'),
                  'receivingyards':('receivingYards','receivingTargets','targets'),
                  'receptions':('receptions','receivingTargets','targets'),
                  'carries':('rushingAttempts',None,'games'),
                  'passingattempts':('passingAttempts',None,'games')}
        if key in fields:
            value, opportunity, unit = fields[key]
            return [value], opportunity, unit, False, False
    if sport == 'nhl':
        fields = {'shots':['shotsTotal'], 'shotsongoal':['shotsTotal'], 'goals':['goals'],
                  'assists':['assists'], 'points':['points'], 'penaltyminutes':['penaltyMinutes']}
        if key in fields:
            return fields[key], 'timeOnIcePerGame', 'ice_time_minutes', True, False
    if sport == 'mlb':
        fields = {'hits':(['hits'],'atBats','at_bats',False),
                  'homeruns':(['homeRuns'],'atBats','at_bats',False),
                  'runs':(['runs'],'atBats','at_bats',False),
                  'rbis':(['RBIs'],'atBats','at_bats',False),
                  'pitcherstrikeouts':(['strikeouts'],'inningsPitched','outs_recorded',True),
                  'outsrecorded':(['inningsPitched'],None,'games',True)}
        if key in fields:
            values, opportunity, unit, innings = fields[key]
            return values, opportunity, unit, False, innings
    raise ValueError('No verified public statistic mapping for '+sport+':'+str(statistic))


def phase(label):
    text = str(label).lower()
    if 'preseason' in text or 'spring training' in text: return 'preseason'
    if 'regular season' in text: return 'regular_season'
    if 'postseason' in text or 'playoff' in text: return 'postseason'
    return 'unknown'


def parse_espn_log(payload, row, *, season, source_url, collected_at):
    fields, opportunity, unit, clock, innings = stat_spec(row['sport'], row['prop_stat'])
    columns = payload.get('names', [])
    declared_season = next((x.get('value') for x in payload.get('filters',[]) if x.get('name')=='season'),None)
    if declared_season is not None and str(declared_season)!=str(season):
        raise ValueError('Source returned a different season')
    if not isinstance(columns, list) or not set(fields + ([opportunity] if opportunity else [])) <= set(columns):
        raise ValueError('Source lacks exact statistic/opportunity columns')
    events = payload.get('events', {})
    records, excluded, seen = [], [], set()
    for section in payload.get('seasonTypes', []):
        competition_phase = phase(section.get('displayName'))
        for category in section.get('categories', []):
            if category.get('type') != 'event': continue
            for game in category.get('events', []):
                event_id = str(game.get('eventId', ''))
                if not event_id or event_id in seen: continue
                seen.add(event_id)
                try:
                    event = events[event_id]
                    played = time(event['gameDate'])
                    # Gamelog dates are kickoff, not completion. Avoid any same-day live rows.
                    if played > collected_at - timedelta(hours=12):
                        raise ValueError('Not safely completed before collection')
                    stats = game['stats']
                    if len(stats) != len(columns): raise ValueError('Column/stat length mismatch')
                    values = dict(zip(columns, stats))
                    value = sum(number(values[f], innings=innings and f=='inningsPitched') for f in fields)
                    volume = number(values[opportunity], clock=clock, innings=innings) if opportunity else 1.0
                    if volume <= 0: raise ValueError('No positive playing opportunity; retained as exclusion')
                    records.append({'event_id':'espn:'+event_id, 'played_at':played.isoformat(),
                        'season':str(season), 'competition':row['sport'], 'competition_phase':competition_phase,
                        'participant':row['participant'], 'prop_stat':row['prop_stat'], 'period':'full_game',
                        'role':'unclassified', 'value':value, 'opportunity':volume, 'opportunity_unit':unit,
                        'opponent':event.get('opponent', {}).get('displayName'), 'home_away':event.get('atVs'),
                        'team':event.get('team', {}).get('abbreviation'),
                        'source_url':source_url, 'collected_at':collected_at.isoformat()})
                    records[-1]['source_statistics'] = values
                except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                    excluded.append({'event_id':event_id, 'reason':str(exc)})
    if not records: raise ValueError('No usable completed player game observations')
    return records, excluded


def parse_mlb_log(payload, row, *, source_url, collected_at):
    key = name(row['prop_stat'])
    pitching = key in {'pitcherstrikeouts', 'outsrecorded'}
    aliases = {'hits':'hits','homeruns':'homeRuns','runs':'runs','rbis':'rbi',
               'pitcherstrikeouts':'strikeOuts','outsrecorded':'outs','totalbases':'totalBases'}
    if key not in aliases: raise ValueError('Unsupported MLB fallback statistic')
    records, excluded = [], []
    for section in payload.get('stats', []):
        if section.get('group', {}).get('displayName') != ('pitching' if pitching else 'hitting'): continue
        for game in section.get('splits', []):
            event_id = str(game.get('game', {}).get('gamePk', ''))
            try:
                if str(game['player']['id']) != str(row['mlbam_id']): raise ValueError('MLB player ID mismatch')
                if name(game['player'].get('fullName')) != name(row.get('player_name') or row['participant']):
                    raise ValueError('MLB player name mismatch')
                if str(game.get('sport',{}).get('id')) != '1': raise ValueError('Non-MLB game excluded')
                played = time(game['date']+'T12:00:00Z')
                if played > collected_at-timedelta(hours=36): raise ValueError('Completion not yet independently established')
                stat = game['stat']
                value = number(stat[aliases[key]])
                volume = number(stat['inningsPitched'], innings=True) if pitching and key!='outsrecorded' else 1.0 if pitching else number(stat['plateAppearances'])
                if volume <= 0: raise ValueError('No positive playing opportunity')
                records.append({'event_id':'mlb:'+event_id, 'played_at':played.isoformat(), 'season':str(game['season']),
                    'competition':'mlb','competition_phase':{'R':'regular_season','F':'postseason','D':'postseason','L':'postseason','W':'postseason','S':'preseason'}.get(game['gameType'],'unknown'),
                    'participant':row['participant'],'prop_stat':row['prop_stat'],'period':'full_game','role':'unclassified',
                    'value':value,'opportunity':volume,'opportunity_unit':'outs_recorded' if pitching and key!='outsrecorded' else 'games' if pitching else 'plate_appearances',
                    'opponent':game.get('opponent',{}).get('name'), 'source_url':source_url,'collected_at':collected_at.isoformat()})
                records[-1]['source_statistics'] = stat
            except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
                excluded.append({'event_id':event_id,'reason':str(exc)})
    if not records: raise ValueError('No usable MLB game observations')
    return records, excluded


def roster_players(payload):
    for item in payload.get('athletes', []):
        if 'items' in item:
            yield from item['items']
        else:
            yield item


def parse_nflverse_stats(text, games_text, row, *, gsis_id, source_url, collected_at):
    aliases = {'passingyards':('passing_yards','attempts','pass_attempts'),
               'passingtds':('passing_tds','attempts','pass_attempts'),
               'passingtouchdowns':('passing_tds','attempts','pass_attempts'),
               'rushingyards':('rushing_yards','carries','carries'),
               'receivingyards':('receiving_yards','targets','targets'),
               'receptions':('receptions','targets','targets'),
               'carries':('carries',None,'games'),'passingattempts':('attempts',None,'games')}
    value_key, opportunity_key, unit = aliases[name(row['prop_stat'])]
    schedule = {g['game_id']:g for g in csv.DictReader(io.StringIO(games_text))}
    records, excluded = [], []
    for stat in csv.DictReader(io.StringIO(text)):
        if stat.get('player_id') != gsis_id: continue
        event_id = stat.get('game_id','')
        try:
            game = schedule[event_id]
            if game.get('home_score','')=='' or game.get('away_score','')=='': raise ValueError('Uncompleted NFL schedule game')
            played = datetime.fromisoformat(game['gameday']+'T'+(game.get('gametime') or '12:00')).replace(tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc)
            if played > collected_at-timedelta(hours=12): raise ValueError('NFL game completion too recent')
            value = number(stat[value_key])
            volume = number(stat[opportunity_key]) if opportunity_key else 1.0
            if volume <= 0: raise ValueError('No positive playing opportunity')
            records.append({'event_id':'espn:'+game['espn'] if game.get('espn') else 'nflverse:'+event_id,
                'played_at':played.isoformat(),'season':stat['season'],'competition':'nfl',
                'competition_phase':{'REG':'regular_season','POST':'postseason','PRE':'preseason'}.get(stat['season_type'],'unknown'),
                'participant':row['participant'],'prop_stat':row['prop_stat'],'period':'full_game','role':'unclassified',
                'value':value,'opportunity':volume,'opportunity_unit':unit,'opponent':stat.get('opponent_team'),
                'source_url':source_url,'collected_at':collected_at.isoformat(),'source_statistics':stat})
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            excluded.append({'event_id':event_id,'reason':str(exc)})
    if not records: raise ValueError('No usable NFL fallback player observations')
    return records, excluded


class PublicPlayerCollector:
    def __init__(self, db, transport, *, max_requests=80):
        self.db, self.transport = db, transport
        self.max_requests, self.requests = max_requests, 0
        self.memo = {}
        db.execute('CREATE TABLE IF NOT EXISTS public_player_cache (url TEXT PRIMARY KEY, collected_at TEXT, payload TEXT)')

    def get(self, url, *, as_of, ttl):
        if url in self.memo: return self.memo[url]
        cached = self.db.execute('SELECT collected_at,payload FROM public_player_cache WHERE url=?',(url,)).fetchone()
        if cached and as_of-timedelta(minutes=ttl) <= time(cached[0]) <= as_of:
            result = json.loads(cached[1]), time(cached[0]), True
        else:
            if self.requests >= self.max_requests: raise ValueError('Public request budget deferred; retry on next scheduled run')
            self.requests += 1
            response = self.transport.get(url)
            if response.status != 200: raise CollectorHttpError('Public player source unavailable',status=response.status)
            is_csv = url.endswith('.csv')
            if len(response.body) > (16 if is_csv else 4)*1024*1024: raise ValueError('Public player payload exceeds size limit')
            data = {'csv':response.body.decode('utf-8-sig')} if is_csv else response.json()
            collected = response.received_at
            if not isinstance(data, dict): raise ValueError('Public player response must be an object')
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO public_player_cache VALUES(?,?,?)',(url,collected.isoformat(),json.dumps(data)))
            result = data, collected, False
        self.memo[url] = result
        return result

    def identity(self, row, *, as_of):
        category, league = ROUTES[row['sport']]
        route = category+'/'+league
        expected_name = row.get('player_name') or row['participant']
        athlete_id = str(row.get('espn_athlete_id') or '')
        if not athlete_id:
            teams_url = SITE+route+'/teams'
            teams, _, _ = self.get(teams_url,as_of=as_of,ttl=1440)
            matches = {}
            for sport in teams.get('sports', []):
                for competition in sport.get('leagues', []):
                    for item in competition.get('teams', []):
                        team = item['team']
                        aliases = {name(team.get(k,'')) for k in ('displayName','name','shortDisplayName')} - {''}
                        if not aliases & ({name(row.get('home_name','')),name(row.get('away_name',''))} - {''}): continue
                        roster_url = SITE+route+'/teams/'+str(team['id'])+'/roster'
                        roster, _, _ = self.get(roster_url,as_of=as_of,ttl=15)
                        for player in roster_players(roster):
                            if name(expected_name) in {name(player.get('fullName','')),name(player.get('displayName',''))}:
                                matches[str(player['id'])] = player
            if len(matches) != 1: raise ValueError('Player name not uniquely resolved on event-team rosters')
            athlete_id = next(iter(matches))
        if not athlete_id.isdigit(): raise ValueError('ESPN athlete ID must be numeric')
        url = WEB+route+'/athletes/'+athlete_id
        payload, collected, cached = self.get(url,as_of=as_of,ttl=15)
        player = payload['athlete']
        if str(player.get('id')) != athlete_id or name(expected_name) not in {name(player.get('fullName','')),name(player.get('displayName',''))}:
            raise ValueError('ESPN profile player identity mismatch')
        year = int(payload.get('season',{}).get('year') or (as_of.year+1 if row['sport'] in {'nba','nhl'} and as_of.month>=9 else as_of.year))
        return athlete_id, year, {'source_url':url,'collected_at':collected.isoformat(),
            'cached':cached,'player_name':player.get('displayName'), 'reported_status':player.get('status'),
            'reported_injuries':player.get('injuries',[]), 'future_role_confirmed':False}

    def collect(self, row, *, as_of):
        if row.get('sport') not in ROUTES and row.get('sport_history'):
            from .soccer_mma_data import player_history
            return player_history(row)
        if row.get('sport') not in ROUTES:
            return {'status':'unsupported','missing':['Structured player-history adapter for this sport'],'observations':[],'attempts':[]}
        attempts, observations, exclusions, profile = [], [], [], None
        athlete_id = str(row.get('espn_athlete_id') or '')
        try:
            if row.get('period') != 'full_game': raise ValueError('Full-game history cannot verify a period-specific prop')
            athlete_id, year, profile = self.identity(row,as_of=as_of)
            route = '/'.join(ROUTES[row['sport']])
            for season in (year,year-1,year-2):
                url = WEB+route+'/athletes/'+athlete_id+'/gamelog?'+urlencode({'season':season})
                try:
                    payload, collected, cached = self.get(url,as_of=as_of,ttl=60 if season==year else 1440)
                    records, dropped = parse_espn_log(payload,row,season=season,source_url=url,collected_at=collected)
                    observations.extend(records);exclusions.extend(dropped)
                    attempts.append({'source_url':url,'status':'parsed','records':len(records),'cached':cached,'collected_at':collected.isoformat()})
                except (CollectorHttpError, KeyError, ValueError, TypeError, ArithmeticError, OSError) as exc:
                    attempts.append({'source_url':url,'status':'failed','reason':str(exc)})
        except (CollectorHttpError, KeyError, ValueError, TypeError, ArithmeticError, OSError) as exc:
            attempts.append({'source':'espn_identity','status':'failed','reason':str(exc)})
        if len(observations)<8 and row['sport']=='nfl':
            fallback = []
            try:
                players_url = 'https://github.com/nflverse/nflverse-data/releases/download/players/players.csv'
                players, _, _ = self.get(players_url,as_of=as_of,ttl=1440)
                ids = {p['gsis_id'] for p in csv.DictReader(io.StringIO(players['csv']))
                       if p.get('gsis_id') and ((athlete_id and p.get('espn_id')==athlete_id and
                       (profile is not None or name(p.get('display_name'))==name(row.get('player_name') or row['participant']))) or
                       (row.get('gsis_id') and p.get('gsis_id')==row['gsis_id'] and name(p.get('display_name'))==name(row.get('player_name') or row['participant'])))}
                if len(ids)!=1: raise ValueError('NFL ESPN/GSIS identity not uniquely resolved')
                games_url = 'https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv'
                games, _, _ = self.get(games_url,as_of=as_of,ttl=60)
                for season in (as_of.year,as_of.year-1,as_of.year-2):
                    url = f'https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv'
                    try:
                        payload,collected,cached = self.get(url,as_of=as_of,ttl=60 if season==as_of.year else 1440)
                        records,dropped = parse_nflverse_stats(payload['csv'],games['csv'],row,gsis_id=next(iter(ids)),source_url=url,collected_at=collected)
                        fallback.extend(records);exclusions.extend(dropped)
                        attempts.append({'source_url':url,'status':'parsed','records':len(records),'cached':cached,'collected_at':collected.isoformat()})
                    except (CollectorHttpError,KeyError,TypeError,ValueError,ArithmeticError,OSError) as exc:
                        attempts.append({'source_url':url,'status':'failed','reason':str(exc)})
                if len(fallback)>len(observations): observations=fallback
            except (CollectorHttpError,KeyError,TypeError,ValueError,ArithmeticError,OSError) as exc:
                attempts.append({'source':'nflverse','status':'failed','reason':str(exc)})
        if len(observations)<8 and row['sport']=='mlb':
            row = deepcopy(row)
            if not str(row.get('mlbam_id','')).isdigit():
                try:
                    search_url = 'https://statsapi.mlb.com/api/v1/people/search?'+urlencode({'names':row.get('player_name') or row['participant'],'sportIds':1})
                    people, _, _ = self.get(search_url,as_of=as_of,ttl=1440)
                    ids = {str(p['id']) for p in people.get('people',[]) if name(p.get('fullName'))==name(row.get('player_name') or row['participant'])}
                    if len(ids)!=1: raise ValueError('MLB fallback identity not uniquely resolved')
                    row['mlbam_id'] = next(iter(ids))
                except (CollectorHttpError,KeyError,TypeError,ValueError,ArithmeticError,OSError) as exc:
                    attempts.append({'source':'mlb_identity','status':'failed','reason':str(exc)})
            fallback = []
            pitching = name(row['prop_stat']) in {'pitcherstrikeouts','outsrecorded'}
            for season in (as_of.year,as_of.year-1,as_of.year-2) if str(row.get('mlbam_id','')).isdigit() else ():
                url = 'https://statsapi.mlb.com/api/v1/people/'+str(row['mlbam_id'])+'/stats?'+urlencode({'stats':'gameLog','group':'pitching' if pitching else 'hitting','season':season})
                try:
                    payload,collected,cached = self.get(url,as_of=as_of,ttl=60 if season==as_of.year else 1440)
                    records,dropped = parse_mlb_log(payload,row,source_url=url,collected_at=collected)
                    fallback.extend(records);exclusions.extend(dropped)
                    attempts.append({'source_url':url,'status':'parsed','records':len(records),'cached':cached,'collected_at':collected.isoformat()})
                except (CollectorHttpError,KeyError,TypeError,ValueError,ArithmeticError,OSError) as exc:
                    attempts.append({'source_url':url,'status':'failed','reason':str(exc)})
            if len(fallback)>len(observations): observations=fallback
        unique = {r['event_id']:r for r in observations}
        return {'status':'collected' if unique else 'missing','observations':sorted(unique.values(),key=lambda r:r['played_at']),
                'profile':profile,'attempts':attempts,'excluded_observations':exclusions,
                'missing':[] if unique else ['Usable exact-player game observations'],
                'future_opportunities_are_estimates':True,'roster_status_is_not_starting_role':True}


def attach_player_data(candidates, collector, *, as_of):
    """Always collect needed prop history; never manufacture role/matchup evidence."""
    rows = []
    for original in candidates:
        row = deepcopy(original)
        if row.get('market') != 'player_prop': rows.append(row);continue
        data = collector.collect(row,as_of=as_of)
        row['public_player_data'] = data
        if 'prop_distribution_input' in row:
            rows.append(row);continue  # Preserve independently supplied complete inputs.
        context = row.get('prop_context', {})
        records = data['observations']
        needed = ['role','competition_phase','comparable_role','role_confirmed','role_evidence',
                  'matchup_evidence','matchup_multiplier','systematic_uncertainty']
        gaps = [key for key in needed if key not in context]
        phase_records = [r for r in records if r['competition_phase']==context.get('competition_phase')]
        if len(phase_records)<8: gaps.append('Eight same-phase game observations')
        if context.get('comparable_role') is not True or context.get('role_confirmed') is not True:
            gaps.append('Confirmed comparable upcoming role')
        if gaps:
            data['distribution_status']='blocked';data['missing']=gaps
            rows.append(row);continue
        # A verified role-screening context is required; do not label all games starters.
        allowed = context.get('historical_role_event_ids')
        if not isinstance(allowed,list) or not allowed:
            data['distribution_status']='blocked';data['missing']=['Source-backed historical role event IDs']
            rows.append(row);continue
        selected = [{**r,'role':context['role'],'period':row['period']} for r in phase_records if r['event_id'] in allowed]
        if len(selected)<8:
            data['distribution_status']='blocked';data['missing']=['Eight games passing historical role screening']
            rows.append(row);continue
        units = {r['opportunity_unit'] for r in selected}
        if len(units)!=1:
            data['distribution_status']='blocked';data['missing']=['Consistent opportunity units']
            rows.append(row);continue
        # Fixed recent-20 window for an explicit empirical volume forecast, not guaranteed minutes.
        recent = selected[-20:]
        scenarios = context.get('opportunity_scenarios') or [
            {'opportunity':r['opportunity'],'weight':str(Decimal(1)/len(recent))} for r in recent]
        latest = max(time(r['collected_at']) for r in selected)
        evidence = {'status':'verified','source_url':selected[-1]['source_url'],
                    'source_updated_at':latest.isoformat(),'collected_at':latest.isoformat(),
                    'timestamp_basis':'public response observation time; publisher update time unavailable'}
        row['prop_distribution_input'] = {**{key:context[key] for key in needed},
            **{key:row[key] for key in ('event_id','participant','prop_stat','period')},
            'competition':row['sport'],'same_competition':True,'opportunity_unit':next(iter(units)),
            'history_evidence':evidence,'observations':selected,'opportunity_scenarios':scenarios,
            'stat_support':'signed_integer' if row['sport']=='nfl' and 'yards' in name(row['prop_stat']) else 'integer',
            'factor_components':context.get('factor_components',[])}
        data['distribution_status']='prepared_unvalidated'
        data['opportunity_forecast_method']='supplied_scenarios' if context.get('opportunity_scenarios') else 'empirical_recent_20_opportunities'
        rows.append(row)
    return rows
