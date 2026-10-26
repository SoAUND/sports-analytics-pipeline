#!/usr/bin/env python3
"""Monitor only approved API sports and run the dedicated NFL paper cycle."""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))
from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data, alias_mapping
from sports_analytics_pipeline.collectors.base import MappingTeamResolver
from sports_analytics_pipeline.history import collect_historical_training_games
from sports_analytics_pipeline.collectors.http import UrllibTransport
from sports_analytics_pipeline.collectors.the_odds_api import TheOddsApiCollector
from sports_analytics_pipeline.quota_budget import DailyCreditBudget, poll_due, record_poll
from sports_analytics_pipeline.sports_registry import SPORT_MARKETS
from sports_analytics_pipeline.evaluation import evaluate_upcoming
from sports_analytics_pipeline.modeling import run_phase4
from sports_analytics_pipeline.paper import settle_paper_decisions
from scripts.run_collectors import collect_nflverse, collect_odds

UTC = timezone.utc
BASE = 'https://api.the-odds-api.com/v4'
APPROVED_SPORTS = ('nfl', 'mlb', 'nba', 'wnba', 'epl', 'mma', 'atp', 'wta')

def approved_catalog(rows):
    return [row for row in rows
            if route(str(row.get('key', ''))) in APPROVED_SPORTS
            and not row.get('has_outrights', False)]

def nfl_result_label(report):
    evaluation = report.get('nfl_evaluation', {})
    if not evaluation.get('games_found', 0):
        return 'NO_GAMES_DUE — no NFL games in the next-hour evaluation window'
    if report.get('nfl_model', {}).get('training', {}).get('status') != 'accepted' and report.get('nfl_model', {}).get('prediction', {}).get('status') == 'not_run':
        return 'BLOCKED_MODEL — no eligible prediction; not a no-edge verdict'
    return str(evaluation.get('qualification_status', 'BLOCKED'))

def render_message(report):
    evaluation = report.get('nfl_evaluation', {})
    lines = ['— APPROVED SPORTS AUTOPILOT',
             'Checked: ' + report['checked_at'],
             'Scope: NFL, MLB, NBA, WNBA, EPL, MMA/UFC, ATP, WTA.',
             'NFL games evaluated: ' + str(evaluation.get('games_found', 0)),
             'NFL result: ' + nfl_result_label(report),
             'NFL model status: ' + str(report.get('nfl_model', {}).get('training', {}).get('status', 'unavailable')),
             'MLB and the six multisport lanes retain their existing workflows and reports.']
    for item in evaluation.get('passes', [])[:8]:
        lines.append(str(item.get('game_id')) + ': ' + str(item.get('pass_reason')))
    training=report.get('nfl_model',{}).get('training',{})
    failed=[name for name,passed in training.get('acceptance_checks',{}).items() if passed is False]
    if failed:
        lines.append('Model artifact: '+str(training.get('model_version'))+' | validation rejected')
        lines.extend('Failed validation check: '+name for name in failed)
        metrics=training.get('metrics',{})
        lines.append('Historical model Brier: '+str(metrics.get('locked_test_brier'))+' | market baseline Brier: '+str(metrics.get('market_baseline_brier')))
        lines.append('Missing for current game: usable prediction record, win/cover probability and uncertainty estimate from an accepted model.')
    odds=report.get('nfl_odds',{})
    if odds.get('status')=='BLOCKED: quota budget':
        budget=odds.get('budget',{})
        lines.append('Odds refresh blocked: allowance '+str(budget.get('daily_allowance'))+'; counted usage '+str(budget.get('spent_today'))+'; account balance '+str(budget.get('estimated_remaining'))+'; reserve '+str(budget.get('reserve_credits'))+'.')
    for item in evaluation.get('test_only_selections', [])[:8]:
        lines.append('TEST ONLY: ' + json.dumps(item, default=str))
    if report.get('errors'):
        lines.append('ERRORS: ' + '; '.join(report['errors']))
    return '\n'.join(lines)


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Event timestamp must include timezone')
    return parsed.astimezone(UTC)

def route(key):
    if key == 'americanfootball_nfl':
        return 'nfl'
    if key == 'baseball_mlb':
        return 'mlb'
    for sport, spec in SPORT_MARKETS.items():
        if spec.matches_provider_sport_key(key):
            return sport
    return None

