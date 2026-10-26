#!/usr/bin/env python3
"""Export timestamped source evidence; never infer missing facts."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.research_export import export_candidates
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--database',required=True);p.add_argument('--output',required=True)
a=p.parse_args();db=sqlite3.connect(a.database);db.row_factory=sqlite3.Row
try:
    rows=export_candidates(db,Settings.load(),as_of=datetime.now(timezone.utc))
    Path(a.output).parent.mkdir(parents=True,exist_ok=True)
    Path(a.output).write_text(json.dumps({'candidates':rows},indent=2,default=str))
finally:db.close()
