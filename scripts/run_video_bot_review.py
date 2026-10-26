"""Run recorded video markets through existing bot code on disposable DB copies."""
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from sports_analytics_pipeline.mlb_modeling import create_mlb_model_run
from sports_analytics_pipeline.mlb_evaluation import evaluate_mlb_upcoming
from sports_analytics_pipeline.multisport_modeling import latest_model, predict_event
from sports_analytics_pipeline.multisport_market_baseline import BASELINE_FEATURE_VERSION, create_market_baseline_prediction
from sports_analytics_pipeline.multisport_evaluation import evaluate_event
from sports_analytics_pipeline.sports_registry import get_sport_market


def connect(folder):
    paths = list(Path(folder).rglob('*.sqlite3'))
    if len(paths) != 1:
        raise ValueError(f'Expected one restored DB in {folder}, found {len(paths)}')
    db = sqlite3.connect(paths[0])
    db.row_factory = sqlite3.Row
    return db


def decimal(odds):
    return 1 + (odds / 100 if odds > 0 else 100 / abs(odds))


def main():
    manifest = json.loads(Path(sys.argv[1]).read_text())
    now = datetime.now(timezone.utc)
    report = {'evaluated_at': now.isoformat(), 'mode': 'test_only', 'paid_credits_used': 0,
              'input': manifest, 'mlb': [], 'tennis': [], 'unsupported': manifest['unsupported']}
    db = connect('review-input/mlb')
    # Refresh event identity from the existing free official collector; no paid keys.
    from sports_analytics_pipeline.collectors.base import MappingTeamResolver
    from sports_analytics_pipeline.collectors.http import UrllibTransport
    from sports_analytics_pipeline.collectors.mlb_official import MappingStadiumResolver, MlbScheduleCollector
    from sports_analytics_pipeline.collectors.storage import CollectorRepository
    from sports_analytics_pipeline.data.seed import alias_mapping, stadium_alias_mapping, stadium_provider_id_mapping
    try:
        schedule = MlbScheduleCollector(transport=UrllibTransport(timeout_seconds=20, attempts=2),
            team_resolver=MappingTeamResolver(alias_mapping(db,'mlb')),
            stadium_resolver=MappingStadiumResolver(stadium_alias_mapping(db,'mlb'),
                provider_ids=stadium_provider_id_mapping('mlb'))).collect_date(manifest['date'])
        report['mlb_schedule_refresh']={'stored':CollectorRepository(db).persist_mlb_schedule(schedule),
            'warnings':list(schedule.warnings)}
    except Exception as exc:
        report['mlb_schedule_refresh']={'error':f'{type(exc).__name__}: {exc}'}
    games = db.execute('''SELECT g.*, h.nickname home_name, a.nickname away_name FROM games g
        JOIN teams h ON h.team_id=g.home_team_id JOIN teams a ON a.team_id=g.away_team_id
        WHERE g.sport='mlb' ''').fetchall()
    for away, home, away_odds, home_odds in manifest['mlb']:
        item = {'event': f'{away} at {home}', 'video_odds': [away_odds, home_odds]}
        report['mlb'].append(item)
        matches = [g for g in games if g['away_name']==away and g['home_name']==home
                   and datetime.fromisoformat(g['kickoff_time'].replace('Z','+00:00')).astimezone(
                       __import__('zoneinfo').ZoneInfo('America/New_York')).date().isoformat()==manifest['date']]
        if len(matches)!=1:
            item['blocker'] = f'Exact event match required; stored matches={len(matches)}'
            continue
        game = matches[0]
        item['game_id'] = game['game_id']
        try:
            run = create_mlb_model_run(db, game_id=game['game_id'], prediction_time=now)
            item['model'] = dict(db.execute('SELECT * FROM model_runs WHERE model_run_id=?',(run,)).fetchone())
            item['stored_market_evaluation'] = evaluate_mlb_upcoming(db,as_of=now,game_id=game['game_id'],persist=False)
            # Isolate the requested video prices from other books in this disposable database.
            db.execute('DELETE FROM odds_snapshots WHERE game_id=?',(game['game_id'],))
            for team, opponent, price in [(game['away_team_id'],game['home_team_id'],away_odds),
                                          (game['home_team_id'],game['away_team_id'],home_odds)]:
                db.execute('''INSERT INTO odds_snapshots(game_id,sportsbook_key,market_key,period,
                    settlement_rule,outcome_team_id,opponent_team_id,decimal_odds,american_odds,
                    source_updated_at,collected_at,market_status,is_accessible,is_consensus_eligible)
                    VALUES (?,?,'moneylines','full_game','video_settlement_unverified',?,?,?,?,?,?,'open',1,0)''',
                    (game['game_id'],manifest['book'],team,opponent,decimal(price),price,
                     manifest['timestamps']['mlb'],manifest['timestamps']['mlb']))
            item['evaluation'] = evaluate_mlb_upcoming(db,as_of=now,game_id=game['game_id'],persist=False)
        except (ValueError, RuntimeError, sqlite3.Error) as exc:
            item['blocker'] = f'{type(exc).__name__}: {exc}'
    db.close()
    db = connect('review-input/multisport')
    events = db.execute('''SELECT e.*,h.full_name home_name,a.full_name away_name FROM multisport_events e
        JOIN multisport_participants h ON h.participant_id=e.home_participant_id
        JOIN multisport_participants a ON a.participant_id=e.away_participant_id''').fetchall()
    report['stored_tennis_model_inventory']=[dict(r) for r in db.execute(
        "SELECT sport,model_version,feature_version,status,trained_at FROM multisport_model_artifacts WHERE sport IN ('atp','wta')")]
    for sport, first, second, first_odds, second_odds, day in manifest['tennis']:
        item = {'event':f'{first} vs {second}', 'video_odds':[first_odds,second_odds]}
        report['tennis'].append(item)
        matches = [e for e in events if e['sport']==sport and
                   ((first.casefold() in e['home_name'].casefold() and second.casefold() in e['away_name'].casefold()) or
                    (first.casefold() in e['away_name'].casefold() and second.casefold() in e['home_name'].casefold())) and
                   datetime.fromisoformat(e['commence_time'].replace('Z','+00:00')).astimezone(
                       __import__('zoneinfo').ZoneInfo('America/New_York')).date().isoformat()==day]
        if len(matches)!=1:
            item['blocker'] = f'Exact event match required; stored matches={len(matches)}'
            continue
        event = matches[0]
        item['event_id']=event['event_id']
        try:
            model = latest_model(db,sport)
            if model is None:
                item['blocker']='No trained test-only model in stored database'
                continue
            item['model_version']=model['model_version']
            item['feature_version']=model['feature_version']
            try:
                if model['feature_version']==BASELINE_FEATURE_VERSION:
                    stored_prediction=create_market_baseline_prediction(db,event_id=event['event_id'],sport=sport,
                        as_of=now,stale_after_minutes=60)
                else:
                    stored_prediction=predict_event(db,event_id=event['event_id'],model_version=model['model_version'],
                        prediction_time=now,feature_cutoff=now)
                item['stored_market_evaluation']=asdict(evaluate_event(db,event_id=event['event_id'],
                    prediction_id=stored_prediction,as_of=now,accessible_bookmakers=(manifest['book'],),
                    consensus_bookmakers=(),capture_unqualified_test=True))
            except (ValueError,RuntimeError,sqlite3.Error) as exc:
                item['stored_market_blocker']=str(exc)
            db.execute('DELETE FROM multisport_odds_snapshots WHERE event_id=?',(event['event_id'],))
            for role in ['home','away']:
                name=event[role+'_name']
                price=first_odds if first.casefold() in name.casefold() else second_odds
                stamp=manifest['timestamps']['tennis']
                db.execute('''INSERT INTO multisport_odds_snapshots(event_id,sport,provider_sport_key,
                    sportsbook_key,sportsbook_title,market_key,provider_market_key,period,settlement_rule,
                    outcome_participant_id,outcome_name,outcome_role,decimal_odds,source_updated_at,collected_at,
                    market_status,test_only) VALUES (?,?,?,?,'Primary Sportsbook','match_moneyline','h2h','full_match',
                    'video_settlement_unverified',?,?,?,?,?,?,'open',1)''',
                    (event['event_id'],sport,event['provider_sport_key'],manifest['book'],
                     event[role+'_participant_id'],name,role,decimal(price),stamp,stamp))
            if model['feature_version']==BASELINE_FEATURE_VERSION:
                prediction=create_market_baseline_prediction(db,event_id=event['event_id'],sport=sport,
                    as_of=now,stale_after_minutes=60)
            else:
                prediction=predict_event(db,event_id=event['event_id'],model_version=model['model_version'],
                    prediction_time=now,feature_cutoff=now)
            item['prediction']=dict(db.execute('SELECT * FROM multisport_predictions WHERE prediction_id=?',(prediction,)).fetchone())
            item['evaluation']=asdict(evaluate_event(db,event_id=event['event_id'],prediction_id=prediction,
                as_of=now,accessible_bookmakers=(manifest['book'],),consensus_bookmakers=(),capture_unqualified_test=True))
        except (ValueError, RuntimeError, sqlite3.Error) as exc:
            item['blocker']=f'{type(exc).__name__}: {exc}'
    db.close()
    if manifest.get('nfl'):
        from sports_analytics_pipeline.modeling import run_phase4
        from sports_analytics_pipeline.evaluation import evaluate_upcoming
        db=connect('review-input/nfl')
        report['nfl']={'video':manifest['nfl']}
        games=db.execute("SELECT * FROM games WHERE sport='nfl' AND away_team_id='SF' AND home_team_id IN ('LA','LAR') AND season=2026").fetchall()
        matches=[g for g in games if datetime.fromisoformat(g['kickoff_time'].replace('Z','+00:00')).astimezone(
            __import__('zoneinfo').ZoneInfo('America/New_York')).date().isoformat()==manifest['date']]
        if len(matches)!=1:
            report['nfl']['blocker']=f'Exact NFL event match required; stored matches={len(matches)}'
        else:
            game=matches[0]
            report['nfl']['event']=dict(game)
            try:
                report['nfl']['stored_market_model']=run_phase4(db,as_of=now,game_id=game['game_id'],persist=True)
                report['nfl']['stored_market_evaluation']=evaluate_upcoming(db,as_of=now,game_id=game['game_id'],persist=False)
                db.execute('DELETE FROM odds_snapshots WHERE game_id=?',(game['game_id'],))
                v=manifest['nfl']; stamp=manifest['timestamps']['nfl']
                for team,opponent,points,price in [(game['away_team_id'],game['home_team_id'],v['spread'],v['away_odds']),
                        (game['home_team_id'],game['away_team_id'],-v['spread'],v['home_odds'])]:
                    db.execute('''INSERT INTO odds_snapshots(game_id,sportsbook_key,market_key,period,
                        settlement_rule,outcome_team_id,opponent_team_id,spread_points,decimal_odds,american_odds,
                        source_updated_at,collected_at,market_status,is_accessible,is_consensus_eligible)
                        VALUES (?,?,'spreads','full_game','video_settlement_unverified',?,?,?,?,?,?,?,'open',1,0)''',
                        (game['game_id'],manifest['book'],team,opponent,points,decimal(price),price,stamp,stamp))
                report['nfl']['video_model']=run_phase4(db,as_of=now,game_id=game['game_id'],persist=True)
                report['nfl']['video_evaluation']=evaluate_upcoming(db,as_of=now,game_id=game['game_id'],persist=False)
            except (ValueError,RuntimeError,sqlite3.Error) as exc:
                report['nfl']['blocker']=f'{type(exc).__name__}: {exc}'
        db.close()
    Path('video-bot-report.json').write_text(json.dumps(report,indent=2,default=str))
    print(json.dumps(report,indent=2,default=str))


if __name__=='__main__':
    main()
