#!/usr/bin/env python3
"""Run Our revised evidence workflow and durable research-message cadence."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.request import Request, urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from sports_analytics_pipeline.research_cadence import prepare_daily_jobs, prepare_manual_job, due_jobs, run_due, pending_messages, mark_delivered
from sports_analytics_pipeline.revised_strategy import timestamp, VERSION, ET


def send_card(file,db=None,message_key=None):
    server=os.environ.get('GITHUB_SERVER_URL','https://github.com')
    repository=os.environ.get('GITHUB_REPOSITORY','OWNER/sports-analytics-pipeline')
    run_id=os.environ.get('GITHUB_RUN_ID','')
    run_url=f'{server}/{repository}/actions'+(f'/runs/{run_id}' if run_id else '')
    text=Path(file).read_text()
    header=text.splitlines()[0] if text.splitlines() else 'Research'
    is_detail=header.startswith(('RESEARCH DETAILS','MIDNIGHT SLATE LOOK','SLATE COVERAGE'))
    limit=3200-len(header)-2 if is_detail else 3200
    chunks=[];current=''
    for line in text.splitlines(keepends=True):
        while len(line)>limit:
            if current:chunks.append(current);current=''
            chunks.append(line[:limit]);line=line[limit:]
        if len(current)+len(line)>limit:chunks.append(current);current=''
        current+=line
    if current:chunks.append(current)
    for i,chunk in enumerate(chunks or ['']):
        receipt=f'{message_key}:part:{i+1}'
        if db is not None:
            db.execute('CREATE TABLE IF NOT EXISTS research_delivery_parts (part_key TEXT PRIMARY KEY)')
            if db.execute('SELECT 1 FROM research_delivery_parts WHERE part_key=?',(receipt,)).fetchone():continue
        if is_detail and i>0:chunk=header+'\n\n'+chunk
        part=Path(file) if len(chunks)<=1 else Path(file).with_name(Path(file).stem+f'-part-{i+1}.txt')
        if part!=Path(file):part.write_text(chunk)
        title=(header if header.startswith('') else 'Research')+(f' ({i+1}/{len(chunks)})' if len(chunks)>1 else '')
        subprocess.run([sys.executable,'scripts/send_telegram_notification.py','--mode','file',
                        '--title',title,'--file',str(part),'--run-url',run_url],check=True)
        if db is not None:
            with db:db.execute('INSERT OR IGNORE INTO research_delivery_parts VALUES (?)',(receipt,))
        time.sleep(3.2)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',default='var/revised_research.sqlite3')
    parser.add_argument('--input',default=os.environ.get('SAP_RESEARCH_INPUT','data/research_candidates/current.json'))
    parser.add_argument('--as-of',type=timestamp)
    parser.add_argument('--source-database',default=os.environ.get('SAP_SOURCE_DATABASE'))
    parser.add_argument('--supplemental-input',default=os.environ.get('SAP_SUPPLEMENT_INPUT','data/research_candidates/supplemental.json'))
    parser.add_argument('--send',action='store_true',help='Deliver research cards using existing Telegram secrets')
    parser.add_argument('--run-now',action='store_true',help='Review remaining games today in Eastern time without changing daily jobs')
    parser.add_argument('--slate-now',action='store_true',help='Send a fresh full-day slate preview without picks or resetting daily jobs')
    args=parser.parse_args()
    if args.send and args.as_of:parser.error('Historical previews cannot send messages')
    now=args.as_of or datetime.now(timezone.utc)
    Path(args.database).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(args.database);db.row_factory=sqlite3.Row
    try:
        prepare_daily_jobs(db,now)
        # A delivery retry must never suppress an explicitly requested fresh scan.
        # Each invocation owns a new manual job; prior outbox messages stay durable.
        if args.run_now:
            prepare_manual_job(db,now)
        if args.slate_now:
            db.execute('INSERT OR IGNORE INTO research_jobs VALUES(?,?,?,?,NULL)',('slate-preview:'+now.isoformat(),'overnight',now.isoformat(),'{}'))
            db.commit()
        jobs=due_jobs(db,now)
        source=os.environ.get('SAP_RESEARCH_CANDIDATES_URL','')
        payload={}
        if jobs or os.environ.get('SAP_RESEARCH_RESULTS_DUE')=='true':
            if source:
                # Explicitly configured public/approved evidence adapter; no sportsbook session.
                if not source.startswith('https://'):raise ValueError('Evidence feed must use HTTPS')
                with urlopen(Request(source,headers={'User-Agent':'SportsAnalytics-Research/2026-10-01'}),timeout=30) as response:
                    payload=json.load(response)
            elif Path(args.input).exists():
                payload=json.loads(Path(args.input).read_text())
            else:
                payload={'candidates':[],'coverage_warning':'No evidence adapter input configured; no picks issued'}
            if args.source_database and Path(args.source_database).exists():
                from sports_analytics_pipeline.config import Settings
                from sports_analytics_pipeline.research_export import export_candidates
                native=sqlite3.connect(args.source_database);native.row_factory=sqlite3.Row
                try:
                    payload.setdefault('candidates',[]).extend(export_candidates(native,Settings.load(),as_of=now))
                finally:
                    native.close()
            if Path(args.supplemental_input).exists():
                supplemental=json.loads(Path(args.supplemental_input).read_text())
                payload.setdefault('candidates',[]).extend(supplemental.get('candidates',[]))
                warnings=supplemental.get('coverage_warnings',[])
                if warnings:payload['coverage_warning']='; '.join(warnings)
            from sports_analytics_pipeline.public_research import PublicResearchCollector,attach_public_research,public_research_sport
            from sports_analytics_pipeline.collectors.http import UrllibTransport
            candidates=payload.get('candidates',[])
            if not args.as_of:
                from sports_analytics_pipeline.full_slate import collect_full_slate
                from sports_analytics_pipeline.config import Settings
                from sports_analytics_pipeline.collectors.http import UrllibTransport
                settings=Settings.load()
                calls,coverage=collect_full_slate(db,UrllibTransport(timeout_seconds=8,attempts=1),
                    api_key=settings.odds_api_key,books=settings.accessible_bookmakers+settings.market_consensus_bookmakers,
                    accessible_books=settings.accessible_bookmakers,as_of=now,
                    preview=all(job['kind']=='overnight' for job in jobs))
                warnings=[coverage['scope']]
                payload['slate_events']=coverage['events']
                payload['slate_coverage']=coverage
                Path('var/revised-cards').mkdir(parents=True,exist_ok=True)
                Path('var/revised-cards/slate-coverage.json').write_text(json.dumps(coverage,indent=2))
                print(json.dumps({'slate_coverage':coverage}))
                payload['experimental_calls']=calls
                from sports_analytics_pipeline.connected_research import inventory_candidates
                payload['experimental_calls'].extend(inventory_candidates(coverage.get('price_shopping_inventory',[]),accessible_books=settings.accessible_bookmakers))
                payload['call_warnings']=warnings
                print(json.dumps({'experimental_calls':calls,'call_warnings':warnings}))
            if args.run_now:
                today=now.astimezone(ET).date()
                filtered=[]
                for row in candidates:
                    try:
                        tip=timestamp(row.get('start_time'))
                        if tip>now and tip.astimezone(ET).date()==today:filtered.append(row)
                    except (ValueError,TypeError):continue
                candidates=filtered
                payload['candidates']=candidates
            active={public_research_sport(r.get('sport')) for r in candidates+payload.get('experimental_calls',[]) if public_research_sport(r.get('sport')) in {'nfl','mlb','nba','wnba','nhl','cfb','epl','mma','atp','wta'}}
            if active and not args.as_of:
                public=PublicResearchCollector(db,UrllibTransport(timeout_seconds=8,attempts=1)).collect(sports=active,as_of=now)
                payload['candidates']=attach_public_research(candidates,public,as_of=now)
                payload['experimental_calls']=attach_public_research(payload.get('experimental_calls',[]),public,as_of=now)
                from sports_analytics_pipeline.evidence_gap_report import evidence_gaps
                for call in payload['experimental_calls']:call['evidence_gaps']=evidence_gaps(call)
                Path('var/public-research').mkdir(parents=True,exist_ok=True)
                Path('var/public-research/latest.json').write_text(json.dumps(public,indent=2))
                print(json.dumps({'public_requests':public['requests'],'public_records':len(public['records']),
                    'public_sources_registered':public['registered_sources'],'sports':public['sports']}))
            if not args.as_of and any(r.get('sport') in {'mma','epl'} or str(r.get('sport','')).startswith('soccer') for r in payload.get('candidates',[])+payload.get('experimental_calls',[])):
                from sports_analytics_pipeline.collectors.public_player_data import PublicPlayerCollector
                from sports_analytics_pipeline.collectors.soccer_mma_data import SoccerMmaCollector, attach_soccer_mma
                sport_cache=PublicPlayerCollector(db,UrllibTransport(timeout_seconds=8,attempts=1),max_requests=100)
                sport_collector=SoccerMmaCollector(sport_cache)
                payload['candidates']=attach_soccer_mma(payload.get('candidates',[]),sport_collector,as_of=now)
                payload['experimental_calls']=attach_soccer_mma(payload.get('experimental_calls',[]),sport_collector,as_of=now)
                Path('var/public-research').mkdir(parents=True,exist_ok=True)
                Path('var/public-research/soccer-mma-history.json').write_text(json.dumps([
                    {'event_id':r.get('event_id'),'sport_history':r['sport_history']} for r in payload.get('candidates',[])+payload.get('experimental_calls',[]) if r.get('sport_history')],indent=2))
                now=datetime.now(timezone.utc)
            if not args.as_of and any(r.get('market')=='player_prop' for r in payload.get('candidates',[])):
                from sports_analytics_pipeline.collectors.public_player_data import PublicPlayerCollector, attach_player_data
                player_collector=PublicPlayerCollector(db,UrllibTransport(timeout_seconds=8,attempts=1))
                payload['candidates']=attach_player_data(payload.get('candidates',[]),player_collector,as_of=now)
                Path('var/public-research').mkdir(parents=True,exist_ok=True)
                player_audit=[{'candidate_id':r.get('candidate_id'),'event_id':r.get('event_id'),
                              'public_player_data':r['public_player_data']} for r in payload['candidates'] if r.get('public_player_data')]
                Path('var/public-research/player-history.json').write_text(json.dumps(player_audit,indent=2,default=str))
                print(json.dumps({'public_player_requests':player_collector.requests,'players_checked':len(player_audit),
                                  'history_collected':sum(r['public_player_data']['status']=='collected' for r in player_audit)}))
                # Fresh response timestamps must precede evaluation/model cutoffs.
                now=datetime.now(timezone.utc)
            if not args.as_of:
                from sports_analytics_pipeline.connected_research import ConnectedResearchCollector,attach_connected_research
                from sports_analytics_pipeline.collectors.public_player_data import PublicPlayerCollector
                # One cache/session covers all events and markets, regardless of earlier failures.
                structured_cache=PublicPlayerCollector(db,UrllibTransport(timeout_seconds=8,attempts=1),max_requests=180)
                connected=ConnectedResearchCollector(structured_cache)
                payload['candidates']=attach_connected_research(payload.get('candidates',[]),connected,as_of=now)
                payload['experimental_calls']=attach_connected_research(payload.get('experimental_calls',[]),connected,as_of=now)
                from sports_analytics_pipeline.follow_through import apply_follow_through
                payload['candidates']=apply_follow_through(payload['candidates'],connected,as_of=now)
                payload['experimental_calls']=apply_follow_through(payload['experimental_calls'],connected,as_of=now)
                now=datetime.now(timezone.utc)
                connected_rows=payload['candidates']+payload['experimental_calls']
                Path('var/public-research').mkdir(parents=True,exist_ok=True)
                Path('var/public-research/connected-inputs.json').write_text(json.dumps(connected_rows,indent=2,default=str))
                print(json.dumps({'structured_connection_requests':structured_cache.requests,
                    'rows_connected':len(connected_rows),
                    'experimental_projections':sum(r.get('projection_connection',{}).get('status')=='experimental_projection' for r in connected_rows),
                    'missing_or_blocked':sum(r.get('projection_connection',{}).get('status')=='blocked' for r in connected_rows)}))
            evaluated=run_due(db,now,payload)
            # Full evidence stays in the run artifact, not dozens of Telegram cards.
            Path('var/revised-cards').mkdir(parents=True,exist_ok=True)
            Path('var/revised-cards/evaluated-selections.json').write_text(json.dumps(evaluated,indent=2,default=str))
            full_audits=[json.loads(r['decision_json']) for r in db.execute(
                'SELECT decision_json FROM research_audit WHERE run_at=? ORDER BY input_key',(now.isoformat(),))]
            Path('var/revised-cards/full-evaluation-audit.json').write_text(json.dumps(full_audits,indent=2,default=str))
            value_audit = [{'candidate_id':r.get('candidate_id'), 'event_id':r.get('event_id'),
                            'price_shopping':r.get('price_shopping'), 'prop_distribution':r.get('prop_distribution'),
                            'publication_decision':r.get('publication_decision'), 'first_blocker':r.get('first_blocker'),
                            'full_evaluation':r.get('full_evaluation')}
                           for r in evaluated]
            Path('var/revised-cards/value-analysis-audit.json').write_text(json.dumps(value_audit,indent=2,default=str))
            ticket_audit=db.execute('SELECT result_json FROM research_parlay_audit WHERE run_at=?',(now.isoformat(),)).fetchone()
            if ticket_audit:
                Path('var/revised-cards/parlay-audit.json').write_text(ticket_audit[0])
            if args.run_now:
                summary={'checked_at':now.isoformat(),'date_et':now.astimezone(ET).date().isoformat(),
                    'candidates':len(evaluated),'qualified':sum(r.get('publication_decision') in {'official_pick','experimental'} for r in evaluated),
                    'decisions':[{k:r.get(k) for k in ('sport','home_name','away_name','start_time','selection_name','publication_decision','first_blocker')} for r in evaluated],
                    'coverage_warning':payload.get('coverage_warning'),'experimental_calls':payload.get('experimental_calls',[]),
                    'call_warnings':payload.get('call_warnings',[])}
                Path('var/revised-cards').mkdir(parents=True,exist_ok=True)
                Path('var/revised-cards/run-now-summary.json').write_text(json.dumps(summary,indent=2))
                print(json.dumps({'run_now':summary}))
        messages=pending_messages(db)
        Path('var/revised-cards').mkdir(parents=True,exist_ok=True)
        for i,message in enumerate(messages):
            file=Path('var/revised-cards')/f'card-{i}.txt';file.write_text(message['message']+'\n')
            if args.send:
                existing=db.execute('SELECT message_key FROM research_outbox WHERE message=? AND delivered_at IS NOT NULL LIMIT 1',(message['message'],)).fetchone()
                if not existing:send_card(file,db,message['message_key'])
                else:print(json.dumps({'reused_delivery_message_key':existing['message_key'],'duplicate_message_key':message['message_key']}))
                mark_delivered(db,message['message_key'],now)
                print(json.dumps({'delivered_message_key':message['message_key'],
                    'card_header':message['message'].splitlines()[0],
                    'checked_at':now.isoformat()}))
        print(json.dumps({'rule_version':VERSION,'jobs_due':len(jobs),'messages_pending':len(messages),
                          'automatic_wagering':False,'coverage_warning':payload.get('coverage_warning'),
                          'live_provider_adapters':['nfl','mlb','nba','wnba','epl','mma','atp','wta','nhl','cfb'],
                          'supplemental_review_required':['nhl','cfb'],
                          'free_player_history_adapters':['nfl','nba','wnba','mlb','nhl'],
                          'evidence_contract_only':['unmapped prop stats','current tennis surface/availability and validated forecasts','unmapped soccer leagues and stats','soccer xG/pressing and MMA weigh-in/foul adapters','confirmed role/matchup projections']},indent=2))
    finally:
        db.close()

if __name__=='__main__':main()
