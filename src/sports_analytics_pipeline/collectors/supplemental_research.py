"""Read-only NHL/CFB data adapter with isolated credentials and durable budgets.

Market observations do not substitute for a model, availability research or
reviewed settlement rules. They enter the publisher as explicit Pass/Watch rows.
"""
from __future__ import annotations

import calendar
from datetime import timedelta
import hashlib
import json
import math
import sqlite3
from .http import CollectorHttpError
from ..revised_strategy import ET, decimal, timestamp

SPORT_KEYS = {'nhl': 'icehockey_nhl', 'cfb': 'americanfootball_ncaaf'}
MARKETS = {'h2h': 'moneyline', 'spreads': 'spreads', 'totals': 'totals'}


class SupplementAdapter:
    def __init__(self, db, transport, *, odds_key=None, cfbd_key=None, books=(),
                 daily_cap=12, monthly_cap=400, cfbd_monthly_cap=900):
        if not 1 <= daily_cap <= 16 or not 1 <= monthly_cap <= 450 or not 1 <= cfbd_monthly_cap <= 950:
            raise ValueError('Supplement budgets exceed free-plan reserves')
        if len(set(books)) > 10:
            raise ValueError('Supplement supports at most ten approved books per request')
        self.db, self.transport = db, transport
        self.odds_key, self.cfbd_key = odds_key, cfbd_key
        self.books = tuple(dict.fromkeys(books))
        self.daily_cap, self.monthly_cap, self.cfbd_cap = daily_cap, monthly_cap, cfbd_monthly_cap
        db.executescript('''
          CREATE TABLE IF NOT EXISTS supplement_cache (
            cache_key TEXT PRIMARY KEY, collected_at TEXT NOT NULL, payload_json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS supplement_requests (
            request_id INTEGER PRIMARY KEY, provider TEXT NOT NULL, request_day TEXT NOT NULL,
            cost INTEGER NOT NULL, status TEXT NOT NULL);
        ''')

    def _request(self, provider, endpoint, params, *, now, ttl_minutes, estimated_cost=1,list_field=None):
        public_params = {k:v for k,v in params.items() if k != 'apiKey'}
        identity = provider + endpoint + json.dumps(public_params, sort_keys=True)+str(list_field)
        key = hashlib.sha256(identity.encode()).hexdigest()
        cached = self.db.execute('SELECT collected_at,payload_json FROM supplement_cache WHERE cache_key=?', (key,)).fetchone()
        if cached and now-timedelta(minutes=ttl_minutes) <= timestamp(cached[0]) <= now:
            return json.loads(cached[1]), cached[0], True
        day = now.astimezone(ET).date().isoformat()
        cap = self.monthly_cap if provider == 'odds' else 4500 if provider=='nhl' else self.cfbd_cap
        # Spread the monthly pool through the calendar month, never borrow future days.
        month_days = calendar.monthrange(int(day[:4]), int(day[5:7]))[1]
        allowance = cap * int(day[8:10]) // month_days
        with self.db:
            total = self.db.execute('SELECT COALESCE(sum(cost),0) FROM supplement_requests WHERE provider=? AND request_day LIKE ?', (provider, day[:7]+'%')).fetchone()[0]
            used = self.db.execute('SELECT COALESCE(sum(cost),0) FROM supplement_requests WHERE provider=? AND request_day=?', (provider,day)).fetchone()[0]
            if estimated_cost and (total+estimated_cost > allowance or used+estimated_cost > (self.daily_cap if provider=='odds' else 120 if provider=='nhl' else 10)):
                raise CollectorHttpError('Supplement quota reserve reached')
            request_id = self.db.execute('INSERT INTO supplement_requests(provider,request_day,cost,status) VALUES(?,?,?,?)',
                (provider,day,estimated_cost,'reserved')).lastrowid
        headers = {'Authorization':'Bearer '+self.cfbd_key} if provider=='cfbd' else None
        try:
            response = self.transport.get(endpoint,params=params,headers=headers)
            if response.status != 200:
                raise CollectorHttpError('Supplement provider request failed',status=response.status)
            payload = response.json()
            if list_field and isinstance(payload,dict):payload=payload.get(list_field)
            if not isinstance(payload,list):
                raise CollectorHttpError('Supplement expected an array response')
            actual = max(estimated_cost,int(response.headers.get('x-requests-last',estimated_cost))) if provider=='odds' else estimated_cost
            stamp = response.received_at.isoformat()
            with self.db:
                self.db.execute('UPDATE supplement_requests SET cost=?,status=? WHERE request_id=?',(actual,'succeeded',request_id))
                self.db.execute('INSERT OR REPLACE INTO supplement_cache VALUES(?,?,?)',(key,stamp,json.dumps(payload)))
            return payload, stamp, False
        except (CollectorHttpError,ValueError,TypeError) as exc:
            with self.db:
                self.db.execute('UPDATE supplement_requests SET status=? WHERE request_id=?',('failed',request_id))
            raise CollectorHttpError('Supplement provider failed; request conservatively counted',status=getattr(exc,'status',None)) from None

    def training_history(self, *, now, sports):
        """Free NHL public results and bounded CFBD history, cached by season."""
        history=[];diagnostics=[]
        local=now.astimezone(ET);year=local.year-(1 if local.month==1 else 0)
        if 'cfb' in sports and self.cfbd_key:
            try:
                teams,_,_=self._request('cfbd','https://api.collegefootballdata.com/teams',{},now=now,ttl_minutes=10080)
                names={t['school']:t['school']+' '+t['mascot'] for t in teams if t.get('school') and t.get('mascot')}
                for season in (year-1,year):
                    rows,_,_=self._request('cfbd','https://api.collegefootballdata.com/games',{'year':str(season)},now=now,ttl_minutes=1440 if season==year else 10080)
                    for g in rows:
                        try:
                            if g.get('completed') is not True or g.get('seasonType')!='regular' or g.get('startTimeTBD'):continue
                            start=timestamp(g['startDate'])
                            if start+timedelta(days=7)>now:continue
                            history.append({'game_id':'cfbd:'+str(g['id']),'sport':'cfb',
                                'home_name':names[g['homeTeam']],'away_name':names[g['awayTeam']],
                                'start_time':start.isoformat(),'final_available_at':(start+timedelta(days=7)).isoformat(),
                                'home_score':g['homePoints'],'away_score':g['awayPoints'],'regular_season':True,'final':True,
                                'availability_method':'retrospective seven-day embargo; not a point-in-time archive'})
                        except (KeyError,TypeError,ValueError):continue
            except CollectorHttpError:diagnostics.append('CFBD historical data unavailable')
        if 'nhl' in sports:
            try:
                teams,_,_=self._request('nhl','https://api-web.nhle.com/v1/standings/now',{},now=now,ttl_minutes=1440,list_field='standings')
                first=local.year if local.month>=7 else local.year-1
                unique={}
                for team in teams:
                    abbreviation=team.get('teamAbbrev',{}).get('default','')
                    if not abbreviation.isalpha() or len(abbreviation)!=3:continue
                    for season in (first-2,first-1,first):
                        url=f'https://api-web.nhle.com/v1/club-schedule-season/{abbreviation}/{season}{season+1}'
                        rows,_,_=self._request('nhl',url,{},now=now,ttl_minutes=1440 if season==first else 10080,list_field='games')
                        for g in rows:
                            try:
                                if g.get('gameType')!=2 or g.get('gameState') not in {'OFF','FINAL'}:continue
                                start=timestamp(g['startTimeUTC'])
                                if start+timedelta(days=7)>now:continue
                                name=lambda t:t['placeName']['default']+' '+t['commonName']['default']
                                row={'game_id':'nhl:'+str(g['id']),'sport':'nhl','home_name':name(g['homeTeam']),'away_name':name(g['awayTeam']),
                                    'start_time':start.isoformat(),'final_available_at':(start+timedelta(days=7)).isoformat(),
                                    'home_score':g['homeTeam']['score'],'away_score':g['awayTeam']['score'],'regular_season':True,'final':True,
                                    'availability_method':'retrospective seven-day embargo; not a point-in-time archive'}
                                if row['game_id'] in unique and unique[row['game_id']]!=row:raise ValueError('Conflicting NHL results')
                                unique[row['game_id']]=row
                            except (KeyError,TypeError):continue
                history.extend(unique.values())
            except (CollectorHttpError,ValueError):diagnostics.append('NHL historical data incomplete or unavailable')
        return history,diagnostics

    def check_connections(self, *, now):
        """Explicit manual check: zero odds credits and at most one CFBD call."""
        checks={}
        if self.odds_key:
            try:
                response=self.transport.get('https://api.the-odds-api.com/v4/sports',params={'apiKey':self.odds_key})
                if response.status != 200 or not isinstance(response.json(),list):raise CollectorHttpError('Invalid odds connection response')
                checks['NHL_CFB_ODDS_API_KEY']='connection succeeded'
            except CollectorHttpError as exc:checks['NHL_CFB_ODDS_API_KEY']='connection failed'+(f' (HTTP {exc.status})' if exc.status else '')
        else:checks['NHL_CFB_ODDS_API_KEY']='missing'
        if self.cfbd_key:
            try:
                year=now.astimezone(ET).year-(1 if now.astimezone(ET).month==1 else 0)
                self._request('cfbd','https://api.collegefootballdata.com/games',{'year':str(year)},now=now,ttl_minutes=1440)
                checks['CFBD_2_API_KEY']='connection succeeded'
            except CollectorHttpError as exc:checks['CFBD_2_API_KEY']='connection failed'+(f' (HTTP {exc.status})' if exc.status else '')
        else:checks['CFBD_2_API_KEY']='missing'
        return checks

    def collect(self, *, now, sports=('nhl','cfb'), slate_only=False):
        observations, warnings, college = [], [], {}
        if any(s not in SPORT_KEYS for s in sports):
            raise ValueError('Unsupported supplemental sport')
        if self.odds_key and sports:
            # This endpoint is free and returns the provider's actual remaining balance.
            response = self.transport.get('https://api.the-odds-api.com/v4/sports',params={'apiKey':self.odds_key})
            if response.status != 200:
                raise CollectorHttpError('Supplement odds authentication failed',status=response.status)
            active = {x['key'] for x in response.json() if x.get('active')}
            remaining = int(response.headers.get('x-requests-remaining',0))
            if not slate_only and not self.books:
                warnings.append('Approved accessible/consensus book list missing; paid odds requests skipped')
            for sport in sports:
                sport_key=SPORT_KEYS[sport]
                if sport_key not in active:
                    warnings.append(sport+': not currently active at provider');continue
                if not slate_only and (not self.books or remaining < 53):
                    warnings.append(sport+': odds unavailable or quota reserve');continue
                endpoint='https://api.the-odds-api.com/v4/sports/'+sport_key+('/events' if slate_only else '/odds')
                params={'apiKey':self.odds_key,'dateFormat':'iso'}
                if not slate_only:
                    params.update(bookmakers=','.join(self.books),markets='h2h,spreads,totals',oddsFormat='decimal')
                try:
                    rows,stamp,cached=self._request('odds',endpoint,params,now=now,ttl_minutes=45 if not slate_only else 180,
                        estimated_cost=0 if slate_only else 3)
                    if not cached and not slate_only:remaining-=3
                    for event in rows:
                        if event.get('sport_key') not in (None,sport_key):continue
                        try:
                            start=timestamp(event['commence_time'])
                            if not now < start <= now+timedelta(hours=24):continue
                            observations.extend(normalize_event(event,sport=sport,collected_at=stamp,allowed_books=self.books))
                        except (KeyError,ValueError,TypeError):
                            warnings.append(sport+': malformed event skipped')
                except CollectorHttpError as exc:
                    warnings.append(sport+': '+str(exc))
        elif sports:
            warnings.append('NHL_CFB_ODDS_API_KEY missing')
        if self.cfbd_key and 'cfb' in sports and not slate_only:
            year=now.astimezone(ET).year-(1 if now.astimezone(ET).month==1 else 0)
            for path in ('games','stats/season'):
                try:
                    rows,stamp,_=self._request('cfbd','https://api.collegefootballdata.com/'+path,{'year':str(year)},now=now,ttl_minutes=1440)
                    college[path]={'collected_at':stamp,'source_url':'https://api.collegefootballdata.com/'+path,'rows':rows}
                except CollectorHttpError as exc:
                    warnings.append('cfbd '+path+': '+str(exc))
        elif 'cfb' in sports and not slate_only:
            warnings.append('CFBD_2_API_KEY missing')
        for row in observations:
            if row['sport']=='cfb':
                row['college_data']=college_match(row,college)
        return {'candidates':observations,'college_data':college,'coverage_warnings':warnings,
                'automatic_wagering':False,'adapter_status':'market data connected; model and research evidence required'}


