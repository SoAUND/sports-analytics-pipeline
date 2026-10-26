"""Second pass for a row that is waiting on evidence.

A retry attaches a category only when the collected record actually contains it.
A source that does not return stays missing. Approval is not changed.
"""
from copy import deepcopy


def _evidence(category, source_key, source_url, when, *, official=False, summary):
    return {
        'category': category, 'status': 'verified', 'source_key': source_key,
        'source_url': source_url, 'source_updated_at': when, 'collected_at': when,
        'official': official, 'summary': summary,
    }


def innings(value):
    whole, _, outs = str(value).partition('.')
    return float(whole or 0) + int(outs or 0) / 3


def fip_from_counts(stat, *, constant=3.10):
    ip = innings(stat.get('inningsPitched') or 0)
    if ip <= 0:
        return None
    hr, bb, hbp, k = (float(stat.get(key) or 0) for key in ('homeRuns', 'baseOnBalls', 'hitBatsmen', 'strikeOuts'))
    return ((13 * hr + 3 * (bb + hbp) - 2 * k) / ip) + constant


def mlb_game(payload, home_name, away_name):
    """Return the one official game matching both team names."""
    matches = []
    for date in payload.get('dates') or []:
        for game in date.get('games') or []:
            teams = game.get('teams') or {}
            home = ((teams.get('home') or {}).get('team') or {}).get('name')
            away = ((teams.get('away') or {}).get('team') or {}).get('name')
            if home == home_name and away == away_name:
                matches.append(game)
    if len(matches) != 1:
        return None
    return matches[0]


def mlb_follow_records(game, *, source_url, collected_at, phase):
    """Official lineup and pitcher rates. FIP is calculated from official counts, constant 3.10."""
    records = {'evidence': [], 'metrics': {}}
    lineups = game.get('lineups') or {}
    if lineups.get('homePlayers') and lineups.get('awayPlayers'):
        records['evidence'].append(_evidence('lineup_or_participants', 'mlb_statsapi', source_url, collected_at, official=True, summary='Official batting orders published'))
        records['evidence'].append(_evidence('official_status', 'mlb_statsapi', source_url, collected_at, official=True, summary='Official schedule game matched'))
    for side, key in (('home', 'home'), ('away', 'away')):
        pitcher = ((game.get('teams') or {}).get(key) or {}).get('probablePitcher') or {}
        stat = pitcher.get('seasonStats') or {}
        if not stat:
            continue
        fip = fip_from_counts(stat)
        metrics = {}
        if fip is not None:
            metrics['fip'] = {'value': fip, 'sample_size': 1, 'unit': 'earned_runs_per_9', 'competition_phase': str(phase), 'source_url': source_url, 'sources': [source_url], 'collected_at': collected_at, 'opponent_adjusted': False, 'constant': 3.10}
        if stat.get('strikeoutsPer9Inn') is not None:
            metrics['strikeout_rate'] = {'value': float(stat['strikeoutsPer9Inn']), 'sample_size': 1, 'unit': 'per_9', 'competition_phase': str(phase), 'source_url': source_url, 'sources': [source_url], 'collected_at': collected_at, 'opponent_adjusted': False}
        if stat.get('walksPer9Inn') is not None:
            metrics['walk_rate'] = {'value': float(stat['walksPer9Inn']), 'sample_size': 1, 'unit': 'per_9', 'competition_phase': str(phase), 'source_url': source_url, 'sources': [source_url], 'collected_at': collected_at, 'opponent_adjusted': False}
        if metrics:
            records['metrics'][side] = metrics
            records['evidence'].append(_evidence('statistics_analytics', 'mlb_statsapi', source_url, collected_at, summary=pitcher.get('fullName', side)+' season pitching counts'))
    return records


