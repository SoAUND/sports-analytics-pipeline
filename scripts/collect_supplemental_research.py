#!/usr/bin/env python3
"""Collect supplemental data only for due jobs; never issue a real-money action or probability."""
import argparse
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.collectors.http import UrllibTransport,CollectorHttpError
from sports_analytics_pipeline.collectors.supplemental_research import SupplementAdapter
from sports_analytics_pipeline.research_cadence import prepare_daily_jobs,due_jobs
from sports_analytics_pipeline.revised_strategy import timestamp


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',default='var/revised_research.sqlite3')
    parser.add_argument('--output',default='data/research_candidates/supplemental.json')
    parser.add_argument('--review-input',default='data/research_candidates/supplemental-review.json')
    parser.add_argument('--check-models',action='store_true')
    parser.add_argument('--check-connections',action='store_true')
    parser.add_argument('--run-now',action='store_true',help='Collect a fresh on-demand NHL/CFB review within existing budgets')
    args=parser.parse_args()
    now=datetime.now(timezone.utc);settings=Settings.load()
    Path(args.database).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(args.database);db.row_factory=sqlite3.Row
    try:
        if args.check_connections:
            adapter=SupplementAdapter(db,UrllibTransport(timeout_seconds=settings.http_timeout_seconds,attempts=1),
                odds_key=settings.nhl_cfb_odds_api_key,cfbd_key=settings.cfbd_2_api_key)
            checks=adapter.check_connections(now=now)
            print(json.dumps(checks))
            if any(status!='connection succeeded' for status in checks.values()):raise SystemExit(1)
            if args.check_models:
                from sports_analytics_pipeline.supplemental_modeling import train_score_model
                history,warnings=adapter.training_history(now=datetime.now(timezone.utc),sports=('nhl','cfb'))
                print(json.dumps({'history_games':len(history),'diagnostics':warnings}))
                for sport in ('nhl','cfb'):
                    model=train_score_model(history,sport=sport,as_of=datetime.now(timezone.utc))
                    print(json.dumps({k:model[k] for k in ('sport','version','historical_training_count','historical_holdout_count','uncertainty','approved','oos_count')}))
            return
        prepare_daily_jobs(db,now)
        jobs=due_jobs(db,now)
        build=args.run_now or any(j['kind']=='midday' and now-timestamp(j['due_at'])<=timedelta(minutes=15) for j in jobs)
        slate=any(j['kind']=='overnight' for j in jobs)
        tip_sports={r.get('sport') for j in jobs if j['kind']=='tip30' for r in json.loads(j['payload_json'])}
        sports=('nhl','cfb') if build or slate else tuple(s for s in ('nhl','cfb') if s in tip_sports)
        payload={'candidates':[],'coverage_warnings':[]}
        if sports:
            accessible=tuple('hardrockbet_fl' if b=='hardrockbet' else b for b in settings.accessible_bookmakers)
            consensus=tuple('hardrockbet_fl' if b=='hardrockbet' else b for b in settings.market_consensus_bookmakers)
            books=tuple(dict.fromkeys(accessible+consensus))
            adapter=SupplementAdapter(db,UrllibTransport(timeout_seconds=settings.http_timeout_seconds,attempts=1),
                odds_key=settings.nhl_cfb_odds_api_key,cfbd_key=settings.cfbd_2_api_key,books=books)
            try:
                payload=adapter.collect(now=now,sports=sports,slate_only=slate and not build and not tip_sports)
                payload['candidates']=[r for r in payload['candidates'] if r.get('market')=='watch' or r.get('book_of_record') in accessible]
                for r in payload['candidates']:r['book_accessible']=r.get('book_of_record') in accessible
                from sports_analytics_pipeline.supplemental_modeling import train_score_model,score_prediction
                from sports_analytics_pipeline.supplemental_review import apply_review
                model_sports={r['sport'] for r in payload['candidates'] if r.get('market') in {'moneyline','spreads','totals'}}
                if model_sports:
                    history,warnings=adapter.training_history(now=datetime.now(timezone.utc),sports=model_sports)
                    payload['coverage_warnings'].extend(warnings)
                    reviews=json.loads(Path(args.review_input).read_text()) if Path(args.review_input).exists() else {}
                    models={};metrics={}
                    for sport in model_sports:
                        try:
                            model=train_score_model(history,sport=sport,as_of=datetime.now(timezone.utc));models[sport]=model
                            metrics[sport]={k:model[k] for k in ('version','uncertainty','historical_training_count','historical_holdout_count','oos_count','approved')}
                        except ValueError as exc:payload['coverage_warnings'].append(sport+': '+str(exc))
                    revised=[]
                    for row in payload['candidates']:
                        if row['sport'] in models and row.get('market')!='watch':
                            try:
                                at=datetime.now(timezone.utc)
                                row=apply_review(row,reviews,as_of=at,accessible_books=accessible)
                                row['model']=score_prediction(models[row['sport']],row,as_of=at)
                            except (ValueError,KeyError,TypeError):row['model']={}
                        revised.append(row)
                    payload['candidates']=revised;payload['model_metrics']=metrics
            except CollectorHttpError:
                payload['coverage_warnings']=['Supplement provider authentication/connection failed; no data promoted']
        Path(args.output).parent.mkdir(parents=True,exist_ok=True)
        Path(args.output).write_text(json.dumps(payload,indent=2,default=str))
        print(json.dumps({'sports_requested':sports,'candidates':len(payload['candidates']),
            'coverage_warnings':payload['coverage_warnings'],'automatic_wagering':False}))
    finally:db.close()

if __name__=='__main__':main()