def college_match(row, data):
    """Exact teams and start-time match; ambiguous identities never merge."""
    games=[]
    for game in data.get('games',{}).get('rows',[]):
        try:
            if game['homeTeam']==row['home_name'] and game['awayTeam']==row['away_name'] and not game.get('startTimeTBD') and abs(timestamp(game['startDate'])-timestamp(row['start_time']))<=timedelta(minutes=5):
                games.append(game)
        except (KeyError,ValueError,TypeError):continue
    return {'games':games if len(games)==1 else [],
            'team_statistics':[x for x in data.get('stats/season',{}).get('rows',[]) if x.get('team') in {row['home_name'],row['away_name']}],
            'matched':len(games)==1}


def normalize_event(event, *, sport, collected_at, allowed_books):
    base={'event_id':sport+':odds:'+event['id'],'sport':sport,'home_name':event['home_team'],'away_name':event['away_team'],
          'start_time':event['commence_time'],'period':'full_game','evidence':[],
          'material_information_complete':False,'conflict_clear':False,'exposure_clear':False,
          'settlement_rule':'','model':{},'stability':'unknown','publication_decision':'pass',
          'first_blocker':'Missing model and availability research'}
    groups={}
    for book in event.get('bookmakers',[]):
        if book['key'] not in allowed_books:continue
        for market in book.get('markets',[]):
            market_key=market.get('key')
            if market_key not in MARKETS:continue
            outcomes=market.get('outcomes',[])
            expected={event['home_team'],event['away_team']} if market_key!='totals' else {'Over','Under'}
            try:
                if len(outcomes)!=2 or {x['name'] for x in outcomes}!=expected:continue
                prices=[float(decimal(x['price'])) for x in outcomes]
                if any(p<=1 or not math.isfinite(p) for p in prices):continue
                if market_key=='spreads' and float(decimal(outcomes[0]['point'])) != -float(decimal(outcomes[1]['point'])):continue
                if market_key=='totals' and decimal(outcomes[0]['point'])!=decimal(outcomes[1]['point']):continue
                updated=market.get('last_update',book.get('last_update'))
                timestamp(updated)
                for index,outcome in enumerate(outcomes):
                    name=outcome['name'];line=float(decimal(outcome['point'])) if market_key!='h2h' else None
                    participant=name.lower() if market_key=='totals' else 'home' if name==event['home_team'] else 'away'
                    key=(market_key,participant,line)
                    group=groups.setdefault(key,[])
                    if any(q['book']==book['key'] for q in group):continue
                    group.append({'book':book['key'],'status':'open','source_updated_at':updated,'collected_at':collected_at,
                                  'selected_index':index,'outcomes':[{'name':x['name'],'decimal_odds':prices[i]} for i,x in enumerate(outcomes)]})
            except (KeyError,ValueError,TypeError,ArithmeticError):continue
    out=[]
    for (market_key,participant,line),quotes in groups.items():
        for quote in quotes:
            row={**base,'market':MARKETS[market_key],'participant':participant,'line':line,
                 'candidate_id':base['event_id']+':'+market_key+':'+participant+':'+str(line)+':'+quote['book'],
                 'selection_name':quote['outcomes'][quote['selected_index']]['name'],'book_of_record':quote['book'],
                 'book_accessible':False,'market_status':'open','decimal_odds':quote['outcomes'][quote['selected_index']]['decimal_odds'],
                 'price_time':quote['source_updated_at'],'outcome_count':2,'all_approved_sources_attempted':True,
                 'source_attempts':[{'book':b,'result':'obtained' if any(q['book']==b for q in quotes) else 'not returned or different line'} for b in allowed_books]}
            identity={k:row[k] for k in ('event_id','market','period','line','participant','settlement_rule')}
            row['quotes']=[{**q,**identity} for q in quotes]
            out.append(row)
    if not out:
        out.append({**base,'candidate_id':base['event_id']+':slate','market':'watch','selection_name':'Slate watch'})
    return out
