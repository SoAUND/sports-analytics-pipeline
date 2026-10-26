import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from sports_analytics_pipeline.collectors.http import HttpResponse, CollectorHttpError
from sports_analytics_pipeline.public_research import catalog, parse_source, PublicResearchCollector, attach_public_research
from sports_analytics_pipeline.revised_strategy import SPORTS

NOW = datetime(2026, 10, 1, 18, tzinfo=timezone.utc)


def spec(key='one', **kw):
    return dict(source_key=key, sports=['nhl'], url='https://example.com/'+key,
                category='injury_availability', official=False, parser='rss',
                source_family='example', cache_minutes=30, **kw)


RSS = b'<rss><channel><item><title>Boston Bruins goalie expected to start</title><link>https://example.com/story</link><pubDate>Thu, 01 Oct 2026 17:00:00 GMT</pubDate></item></channel></rss>'


class Transport:
    def __init__(self, fail=False): self.calls=[]; self.fail=fail
    def get(self, url, **kw):
        self.calls.append(url)
        if self.fail: raise CollectorHttpError('failed', status=403)
        return HttpResponse(200, RSS, {}, NOW)


class PublicResearchTests(unittest.TestCase):
    def test_catalog_covers_every_supported_sport(self):
        sources=catalog()
        self.assertEqual(set(SPORTS), {s for row in sources for s in row['sports']})
        self.assertGreaterEqual(len(sources), 100)
        self.assertEqual(len(sources),len({r['source_key'] for r in sources}))

    def test_rss_preserves_original_time_and_context_only(self):
        rows,_=parse_source(spec(),RSS)
        self.assertEqual(rows[0]['source_updated_at'],'2026-10-01T17:00:00+00:00')
        self.assertEqual(rows[0]['verification_status'],'context_only')

    def test_html_projection_is_not_confirmed_and_has_unknown_time(self):
        s=spec(); s['parser']='html';s['category']='lineup_or_participants'
        rows,_=parse_source(s,b'<div>Boston Bruins</div><div>Goalie</div><div>Likely</div>')
        self.assertEqual(rows[0]['reported_status'],'likely')
        self.assertIsNone(rows[0]['source_updated_at'])
        self.assertEqual(rows[0]['verification_status'],'context_only')

    def test_empty_dynamic_page_is_unparsed(self):
        s=spec();s['parser']='html'
        with self.assertRaises(ValueError):parse_source(s,b'<h1>Loading games...</h1>')

    def test_cached_records_keep_original_download_and_source_dates(self):
        db=sqlite3.connect(':memory:');t=Transport();c=PublicResearchCollector(db,t,sources=[spec()])
        a=c.collect(sports=['nhl'],as_of=NOW)
        b=c.collect(sports=['nhl'],as_of=NOW+timedelta(minutes=10))
        self.assertEqual(len(t.calls),1)
        self.assertEqual(a['records'],b['records'])
        self.assertEqual(b['source_attempts'][0]['mode'],'cache')

    def test_failed_requests_consume_budget_and_rotate(self):
        db=sqlite3.connect(':memory:');t=Transport(fail=True)
        c=PublicResearchCollector(db,t,sources=[spec('one'),spec('two')],max_requests=1,daily_cap=2)
        a=c.collect(sports=['nhl'],as_of=NOW)
        b=c.collect(sports=['nhl'],as_of=NOW+timedelta(minutes=31))
        self.assertEqual(t.calls,['https://example.com/one','https://example.com/two'])
        self.assertEqual(a['source_attempts'][0]['status'],'http_403')
        self.assertEqual(a['source_attempts'][1]['status'],'deferred_budget')
        self.assertEqual(b['records'],[])

    def test_shared_url_requested_once_for_two_sports(self):
        a=spec();b={**a,'source_key':'two','sports':['cfb']}
        db=sqlite3.connect(':memory:');t=Transport()
        report=PublicResearchCollector(db,t,sources=[a,b]).collect(sports=['nhl','cfb'],as_of=NOW)
        self.assertEqual(len(t.calls),1)
        self.assertEqual(set(report['source_attempts'][0]['sports']),{'nhl','cfb'})

    def test_matching_rejects_old_news_and_deduplicates_syndication(self):
        rows,_=parse_source(spec(),RSS)
        report={'source_attempts':[{'source_key':'one','sports':['nhl']}],
                'records':rows+[{**rows[0],'source_family':'other'}]+[{**rows[0],'text':'Boston Bruins injury old','source_updated_at':'2026-09-01T00:00:00+00:00'}]}
        candidate={'sport':'nhl','home_name':'Boston Bruins','away_name':'Florida Panthers','start_time':NOW.isoformat(),'lineup_verified':False}
        out=attach_public_research([candidate],report,as_of=NOW)[0]
        self.assertEqual(len(out['public_research']),1)
        self.assertFalse(out['lineup_verified'])

    def test_scoreboard_game_time_mismatch_is_rejected(self):
        s=spec();s['parser']='espn_scoreboard'
        rows,_=parse_source(s,json.dumps({'events':[{'date':'2026-10-05T18:00:00Z','competitions':[{'competitors':[{'team':{'displayName':'Boston Bruins'}},{'team':{'displayName':'Florida Panthers'}}]}]}]}).encode())
        report={'source_attempts':[{'source_key':'one','sports':['nhl']}],'records':rows}
        out=attach_public_research([{'sport':'nhl','home_name':'Boston Bruins','start_time':NOW.isoformat()}],report,as_of=NOW)[0]
        self.assertEqual(out['public_research'],[])

    def test_discovery_does_not_cross_publisher_hosts(self):
        s=spec();s['parser']='html'
        body=b'<a href="https://outside.example/article">Boston Bruins injury update today</a>'
        class HtmlTransport:
            def get(self,*a,**kw):return HttpResponse(200,body,{},NOW)
        db=sqlite3.connect(':memory:')
        PublicResearchCollector(db,HtmlTransport(),sources=[s]).collect(sports=['nhl'],as_of=NOW)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM public_discovered_sources').fetchone()[0],0)


if __name__=='__main__':unittest.main()
