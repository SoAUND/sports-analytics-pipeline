"""Level-1 market discovery rows.  These are watchlist items, never picks."""
from datetime import timedelta
from hashlib import sha256
import json
from statistics import median

from .collectors.http import CollectorHttpError
from .revised_strategy import timestamp, decimal, ET
from .sports_registry import SPORT_MARKETS
from .deep_analysis import advanced_metric_gaps, discoverable_markets

BASE='https://api.the-odds-api.com/v4'


def sport_route(key):
    exact={'americanfootball_nfl':'nfl','baseball_mlb':'mlb','icehockey_nhl':'nhl','americanfootball_ncaaf':'cfb'}
    return exact.get(key) or next((s for s,spec in SPORT_MARKETS.items() if spec.matches_provider_sport_key(key)),None)


def market_calls(events, *, as_of, accessible_books=()):
    """One market-consensus watchlist row per event; never a publishable selection."""
    calls=[]
    for event in events:
        try:
            sport=sport_route(event['sport_key']) or event['sport_key'];tip=timestamp(event['commence_time'])
            names=[event['home_team'],event['away_team']]
            if not sport or not event.get('id') or len(set(names))!=2 or not all(names):continue
            if not as_of<tip<=as_of+timedelta(hours=24) or tip.astimezone(ET).date()!=as_of.astimezone(ET).date():continue
            reference={};quotes=[];seen=set()
            for book in event.get('bookmakers',[]):
                if not book.get('key') or book['key'] in seen:continue
                market=next((m for m in book.get('markets',[]) if m.get('key')=='h2h'),None)
                if not market:continue
                updated=timestamp(market.get('last_update') or book.get('last_update'))
                if not as_of-timedelta(minutes=60)<=updated<=as_of:continue
                outcomes=market['outcomes'];labels=[o['name'] for o in outcomes]
                expected=set(names)|({'Draw'} if sport=='epl' or event['sport_key'].startswith('soccer_') else set())
                if set(labels)!=expected or len(labels)!=len(expected):continue
                prices=[decimal(o['price']) for o in outcomes]
                if any(p<=1 or not p.is_finite() for p in prices):continue
                raw=[1/p for p in prices];total=sum(raw)
                for o,p in zip(outcomes,raw):reference.setdefault(o['name'],[]).append(float(p/total))
                quotes.append((book['key'],outcomes,updated));seen.add(book['key'])
            if not reference:continue
            # Public market consensus determines this lean, not an invented independent model.
            selection=max(reference,key=lambda n:(median(reference[n]),n))
            offers=[(b,decimal(o['price']),u) for b,os,u in quotes for o in os if o['name']==selection and decimal('1.333333333333333333')<=decimal(o['price'])<=4]
            if not offers:continue
            own=[o for o in offers if o[0] in accessible_books]
            book,price,updated=max(own or offers,key=lambda x:x[1])
            available_keys={m.get('key') for b in event.get('bookmakers',[]) for m in b.get('markets',[]) if m.get('key')}
            row={'event_id':event['id'],'candidate_id':'market-watch:'+event['id'],
                'sport':sport,'home_name':names[0],'away_name':names[1],'start_time':tip.isoformat(),
                'market':'moneyline','period':'full_fight' if sport=='mma' else 'full_match' if sport in {'atp','wta'} else 'full_game',
                'selection_name':selection,'decimal_odds':str(price),'price_time':updated.isoformat(),
                'book_of_record':book,'book_accessible':bool(own),'market_status':'open',
                'price_scope':'verified_execution' if own else 'public_reference',
                'outcome_count':len(expected),
                'publication_decision':'watchlist','confidence':'Ungraded',
                'honesty_label':'Level-1 market discovery | not a pick',
                'reference_probability':median(reference[selection]),'matching_book_count':len(quotes),
                'market_inventory':discoverable_markets(available_keys),
                'first_blocker':'level_4_advanced_research','analysis_level':3,
                'missing_evidence':['Independent model probability/uncertainty','Official injuries/inactives',
                    'Confirmed starters/lineups','Rest/travel/schedule','Line movement','Full sport-specific matchup research'],
                'automatic_wagering':False,'countable_oos':False}
            # Preserve the complete mutually exclusive prices for the later audit.
            # Provider quotes are evidence of prices, never an independent forecast.
            row['quotes']=[{
                'event_id':row['event_id'],'market':row['market'],'period':row['period'],
                'book':b,'status':'open','source_key':'the_odds_api',
                'source_url':BASE+'/sports/'+event['sport_key']+'/odds',
                'source_updated_at':u.isoformat(),'collected_at':as_of.isoformat(),
                'selected_index':next(i for i,o in enumerate(os) if o['name']==selection),
                'outcomes':[{'name':o['name'],'decimal_odds':str(decimal(o['price']))} for o in os]
            } for b,os,u in quotes]
            row['advanced_metric_gaps']=advanced_metric_gaps(row)
            calls.append(row)
        except (KeyError,ValueError,TypeError,ArithmeticError):continue
    return sorted(calls,key=lambda r:(r['start_time'],r['sport'],r['event_id']))


def collect_calls(db, transport, *, api_key, books, accessible_books, as_of):
    """Discover core markets with a 30-minute cache and provider-credit reserve."""
    db.executescript('''CREATE TABLE IF NOT EXISTS experimental_call_requests(day TEXT NOT NULL,status TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS experimental_call_cache(cache_key TEXT PRIMARY KEY,checked_at TEXT NOT NULL,payload_json TEXT NOT NULL);''')
    if not api_key or not books:return [],['Experimental call odds key/book configuration missing']
    books=tuple(dict.fromkeys(books))
    if len(books)>10:return [],['Experimental call request exceeds ten-book budget']
    identity=sha256((','.join(books)+sha256(api_key.encode()).hexdigest()).encode()).hexdigest()
    old=db.execute('SELECT checked_at,payload_json FROM experimental_call_cache WHERE cache_key=?',(identity,)).fetchone()
    if old and as_of-timedelta(minutes=30)<=timestamp(old[0])<=as_of:
        return market_calls(json.loads(old[1]),as_of=as_of,accessible_books=accessible_books),[]
    day=as_of.astimezone(ET).date().isoformat()
    try:
        metadata=transport.get(BASE+'/sports',params={'apiKey':api_key})
        if metadata.status!=200:raise CollectorHttpError('Experimental call authentication failed',status=metadata.status)
        if int(metadata.headers.get('x-requests-remaining',0))<=50:return [],['Experimental call monthly credit reserve reached']
        with db:db.execute('INSERT INTO experimental_call_requests VALUES (?,?)',(day,'reserved'))
        response=transport.get(BASE+'/sports/upcoming/odds',params={'apiKey':api_key,'bookmakers':','.join(books),'markets':'h2h,spreads,totals','oddsFormat':'decimal','dateFormat':'iso'})
        if response.status!=200:raise CollectorHttpError('Experimental call odds unavailable',status=response.status)
        events=response.json()
        if not isinstance(events,list):raise CollectorHttpError('Experimental call response malformed')
        with db:
            db.execute('INSERT OR REPLACE INTO experimental_call_cache VALUES (?,?,?)',(identity,as_of.isoformat(),json.dumps(events)))
        return market_calls(events,as_of=as_of,accessible_books=accessible_books),[
            'Level-1 discovery covers provider h2h/spreads/totals only; unsupported props, alternates and period markets are reported as unavailable, never promoted to picks']
    except (CollectorHttpError,ValueError,TypeError,OSError):return [],['Experimental call provider unavailable; no prices invented']
