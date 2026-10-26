"""Bounded public research across every supported sport, with original provenance.

Pages and headlines are context, not proof that all players are available. Existing
structured official adapters remain the authority for verified safety categories.
"""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
from importlib.resources import files
import json
import re
import sqlite3
import unicodedata
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

from .collectors.http import CollectorHttpError
from .revised_strategy import timestamp, SPORTS

MAX_BYTES = 4 * 1024 * 1024
NEWS_WORDS = re.compile(r'injur|scratch|inactive|withdraw|suspend|lineup|line-up|goalie|starter|quarterback|weigh|roster|trade|out for|ruled out', re.I)
PRIORITY = {'injury_availability':0, 'lineup_or_participants':1, 'official_status':2, 'transactions_or_changes':3, 'news':4, 'statistics_analytics':5, 'rest_travel':6}


def catalog():
    return json.loads(files('sports_analytics_pipeline').joinpath('data/public_sources.json').read_text())['sources']


def safe_public_url(url):
    p=urlsplit(url)
    if p.scheme not in {'https','http'} or not p.hostname or p.username or p.password:
        return False
    return True


def clean_name(value):
    return re.sub(r'[^a-z0-9]+',' ',unicodedata.normalize('NFKD',str(value)).encode('ascii','ignore').decode().lower()).strip()


def public_research_sport(sport):
    """Route soccer-family context while retaining the event's actual competition."""
    return 'epl' if str(sport).startswith('soccer_') else sport


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[];self.links=[];self.hidden=0;self.href=None;self.anchor=[];self.scripts=[];self.script=None;self.script_type=None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag in {'script','style','noscript'}:
            self.hidden+=1
            if tag=='script':self.script=[];self.script_type=a.get('type','')
        if tag=='a':self.href=a.get('href');self.anchor=[]
        if tag=='time' and a.get('datetime'):self.parts.append(a['datetime'])
    def handle_endtag(self,tag):
        if tag in {'script','style','noscript'}:
            self.hidden=max(0,self.hidden-1)
            if tag=='script' and self.script is not None:
                if self.script_type=='application/ld+json':self.scripts.append(''.join(self.script))
                self.script=None
        if tag=='a' and self.href:
            self.links.append((self.href,' '.join(self.anchor)));self.href=None
    def handle_data(self,data):
        if self.script is not None:self.script.append(data)
        if self.hidden:return
        data=' '.join(data.split())
        if data:self.parts.append(data)
        if self.href:self.anchor.append(data)


def original_time(value):
    if not value:return None
    try:return timestamp(value).isoformat()
    except (ValueError,TypeError):
        try:
            at=parsedate_to_datetime(value)
            return at.astimezone(timezone.utc).isoformat() if at.tzinfo else None
        except (ValueError,TypeError,OverflowError):return None


def record(spec, *, text, url=None, updated=None, **extra):
    return {'source_key':spec['source_key'],'source_family':spec['source_family'],
            'source_url':url or spec['url'],'official':spec['official'],
            'category':spec['category'],'source_updated_at':original_time(updated),
            'text':' '.join(text.split())[:500],'status':'reported','verification_status':'context_only',**extra}


