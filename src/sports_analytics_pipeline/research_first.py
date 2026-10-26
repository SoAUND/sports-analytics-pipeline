"""Oct 8, 2026 research-first grade. Price and model gates do not block a pick."""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

RULE = 'sap-research-first-2026-10-08'
ESTIMATE_LABEL = 'Estimated combined (public books, not a book quote)'
EXPERIMENTAL = {'preseason', 'nations_league', 'friendly', 'friendlies', 'contender', 'contender_series'}
FRESH_MINUTES = {
    'official_status': 180, 'injury_availability': 180, 'lineup_or_participants': 180,
    'rest_travel': 720, 'statistics_analytics': 1440, 'transactions_or_changes': 720,
    'multi_book_prices': 60, 'line_movement': 60, 'weather_environment': 180, 'weigh_in_status': 360,
}


def _num(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def usable_evidence(row, as_of):
    """A point requires a verified, fresh source for this event or player."""
    accepted, gaps = {}, []
    for item in row.get('evidence') or []:
        category = item.get('category') or 'evidence'
        if item.get('status') != 'verified' or not item.get('source_url') or not item.get('source_key'):
            gaps.append(category + ' unverified')
            continue
        updated, collected = _time(item.get('source_updated_at')), _time(item.get('collected_at'))
        window = timedelta(minutes=FRESH_MINUTES.get(category, 180))
        fresh = updated and collected and updated <= collected <= as_of and as_of - window <= updated
        if not fresh:
            gaps.append(category + ' stale')
            continue
        if row.get('event_id') and item.get('event_id') not in {None, row.get('event_id')}:
            gaps.append(category + ' wrong event')
            continue
        if item.get('event_id') is None:
            gaps.append(category + ' event not matched')
            continue
        player = item.get('player') or item.get('participant')
        row_player = row.get('participant') or row.get('selection_name')
        if player and row_player and str(player).casefold() != str(row_player).casefold():
            gaps.append(category + ' wrong player')
            continue
        accepted[category] = item
    return accepted, gaps


def prop_history(row, as_of):
    """Five recent completed logs for this player, role, stat, and competition."""
    if row.get('market') != 'player_prop':
        return {'enough': True, 'hits': None, 'misses': None, 'pushes': None, 'games': 0}
    line = _num(row.get('line'))
    names = {str(row.get('participant') or '').casefold(), str(row.get('selection_name') or '').casefold()} - {''}
    stat = str(row.get('prop_stat') or '').casefold()
    competition = str(row.get('competition') or row.get('league') or '').casefold()
    direction = str(row.get('direction') or '').casefold()
    role = str(row.get('role') or '').casefold()
    seen, hits, misses, pushes = set(), 0, 0, 0
    logs = list(row.get('game_logs') or [])
    logs.extend((row.get('public_player_data') or {}).get('observations') or [])
    for game in logs:
        if not str(game.get('source_url') or '').strip() or game.get('actual') is None:
            continue
        if game.get('comparable') is not True or str(game.get('status') or '').casefold() != 'completed':
            continue
        if not role or str(game.get('role') or '').casefold() != role:
            continue
        if names and str(game.get('player') or '').casefold() not in names:
            continue
        if not stat or str(game.get('stat') or '').casefold() != stat:
            continue
        if not competition or str(game.get('competition') or '').casefold() != competition:
            continue
        played = _time(game.get('date') or game.get('source_updated_at'))
        if not played or not as_of - timedelta(days=45) <= played <= as_of:
            continue
        game_id = game.get('game_id') or game.get('date')
        if not game_id or game_id in seen:
            continue
        actual = _num(game.get('actual'))
        if actual is None or line is None or direction not in {'over', 'under'}:
            continue
        seen.add(game_id)
        if actual == line:
            pushes += 1
        elif (direction == 'over' and actual > line) or (direction == 'under' and actual < line):
            hits += 1
        else:
            misses += 1
    return {'enough': len(seen) >= 5, 'hits': hits, 'misses': misses, 'pushes': pushes, 'games': len(seen)}
    """Five recent comparable logs for this player, stat, and competition, graded on the selected side."""
    if row.get('market') != 'player_prop':
        return {'enough': True, 'hits': None, 'misses': None, 'pushes': None, 'games': 0}
    line = _num(row.get('line'))
    names = {str(row.get('participant') or '').casefold(), str(row.get('selection_name') or '').casefold()} - {''}
    stat = str(row.get('prop_stat') or '').casefold()
    competition = str(row.get('competition') or row.get('league') or '').casefold()
    direction = str(row.get('direction') or '').casefold()
    role = str(row.get('role') or '').casefold()
    seen, hits, misses, pushes = set(), 0, 0, 0
    logs = list(row.get('game_logs') or [])
    logs.extend((row.get('public_player_data') or {}).get('observations') or [])
    for game in logs:
        if game.get('source_url') is None or game.get('actual') is None or game.get('comparable') is False:
            continue
        if names and str(game.get('player') or '').casefold() not in names:
            continue
        if not stat or str(game.get('stat') or '').casefold() != stat:
            continue
        if not competition or str(game.get('competition') or '').casefold() != competition:
            continue
        if role and str(game.get('role') or role).casefold() != role:
            continue
        played = _time(game.get('date') or game.get('source_updated_at'))
        if not played or not as_of - timedelta(days=45) <= played <= as_of:
            continue
        game_id = game.get('game_id') or game.get('date')
        if not game_id or game_id in seen:
            continue
        actual = _num(game.get('actual'))
        if actual is None or line is None or direction not in {'over', 'under'}:
            continue
        seen.add(game_id)
        over_side = actual > line
        under_side = actual < line
        if actual == line:
            pushes += 1
        elif (direction == 'over' and over_side) or (direction == 'under' and under_side):
            hits += 1
        else:
            misses += 1
    return {'enough': len(seen) >= 5, 'hits': hits, 'misses': misses, 'pushes': pushes, 'games': len(seen)}

def score_research(row, as_of):
    """Score only sourced findings. A missing factor is 0 and named."""
    by, identity_gaps = usable_evidence(row, as_of)
    missing = list(dict.fromkeys(identity_gaps))
    open_items = []
    lineup = by.get('lineup_or_participants') or {}
    official = by.get('official_status') or {}
    probable = str(row.get('starter_status') or row.get('goalie_status') or lineup.get('status_label') or '').casefold() in {'probable', 'expected', 'projected'}
    if official.get('official') is True and lineup.get('official') is True and not probable:
        information = 25
    elif probable and (official or lineup):
        information = 15
        open_items.append('probable or unconfirmed role')
    elif by.get('injury_availability'):
        information = 5
        open_items.append('material availability unknown')
    else:
        information = 0
        missing.append('information')
    stats = by.get('statistics_analytics')
    gaps = list(row.get('advanced_metric_gaps') or [])
    if stats and not gaps and (row.get('advanced_metrics') or stats.get('sport_specific') is True):
        advanced = 25
    elif stats:
        advanced = 8
        missing.append('sport-specific advanced matchup')
    else:
        advanced = 0
        missing.append('advanced stats')
    situational = 15 if by.get('rest_travel') else 0
    if situational == 0:
        missing.append('situational')
    trend = row.get('trend') or {}
    sample = _num(trend.get('sample_size'))
    span = _num(trend.get('season_span'))
    trend_time = _time(trend.get('source_updated_at'))
    if sample is not None and sample >= 8 and trend.get('same_competition') is True and span is not None and 2 <= span <= 3 and trend.get('source_url') and trend_time and as_of - timedelta(hours=24) <= trend_time <= as_of:
        trends = 10
    else:
        trends = 0
        missing.append('trends')
    books = int(row.get('matching_book_count') or 0)
    if by.get('line_movement') and books >= 2:
        market = 15
    elif books >= 2:
        market = 8
        missing.append('line movement')
    elif books == 1:
        market = 4
        missing.append('multi-book market read')
    else:
        market = 0
        missing.append('market read')
    source_keys = {item.get('source_key') for item in by.values() if item.get('source_key')}
    source_coverage = 10 if len(source_keys) >= 3 else 0
    if source_coverage == 0:
        missing.append('source coverage')
    if row.get('line_split'):
        open_items.append('book split: ' + str(row.get('line_split')))
    if row.get('price_scope') == 'public_reference':
        open_items.append('Primary-book price unverified')
    history = prop_history(row, as_of)
    if row.get('market') == 'player_prop' and not history['enough']:
        missing.append('five comparable games')
    base = information + advanced + situational + trends + market + source_coverage
    bonus = 0
    edge = _num(row.get('probability_edge'))
    ev = _num(row.get('ev_per_dollar'))
    if row.get('model_probability') and edge is not None and edge > 0:
        bonus += 5
        if ev is not None and ev > 0:
            bonus += 5
    score = base + bonus
    phase = str(row.get('competition_phase') or row.get('league_status') or '').casefold()
    experimental = row.get('experimental_league') is True or phase in EXPERIMENTAL
    material = [item for item in open_items if item.startswith('probable') or item.startswith('material')]
    if row.get('market') == 'player_prop' and not history['enough']:
        tier, decision = 'watchlist', 'watchlist'
    elif experimental:
        tier, decision = 'test', 'experimental'
    elif score >= 80 and information == 25 and not material:
        tier, decision = 'tier1', 'official_pick'
    elif (score >= 70 and not material) or (score >= 80 and len(open_items) == 1 and not material):
        tier, decision = 'tier2', 'official_pick'
    elif score >= 60 and open_items:
        tier, decision = 'tier3', 'conditional_pick'
    elif 50 <= score <= 59:
        tier, decision = 'test', 'experimental'
    else:
        tier, decision = 'watchlist', 'watchlist'
    return {
        'rule_version': RULE,
        'research_score': score,
        'research_base': base,
        'price_bonus': bonus,
        'research_factors': {
            'information': information, 'advanced': advanced, 'situational': situational,
            'trends': trends, 'market_read': market, 'source_coverage': source_coverage,
        },
        'research_missing': missing,
        'open_items': open_items,
        'research_tier': tier,
        'publication_decision': decision,
        'confidence': {'tier1': 'Tier 1', 'tier2': 'Tier 2', 'tier3': 'Tier 3', 'test': 'Test-only'}.get(tier, 'Watchlist'),
        'edge_tier': tier,
        'prop_record': history,
    }


def apply_research_first(row, as_of):
    """Override the price and model publication gate. Audit gates stay on the row."""
    if row.get('data_error'):
        row['research_tier'] = 'pass'
        row['publication_decision'] = 'pass'
        row['open_items'] = [str(row['data_error'])]
        return row
    graded = score_research(row, as_of)
    row['legacy_decision'] = row.get('publication_decision')
    row.update(graded)
    row['honesty_label'] = row.get('confidence')
    if row.get('price_scope') == 'public_reference':
        row['honesty_label'] += ' | Primary-book price unverified'
    if 'New material news' in str((row.get('gate_details') or {}).get('material_information') or ''):
        row['open_items'] = list(row.get('open_items') or []) + ['new material news requires tip-30 recheck']
        if row.get('research_score', 0) >= 60 and row.get('research_tier') != 'test' and (row.get('prop_record') or {}).get('enough', True):
            row['research_tier'] = 'tier3'
            row['publication_decision'] = 'conditional_pick'
            row['confidence'] = 'Tier 3'
    row['first_blocker'] = None if row.get('publication_decision') in {'official_pick', 'conditional_pick', 'experimental'} else (row.get('research_missing') or [None])[0]
    row['manual_wager_approval_required'] = True
    row['automatic_wagering'] = False
    return row

