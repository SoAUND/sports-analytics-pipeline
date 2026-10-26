#!/usr/bin/env python3
"""Collect public-source context and diagnostics; consumes no paid odds credits."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from sports_analytics_pipeline.public_research import PublicResearchCollector,catalog
from sports_analytics_pipeline.collectors.http import UrllibTransport
from sports_analytics_pipeline.revised_strategy import SPORTS

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--sports',default=','.join(SPORTS));p.add_argument('--database',default='var/public_research.sqlite3')
p.add_argument('--output',default='var/public-research/latest.json');p.add_argument('--max-requests',type=int,default=96)
p.add_argument('--manifest-only',action='store_true')
a=p.parse_args()
if a.manifest_only:
 print(json.dumps({'sources':len(catalog()),'sports':sorted({s for r in catalog() for s in r['sports']})}));raise SystemExit(0)
Path(a.database).parent.mkdir(parents=True,exist_ok=True);db=sqlite3.connect(a.database)
try:
 report=PublicResearchCollector(db,UrllibTransport(timeout_seconds=8,attempts=1),max_requests=a.max_requests).collect(sports=a.sports.split(','),as_of=datetime.now(timezone.utc))
 Path(a.output).parent.mkdir(parents=True,exist_ok=True);Path(a.output).write_text(json.dumps(report,indent=2))
 print(json.dumps({'sources':report['registered_sources'],'requests':report['requests'],'records':len(report['records']),
  'source_attempts':[{k:r[k] for k in ('source_key','sports','status')} for r in report['source_attempts']]}))
finally:db.close()