def parse_source(spec, body):
    if len(body)>MAX_BYTES:raise ValueError('response_too_large')
    text=body.decode('utf-8',errors='replace');parser=spec['parser'];records=[];discovered=[]
    if parser=='espn_scoreboard':
        payload=json.loads(text)
        for event in payload.get('events',[]):
            competitions=event.get('competitions',[])
            if not competitions:continue
            comp=competitions[0];teams=comp.get('competitors',[])
            if len(teams)!=2:continue
            names=[t.get('team',{}).get('displayName') or t.get('athlete',{}).get('displayName') for t in teams]
            if not all(names):continue
            records.append(record(spec,text=' vs '.join(names)+' | '+event.get('status',{}).get('type',{}).get('description','Unknown'),
                event_start=event.get('date'),participants=names,event_status=event.get('status',{}).get('type',{}).get('name'),provider_event_id=event.get('id')))
        return records,discovered
    if parser=='rss':
        root=ET.fromstring(text)
        for item in root.findall('.//item')+root.findall('.//{http://www.w3.org/2005/Atom}entry'):
            title=item.findtext('title') or item.findtext('{http://www.w3.org/2005/Atom}title') or ''
            link=item.findtext('link')
            if not link:
                node=item.find('{http://www.w3.org/2005/Atom}link');link=node.get('href') if node is not None else None
            date=item.findtext('pubDate') or item.findtext('{http://www.w3.org/2005/Atom}updated')
            if title and link and safe_public_url(link):records.append(record(spec,text=title,url=link,updated=date))
        return records[:100],discovered
    page=Page();page.feed(text)
    for script in page.scripts:
        try:
            objects=json.loads(script);objects=objects if isinstance(objects,list) else [objects]
            for obj in objects:
                if not isinstance(obj,dict):continue
                for article in [obj]+obj.get('@graph',[]):
                    if not isinstance(article,dict):continue
                    if article.get('@type') not in {'NewsArticle','Article','SportsEvent'}:continue
                    value=article.get('headline') or article.get('name') or ''
                    if value:records.append(record(spec,text=value,updated=article.get('dateModified') or article.get('datePublished')))
        except (ValueError,TypeError):pass
    for href,label in page.links:
        absolute=urljoin(spec['url'],href)
        if len(label)>25 and NEWS_WORDS.search(label) and safe_public_url(absolute):
            records.append(record(spec,text=label,url=absolute))
            if urlsplit(absolute).hostname==urlsplit(spec['url']).hostname:discovered.append(absolute)
        elif 'availability' in label.lower() and safe_public_url(absolute):discovered.append(absolute)
    # Extract reported snippets, never treat a live page fetch as a fresh source update.
    for i,part in enumerate(page.parts):
        if len(part)>25 and NEWS_WORDS.search(part):
            records.append(record(spec,text=' '.join(page.parts[max(0,i-2):i+3])))
    if spec['category'] in {'injury_availability','lineup_or_participants'}:
        for i,part in enumerate(page.parts):
            if part.lower() in {'out','questionable','probable','day-to-day','injured reserve','confirmed','likely','expected'}:
                records.append(record(spec,text=' '.join(page.parts[max(0,i-6):i+4]),reported_status=part.lower()))
    if not records:raise ValueError('dynamic_or_no_research_records')
    unique={sha256((r['source_url']+r['text']).encode()).hexdigest():r for r in records}
    return list(unique.values())[:150],list(dict.fromkeys(discovered))[:12]


