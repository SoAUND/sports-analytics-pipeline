"""Enumerate every active provider league, independently of pick limits.

Provider coverage is not claimed to be the complete Florida sportsbook catalog.
Missing odds, unsupported evaluators and failed requests remain explicit.
"""
from datetime import timedelta, timezone
from hashlib import sha256
import json
from .collectors.http import CollectorHttpError
from .experimental_calls import BASE, sport_route, market_calls
from .revised_strategy import ET, timestamp
from .value_models import shop_public_board


def collect_full_slate(db, transport, *, api_key, books, accessible_books, as_of, preview=False):
    audit={'date_et':str(as_of.astimezone(ET).date()), 'scope':'All active provider leagues; Primary-book catalog completeness unverified',
           'leagues':[], 'events':[], 'markets_requested':['h2h','spreads','totals'],
           'additional_markets':'Player props need matching public quotes, supported history statistics and evidence-backed upcoming opportunity inputs',
           'price_shopping_inventory': []}
    if not api_key:
        audit['error']='Odds API key missing'
        return [],audit
    db.execute('CREATE TABLE IF NOT EXISTS full_slate_cache (key TEXT PRIMARY KEY, checked_at TEXT, payload TEXT)')
    start=as_of.astimezone(ET).replace(hour=0,minute=0,second=0,microsecond=0)
    end=start+timedelta(days=1)
    dates={'commenceTimeFrom':start.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),
           'commenceTimeTo':(end-timedelta(seconds=1)).astimezone(timezone.utc).isoformat().replace('+00:00','Z')}
    def get(path,params,minutes):
        identity=sha256((path+json.dumps(params,sort_keys=True)+sha256(api_key.encode()).hexdigest()).encode()).hexdigest()
        old=db.execute('SELECT checked_at,payload FROM full_slate_cache WHERE key=?',(identity,)).fetchone()
        if old and as_of-timedelta(minutes=minutes)<=timestamp(old[0])<=as_of:
            return json.loads(old[1]),remaining
        response=transport.get(BASE+path,params={'apiKey':api_key,**params})
        if response.status!=200:raise CollectorHttpError('Provider request failed',status=response.status)
        payload=response.json()
        if not isinstance(payload,list):raise CollectorHttpError('Provider list malformed')
        with db:db.execute('INSERT OR REPLACE INTO full_slate_cache VALUES (?,?,?)',(identity,as_of.isoformat(),json.dumps(payload)))
        header_remaining=response.headers.get('x-requests-remaining')
        return payload,int(header_remaining) if header_remaining is not None else None
    try:
        catalog,remaining=get('/sports',{},0)
    except (CollectorHttpError,ValueError,TypeError,OSError) as exc:
        audit['error']='Sport catalog unavailable (status '+str(getattr(exc,'status',None))+')'
        return [],audit
    # Always enumerate free events, even when paid odds cannot be refreshed.
    active=[x for x in catalog if x.get('active') and not x.get('has_outrights') and isinstance(x.get('key'),str)]
    active.sort(key=lambda x:(sport_route(x['key'])!='nfl',x['key']))
    calls=[]
    seen=set()
    for league in active:
        key=league['key'];route=sport_route(key)
        record={'provider_sport':key,'sport':route or key,'status':'pending','events_today':0,
                'evaluation':'supported moneyline research' if route else 'Generic experimental moneyline check; no sport-specific model',
                'primary_book_events':0,'quote_outcomes':0}
        audit['leagues'].append(record)
        try:
            events,left=get('/sports/'+key+'/events',dates,5)
            if left is not None:remaining=left
            today=[]
            for e in events:
                try:
                    tip=timestamp(e['commence_time'])
                    if tip.astimezone(ET).date()!=start.date() or tip<=as_of:continue
                    if not e.get('id') or not e.get('home_team') or not e.get('away_team'):continue
                    if (key,e['id']) in seen:continue
                    seen.add((key,e['id']));today.append(e)
                    audit['events'].append({'event_id':e['id'],'sport':route or key,'sport_title':league.get('title',key),
                        'provider_sport':key,'home_name':e['home_team'],'away_name':e['away_team'],
                        'start_time':tip.isoformat(),'primary_book_status':'Not verified by event discovery',
                        'evaluation_status':'Preview only' if preview else 'Awaiting odds'})
                except (KeyError,TypeError,ValueError):record['malformed_events']=record.get('malformed_events',0)+1
            record['events_today']=len(today)
            record['status']='preview' if preview else 'no_events_today' if not today else 'discovered'
            if preview or not today:continue
            # Three standard markets, at most ten books: worst-case cost is three credits.
            if not books or len(set(books))>10:
                record['status']='Book configuration unavailable'
                for event in audit['events']:
                    if event['provider_sport']==key:event['evaluation_status']=record['status']
                continue
            if remaining is None or remaining-3<50:
                record['status']='Odds deferred: provider credit reserve or balance unverified'
                for event in audit['events']:
                    if event['provider_sport']==key:event['evaluation_status']=record['status']
                continue
            odds,left=get('/sports/'+key+'/odds',{**dates,'bookmakers':','.join(dict.fromkeys(books)),
                'markets':'h2h,spreads,totals','oddsFormat':'decimal','dateFormat':'iso'},30)
            if left is not None:remaining=left
            else:remaining=None
            indexed={e.get('id'):e for e in odds}
            audit['price_shopping_inventory'].extend(shop_public_board(odds, as_of=as_of))
            for e in audit['events']:
                if e['provider_sport']!=key:continue
                quoted=indexed.get(e['event_id'],{})
                own=[b for b in quoted.get('bookmakers',[]) if b.get('key') in accessible_books]
                e['primary_book_status']='Quoted in configured accessible feed' if own else 'No accessible-book quote returned'
                e['evaluation_status']='Core market quotes collected; downstream projection and eligibility checks required' if quoted else 'No usable event odds'
                record['primary_book_events']+=bool(own)
                record['quote_outcomes']+=sum(len(m.get('outcomes',[])) for b in quoted.get('bookmakers',[]) for m in b.get('markets',[]))
            calls.extend(market_calls(odds,as_of=as_of,accessible_books=accessible_books))
            record['status']='odds_checked'
        except (CollectorHttpError,ValueError,TypeError,OSError) as exc:
            record['status']='Provider request failed';record['http_status']=getattr(exc,'status',None)
            for event in audit['events']:
                if event['provider_sport']==key:event['evaluation_status']=record['status']
    audit['remaining_credits']=remaining
    audit['total_events']=len(audit['events'])
    audit['active_leagues_checked']=len(audit['leagues'])
    return calls,audit