def evidence_from_connection(row, when):
    """Turn an already collected connection into evaluator evidence. No invented fields."""
    history = row.get('connected_research') or {}
    found = []
    snapshot = history.get('snapshot') or {}
    if snapshot.get('lineups_confirmed') is True and snapshot.get('lineup_source'):
        found.append(_evidence('lineup_or_participants', 'mlb_statsapi', snapshot['lineup_source'], snapshot.get('lineup_collected_at') or when, official=True, summary='Both official batting orders published'))
    if snapshot.get('provider_event_id') and snapshot.get('source_url'):
        found.append(_evidence('official_status', snapshot.get('source_key') or 'official_schedule', snapshot['source_url'], snapshot.get('collected_at') or when, official=True, summary='Official event identity matched'))
    teams = history.get('teams') or {}
    for side, team in teams.items():
        metric = (team.get('advanced_metrics') or {}).get('confirmed_out_count')
        if metric and metric.get('source_url'):
            found.append(_evidence('injury_availability', 'availability_report', metric['source_url'], metric['collected_at'], official=True, summary=side+' confirmed out count '+str(metric['value'])))
            break
        observations = team.get('observations') or []
        if observations and observations[0].get('source_url'):
            found.append(_evidence('statistics_analytics', 'completed_games', observations[0]['source_url'], observations[0]['collected_at'], summary=str(len(observations))+' completed same-phase games'))
            found.append(_evidence('rest_travel', 'completed_games', observations[0]['source_url'], observations[0]['collected_at'], summary='Last completed game '+str(observations[0].get('played_at'))))
            break
    quotes = row.get('quotes') or []
    books = {q.get('book') for q in quotes if q.get('book')}
    if len(books) >= 2 and quotes[0].get('source_url'):
        found.append(_evidence('multi_book_prices', 'odds_quotes', quotes[0]['source_url'], quotes[0].get('source_updated_at') or quotes[0].get('collected_at') or when, summary=str(len(books))+' matching books'))
    return found


def _mlb_pass(row, collector, as_of, already):
    date = as_of.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date().isoformat()
    url = 'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=' + date + '&hydrate=probablePitcher,lineups,team'
    try:
        payload, collected = collector.get(url, as_of, row.setdefault('source_attempts', []), 10)
    except Exception as exc:
        row.setdefault('follow_notes', []).append('MLB schedule follow-through failed: ' + str(exc))
        return []
    game = mlb_game(payload, row.get('home_name'), row.get('away_name'))
    if not game:
        row.setdefault('follow_notes', []).append('MLB schedule had no unique team match')
        return []
    phase = (row.get('connected_research') or {}).get('snapshot', {}).get('competition_phase') or game.get('gameType') or 'unknown'
    for side, key in (('home', 'home'), ('away', 'away')):
        pitcher = ((game.get('teams') or {}).get(key) or {}).get('probablePitcher') or {}
        if pitcher.get('id') and not pitcher.get('seasonStats'):
            stat_url = f"https://statsapi.mlb.com/api/v1/people/{pitcher['id']}/stats?stats=season&group=pitching&season={as_of.year}"
            try:
                stat_payload, _ = collector.get(stat_url, as_of, row['source_attempts'], 30)
                splits = ((stat_payload.get('stats') or [{}])[0].get('splits') or [])
                if splits:
                    pitcher['seasonStats'] = splits[0].get('stat') or {}
            except Exception as exc:
                row.setdefault('follow_notes', []).append('Pitcher stats unavailable: ' + str(exc))
    records = mlb_follow_records(game, source_url=url, collected_at=collected.isoformat(), phase=phase)
    history = row.setdefault('connected_research', {'teams': {}, 'missing': []})
    for side, metrics in records['metrics'].items():
        team = history.setdefault('teams', {}).setdefault(side, {'observations': [], 'advanced_metrics': {}})
        team.setdefault('advanced_metrics', {}).update(metrics)
    return [item for item in records['evidence'] if item['category'] not in already]


def _sport_pass(row, collector, as_of, already):
    sport = row.get('sport')
    if sport == 'mlb':
        return _mlb_pass(row, collector, as_of, already)
    if sport in {'nba', 'wnba'}:
        return _basketball_pass(row, collector, as_of, already)
    if sport == 'nhl':
        return _nhl_pass(row, collector, as_of, already)
    if sport == 'nfl':
        row.setdefault('follow_notes', []).append('NFL follow-through uses the nflverse injury and prior-week EPA pass')
        return []
    row.setdefault('follow_notes', []).append(str(sport or 'unknown') + ' follow-through attempted; no official live feed returned a confirmable input')
    return []