def event_gate(event, now):
    if timestamp(event['commence_time']) <= now:
        return 'PASS: pregame window closed; no backdated pick'
    if route(event['sport_key']) is None:
        return 'PASS: sport-specific model, research and settlement adapter not implemented'
    return 'AWAITING sport-specific evaluator; discovery alone is not a pick'

def ensure_audit(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS autopilot_coverage (
      sport_key TEXT PRIMARY KEY, title TEXT NOT NULL, active INTEGER NOT NULL,
      route TEXT, status TEXT NOT NULL, checked_at TEXT NOT NULL,
      events_checked_at TEXT, event_count INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS autopilot_event_checks (
      sport_key TEXT NOT NULL, event_id TEXT NOT NULL, checked_at TEXT NOT NULL,
      commence_time TEXT NOT NULL, decision TEXT NOT NULL, payload_json TEXT NOT NULL,
      PRIMARY KEY(sport_key,event_id));
    CREATE TABLE IF NOT EXISTS autopilot_runs (
      checked_at TEXT PRIMARY KEY, report_json TEXT NOT NULL);
    ''')

def requested_game(transport, now):
    # Read the actual scoreboard; never manufacture an as-of time or pregame odds.
    payload = transport.get('https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard',
                            params={'dates': '20260909'}).json()
    for event in payload.get('events', []):
        competition = event['competitions'][0]
        names = {c['team']['abbreviation'] for c in competition['competitors']}
        if names == {'NE', 'SEA'}:
            state = competition.get('status', event.get('status', {})).get('type', {})
            kickoff = timestamp(event['date'])
            closed = kickoff <= now or state.get('state') in {'in', 'post'}
            return {'game': event['name'], 'kickoff': kickoff.isoformat(),
                    'checked_at': now.isoformat(), 'source': 'https://www.espn.com/nfl/scoreboard/_/date/20260909',
                    'scoreboard_status': state.get('description'),
                    'decision': 'PASS' if closed else 'PENDING_EVALUATION',
                    'reason': 'Pregame window closed. No live-betting model is implemented.' if closed else 'Pregame research and price gates required.',
                    'countable_oos': False, 'official_pick': None,
                    'scores': {c['team']['displayName']: c.get('score') for c in competition['competitors']}}
    return {'game': 'Patriots at Seahawks, September 9, 2026', 'decision': 'PASS',
            'reason': 'Current event status could not be verified', 'countable_oos': False}

def run():
    settings = Settings.load('.env')
    db = connect(settings.database_path)
    initialize_schema(db)
    seed_reference_data(db)
    ensure_audit(db)
    now = datetime.now(UTC)
    transport = UrllibTransport(timeout_seconds=15, attempts=2)
    report = {'checked_at': now.isoformat(), 'mode': settings.bot_mode, 'errors': [], 'automatic_wagering': False}
    try:
        # Existing collector refreshes regular-season schedule, rosters and injuries.
        report['nfl_collection'] = collect_nflverse(SimpleNamespace(env='.env', season=now.year if now.month >= 3 else now.year-1, include_pbp=False))
        for name, result in report['nfl_collection'].items():
            if result.get('status') == 'failed':
                report['errors'].append('NFL ' + name + ': ' + result.get('error', 'collection failed'))
        report['nfl_source_warnings'] = [dict(row) for row in db.execute(
            "SELECT endpoint_name,warnings_json FROM collection_runs WHERE source_key='nflverse' ORDER BY started_at DESC LIMIT 3")]
        response = transport.get(BASE + '/sports/', params={'apiKey': settings.require_odds_api_key(), 'all': 'true'})
        catalog = response.json()
        if not isinstance(catalog, list):
            raise ValueError('Invalid sports catalog')
        catalog = approved_catalog(catalog)
        approved_keys = {row['key'] for row in catalog}
        report['approved_sports'] = list(APPROVED_SPORTS)
        report['catalog_sports'] = len(catalog)
        for sport in catalog:
            if sport.get('group', '').casefold() == 'politics':
                continue
            key = sport['key']
            active = sport.get('active') is True
            status = 'active_discovery' if active else 'offseason'
            if sport.get('has_outrights'):
                status = 'BLOCKED: outright market evaluation not implemented'
            db.execute('''INSERT INTO autopilot_coverage(sport_key,title,active,route,status,checked_at)
              VALUES(?,?,?,?,?,?) ON CONFLICT(sport_key) DO UPDATE SET title=excluded.title,
              active=excluded.active,route=excluded.route,status=excluded.status,checked_at=excluded.checked_at''',
              (key,sport.get('title',key),int(active),route(key),status,now.isoformat()))
        # Least-recently-polled rotation prevents a long catalog starving later sports.
        candidates = db.execute('''SELECT * FROM autopilot_coverage WHERE active=1
          AND status='active_discovery' ORDER BY CASE WHEN sport_key='americanfootball_nfl' THEN 0 ELSE 1 END,
          COALESCE(events_checked_at,''),sport_key''').fetchall()
        for sport in candidates:
            key = sport['sport_key']
            if key not in approved_keys:
                continue
            try:
                events = transport.get(BASE + '/sports/' + key + '/events', params={'apiKey': settings.require_odds_api_key()}).json()
                if not isinstance(events,list):
                    raise ValueError('Invalid events response')
                for event in events:
                    event['sport_key'] = key
                    decision = event_gate(event, now)
                    db.execute('INSERT OR REPLACE INTO autopilot_event_checks VALUES(?,?,?,?,?,?)',
                        (key,event['id'],now.isoformat(),event['commence_time'],decision,json.dumps(event)))
                db.execute('UPDATE autopilot_coverage SET events_checked_at=?,event_count=? WHERE sport_key=?',
                           (now.isoformat(),len(events),key))
            except Exception as exc:
                report['errors'].append(key + ': ' + str(exc))
                db.execute('UPDATE autopilot_coverage SET status=?,events_checked_at=? WHERE sport_key=?',
                           ('FAILED: event discovery',now.isoformat(),key))
        db.commit()
        # Only pay for NFL odds near an upcoming kickoff, paced by account-wide quota headers.
        due_games = db.execute("SELECT game_id FROM games WHERE season_type='regular' AND status='scheduled' AND kickoff_time>? AND kickoff_time<=?",
                       (now.isoformat(),(now+timedelta(minutes=90)).isoformat())).fetchall()
        metadata = TheOddsApiCollector._metadata(response.headers)
        budget = DailyCreditBudget.from_metadata(db, provider_key='the_odds_api', metadata=metadata,
                as_of=now,reserve_credits=settings.odds_api_min_remaining_credits,daily_credit_cap=2)
        books = set(settings.market_consensus_bookmakers + settings.accessible_bookmakers)
        cost = max(1, math.ceil(len(books)/10))
        report['nfl_odds'] = {'status': 'not_due', 'near_kickoff_games': len(due_games)}
        if due_games and poll_due(db,provider_key='the_odds_api',sport='nfl',endpoint_name='odds',scope_key='americanfootball_nfl',as_of=now,interval_minutes=30):
            if budget.reserve(cost):
                # Persist reservation before network activity so failures cannot loop paid calls.
                record_poll(db,provider_key='the_odds_api',sport='nfl',endpoint_name='odds',scope_key='americanfootball_nfl',polled_at=now,request_cost=cost,requests_remaining=budget.current_remaining)
                db.commit()
                report['nfl_odds'] = collect_odds(SimpleNamespace(env='.env'))
                budget.reconcile(report['nfl_odds'].get('quota'))
            else:
                report['nfl_odds'] = {'status': 'BLOCKED: quota budget', 'budget': budget.as_dict()}
        if db.execute('SELECT COUNT(*) FROM historical_training_games').fetchone()[0] == 0:
            report['nfl_history'] = collect_historical_training_games(db,transport=transport,resolver=MappingTeamResolver(alias_mapping(db)))
        report['nfl_model'] = run_phase4(db,as_of=datetime.now(UTC),hours=1,persist=True)
        report['nfl_evaluation'] = evaluate_upcoming(db,as_of=datetime.now(UTC),hours=1,persist=True)
        report['nfl_settlement'] = settle_paper_decisions(db)
        report['coverage'] = [dict(row) for row in db.execute('SELECT * FROM autopilot_coverage ORDER BY sport_key') if row['sport_key'] in approved_keys]
    except Exception as exc:
        report['errors'].append(str(exc))
    db.execute('INSERT OR REPLACE INTO autopilot_runs VALUES(?,?)',(now.isoformat(),json.dumps(report,default=str)))
    db.commit()
    db.execute('PRAGMA wal_checkpoint(FULL)')
    db.close()
    output = Path('var/autopilot-report.json')
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report,indent=2,default=str))
    Path('var/autopilot-message.txt').write_text(render_message(report))
    print(json.dumps(report,indent=2,default=str))
    return 1 if report['errors'] else 0

if __name__ == '__main__':
    raise SystemExit(run())