class PublicResearchCollector:
    def __init__(self,db,transport,*,sources=None,max_requests=96,daily_cap=1000):
        if not 1<=max_requests<=200 or not 1<=daily_cap<=1000:raise ValueError('Invalid public request limits')
        self.db=db;self.transport=transport;self.sources=list(sources if sources is not None else catalog())
        self.max_requests=max_requests;self.daily_cap=daily_cap
        db.execute('CREATE TABLE IF NOT EXISTS public_source_cache (url TEXT PRIMARY KEY, checked_at TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS public_discovered_sources (url TEXT PRIMARY KEY, spec_json TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS public_source_requests (day TEXT NOT NULL, url TEXT NOT NULL, requested_at TEXT NOT NULL)')
        db.commit()

    def collect(self,*,sports,as_of,probe=False):
        wanted=set(sports)
        if wanted-set(SPORTS):raise ValueError('Unsupported public-research sport')
        all_sources=self.sources+[json.loads(r[0]) for r in self.db.execute('SELECT spec_json FROM public_discovered_sources')]
        specs=[s for s in all_sources if wanted.intersection(s['sports'])]
        # Deduplicate shared URLs: three pages from one publisher are not three independent sources.
        by_url={}
        for spec in specs:
            if not safe_public_url(spec['url']):continue
            if spec['url'] not in by_url:by_url[spec['url']]={**spec,'sports':list(spec['sports'])}
            else:by_url[spec['url']]['sports']=sorted(set(by_url[spec['url']]['sports'])|set(spec['sports']))
        cache={r[0]:r for r in self.db.execute('SELECT url,checked_at,status,payload_json FROM public_source_cache')}
        ordered=sorted(by_url.values(),key=lambda s:(cache.get(s['url'],('', '', '', ''))[1],PRIORITY.get(s['category'],9),s['source_key']))
        attempts=[];records=[];requests=0
        used=self.db.execute('SELECT COUNT(*) FROM public_source_requests WHERE day=?',(as_of.date().isoformat(),)).fetchone()[0]
        for spec in ordered:
            url=spec['url'];cached=cache.get(url)
            if cached and as_of-timedelta(minutes=spec['cache_minutes'])<=timestamp(cached[1])<=as_of:
                item=json.loads(cached[3]);status=cached[2];mode='cache'
            elif requests>=self.max_requests or used>=self.daily_cap:
                attempts.append({'source_key':spec['source_key'],'sports':spec['sports'],'url':url,'status':'deferred_budget','source_family':spec['source_family']});continue
            else:
                self.db.execute('INSERT INTO public_source_requests VALUES (?,?,?)',(as_of.date().isoformat(),url,as_of.isoformat()));self.db.commit()
                requests+=1;used+=1;mode='live'
                try:
                    response=self.transport.get(url,headers={'Accept':'application/json, application/rss+xml, text/html;q=0.9','User-Agent':'-Sports-Research/1.0'})
                    if response.status!=200:raise CollectorHttpError('Public source HTTP failure',status=response.status)
                    parsed,links=parse_source(spec,response.body)
                    item={'records':[{**r,'collected_at':as_of.isoformat()} for r in parsed],'discovered_links':links}
                    status='retrieved'
                    # Expand relevant public links within a known publisher; no login or hidden endpoint discovery.
                    count=self.db.execute('SELECT COUNT(*) FROM public_discovered_sources').fetchone()[0]
                    for link in links[:4]:
                        if count>=200:break
                        if urlsplit(link).hostname!=urlsplit(url).hostname or link==url:continue
                        linked={**spec,'url':link,'parser':'html','source_key':spec['source_key']+':link:'+sha256(link.encode()).hexdigest()[:12]}
                        self.db.execute('INSERT OR IGNORE INTO public_discovered_sources VALUES (?,?)',(link,json.dumps(linked)))
                        count+=1
                except (CollectorHttpError,ValueError,TypeError,AttributeError,KeyError,OSError,ET.ParseError,UnicodeError) as exc:
                    status='http_'+str(exc.status) if isinstance(exc,CollectorHttpError) and exc.status else 'unavailable_or_unparsed'
                    item={'records':[],'discovered_links':[]}
                self.db.execute('INSERT OR REPLACE INTO public_source_cache VALUES (?,?,?,?)',(url,as_of.isoformat(),status,json.dumps(item)));self.db.commit()
            attempts.append({'source_key':spec['source_key'],'sports':spec['sports'],'url':url,'status':status,'mode':mode,'source_family':spec['source_family'],
                             'records':len(item['records']),'checked_at':cached[1] if mode=='cache' else as_of.isoformat(),'discovered_links':item.get('discovered_links',[])})
            records.extend(item['records'])
        return {'as_of':as_of.isoformat(),'sports':sorted(wanted),'source_attempts':attempts,'records':records,'requests':requests,
                'registered_sources':len(specs),'automatic_wagering':False}


def attach_public_research(candidates,report,*,as_of):
    """Attach exact-name context; never promote unknown dates/projections to verified."""
    out=[]
    for original in candidates:
        row={**original};sport=public_research_sport(row.get('sport'));names=[clean_name(row.get(k,'')) for k in ('home_name','away_name')]
        sourcekeys={s['source_key'] for s in report['source_attempts'] if sport in s['sports']}
        matches=[];seen=set()
        for item in report['records']:
            if item['source_key'] not in sourcekeys:continue
            content=' '+clean_name(item['text'])+' '
            if not any(n and ' '+n+' ' in content for n in names):continue
            if item.get('event_start'):
                try:
                    if abs(timestamp(item['event_start'])-timestamp(row['start_time']))>timedelta(minutes=10):continue
                except (ValueError,KeyError):continue
            if item.get('source_updated_at'):
                try:
                    if not as_of-timedelta(days=3)<=timestamp(item['source_updated_at'])<=as_of:continue
                except ValueError:continue
            key=(item['source_family'],sha256(clean_name(item['text']).encode()).hexdigest())
            # Identical syndicated headlines count once even across different publishers.
            fingerprint=key[1]
            if fingerprint in seen:continue
            seen.add(fingerprint);matches.append(item)
        row['public_research']=matches[:30]
        row['public_source_attempts']=[a for a in report['source_attempts'] if sport in a['sports']]
        statuses={}
        for attempt in row['public_source_attempts']:
            status=attempt.get('status','unknown');statuses[status]=statuses.get(status,0)+1
        row['public_source_status_counts']=statuses
        row['public_research_scope']='soccer-family context; exact competition statistics and lineup confirmation still required' if str(row.get('sport')).startswith('soccer_') else 'exact sport context; reported items are not confirmed evidence'
        row['public_research_summary']=f"{len(matches)} matching public reports; {len({m['source_family'] for m in matches})} publisher families. Reports supplement confirmed evidence."
        out.append(row)
    return out