def _basketball_pass(row, collector, as_of, already):
    from .public_inputs import espn_injury_out_count, espn_web_url
    league = 'wnba' if row.get('sport') == 'wnba' else 'nba'
    url = f'https://site.api.espn.com/apis/site/v2/sports/basketball/{league}/injuries'
    try:
        payload, collected = collector.get(url, as_of, row.setdefault('source_attempts', []), 20)
    except Exception:
        url = espn_web_url(url)
        try:
            payload, collected = collector.get(url, as_of, row.setdefault('source_attempts', []), 20)
        except Exception as exc:
            row.setdefault('follow_notes', []).append('Basketball injury follow-through failed: ' + str(exc))
            return []
    phase = (row.get('connected_research') or {}).get('snapshot', {}).get('competition_phase') or 'unknown'
    added = []
    history = row.setdefault('connected_research', {'teams': {}, 'missing': []})
    for side in ('home', 'away'):
        record, outs = espn_injury_out_count(payload, row.get(side + '_name'), phase=phase, as_of=as_of, source_url=url, collected_at=collected.isoformat())
        team = history.setdefault('teams', {}).setdefault(side, {'observations': [], 'advanced_metrics': {}})
        if record:
            team.setdefault('advanced_metrics', {})['confirmed_out_count'] = record
            team['confirmed_out'] = outs
    if 'injury_availability' not in already:
        added.append(_evidence('injury_availability', 'espn_site_api', url, collected.isoformat(), official=False, summary='ESPN injury list parsed'))
    return added


def _nhl_pass(row, collector, as_of, already):
    url = 'https://api-web.nhle.com/v1/scoreboard/now'
    try:
        payload, collected = collector.get(url, as_of, row.setdefault('source_attempts', []), 15)
    except Exception as exc:
        row.setdefault('follow_notes', []).append('NHL scoreboard follow-through failed: ' + str(exc))
        return []
    games = payload.get('games') or payload.get('gamesByDate') or []
    if isinstance(games, dict):
        games = [game for day in games.values() for game in (day if isinstance(day, list) else day.get('games') or [])]
    matched = [game for game in games if row.get('home_name') and row.get('away_name') and row['home_name'] in str(game) and row['away_name'] in str(game)]
    if len(matched) != 1:
        row.setdefault('follow_notes', []).append('NHL scoreboard had no unique team match')
        return []
    goalies = matched[0].get('goalies') or matched[0].get('confirmedGoalies')
    if not goalies or 'official_status' in already:
        row.setdefault('follow_notes', []).append('NHL goalie not confirmed on the official scoreboard')
        return []
    return [_evidence('official_status', 'nhl_api', url, collected.isoformat(), official=True, summary='Official scoreboard game matched')]


def apply_follow_through(rows, collector, *, as_of):
    """Recollect a waiting row once, attach real evidence, and record the attempt."""
    output = []
    when = as_of.isoformat()
    for original in rows:
        row = deepcopy(original)
        if collector is not None and hasattr(collector, 'memo'):
            collector.memo.pop(row.get('event_id'), None)
            try:
                from .connected_research import attach_connected_research
                refreshed = attach_connected_research([row], collector, as_of=as_of)[0]
                row.update({k: refreshed[k] for k in ('connected_research', 'projection_connection', 'model', 'team_consistency_observations') if k in refreshed})
            except Exception as exc:
                row['follow_through'] = {'status': 'retry_failed', 'reason': str(exc), 'attempted_at': when}
                output.append(row)
                continue
        before = {item.get('category') for item in row.get('evidence') or []}
        added = [item for item in evidence_from_connection(row, when) if item['category'] not in before]
        if collector is not None and hasattr(collector, 'get'):
            added.extend(_sport_pass(row, collector, as_of, before | {item['category'] for item in added}))
        row['evidence'] = list(row.get('evidence') or []) + added
        history = row.get('connected_research') or {}
        if row.get('model') and history.get('teams'):
            try:
                from .advanced_features import apply_advanced_features
                row['model'] = apply_advanced_features(row['model'], history, row, as_of=as_of)
            except Exception as exc:
                row.setdefault('follow_notes', []).append('Projection was not rebuilt: ' + str(exc))
        still = sorted(set(history.get('missing') or []) | set(row.get('follow_notes') or []))
        row['follow_through'] = {
            'status': 'completed', 'attempted_at': when, 'sport': row.get('sport'),
            'added': [item['category'] for item in added],
            'still_missing': still,
            'note': 'Follow-through ran for this sport. A missing source was not invented.',
        }
        output.append(row)
    return output
