"""Our October 1 amendment: research publishing, never real-money execution.

Adapter-neutral evidence contract supports every approved sport and market. Missing
probabilities, provenance, quotes or facts fail closed. Historical paper ledgers
retain their original types; publication metadata is a separate, auditable layer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from statistics import median
from zoneinfo import ZoneInfo

from .deep_analysis import advanced_metric_gaps, audit_candidate, report_section, soccer_outcome_gaps, STRATEGY_STAGES
from .value_models import exact_market_key, matching_quotes, prepare_value_candidate

D = Decimal
UTC = timezone.utc
ET = ZoneInfo('America/New_York')
VERSION = 'sap-research-first-2026-10-08'
SPORTS = {'nfl': '🏈 NFL', 'cfb': '🏈 CFB', 'mlb': '⚾ MLB', 'nba': '🏀 NBA',
          'wnba': '🏀 WNBA', 'nhl': '🏒 NHL', 'epl': '⚽ Premier League',
          'mma': '🥊 UFC/MMA', 'atp': '🎾 ATP', 'wta': '🎾 WTA'}
MARKETS = {'moneyline', 'moneylines', 'three_way_moneyline', 'match_moneyline',
           'spreads', 'totals', 'player_prop'}
GATES = ('research', 'current_price', 'market_match', 'material_information',
         'model_eligibility', 'conservative_probability', 'required_edge',
         'positive_ev', 'acceptable_price', 'conflict_integrity', 'exposure',
         'manual_approval')


def timestamp(value):
    t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if t.tzinfo is None:
        raise ValueError('Timestamp must include a timezone')
    return t.astimezone(UTC)


def et_time(value):
    return timestamp(value).astimezone(ET).strftime('%b %d, %I:%M %p ET')


def decimal(value):
    v = D(str(value))
    if not v.is_finite():
        raise ValueError('Non-finite number')
    return v


def american(value):
    odds = decimal(value)
    if odds <= 1:
        raise ValueError('Decimal price must exceed 1')
    a = (odds - 1) * 100 if odds >= 2 else -100 / (odds - 1)
    return f'{int(a.to_integral_value()):+d}'


def publication_metadata(gates, score, confidence, *, validated=False, decision='test_only_selection'):
    """Classify the publishable card without rewriting a paper-tracking record."""
    blocked = decision == 'pass' or any(not value for key, value in gates.items()
        if key not in {'manual_approval', 'model_eligibility', 'pick_strength', 'confluence_complete'})
    if blocked:
        label, band = 'pass', 'Pass'
    elif gates.get('confluence_complete') is False:
        label, band = 'conditional_pick', 'Confluence shortfall'
    elif not validated:
        label, band = 'experimental', 'experimental (unvalidated model)'
    elif score < 75:
        label, band = 'experimental', 'thin edge'
    else:
        label, band = 'official_pick', 'High - strong edge' if score >= 85 else 'Medium - strong edge'
    return {'publication_decision': label, 'honesty_label': band,
            'manual_wager_approval_required': True, 'automatic_wagering': False,
            'publication_rule_version': VERSION}


def market_identity(row):
    """Every quoted outcome group must use exactly this event/line/period contract."""
    return exact_market_key(row)


def optional_settlement_review(row):
    """Use a disclosed model/market assumption when book grading is unreviewed."""
    result = {**row}
    if not result.get('settlement_rule') and result.get('sport') in SPORTS and result.get('market') in MARKETS and result.get('period'):
        key = 'assumed:' + ':'.join(str(result[k]) for k in ('sport', 'market', 'period'))
        result['settlement_rule'] = key
        result['settlement_assumed'] = True
        result['quotes'] = [{**q, 'settlement_rule': key, 'settlement_assumed': True}
                            if not q.get('settlement_rule') else {**q} for q in result.get('quotes', [])]
    if result.get('settlement_assumed') or any(q.get('settlement_assumed') for q in result.get('quotes', [])):
        result['settlement_assumed'] = True
        result['settlement_note'] = 'Settlement review skipped: assumes the quoted market follows the model score/stat definition; sportsbook grading unverified.'
    return result


def _evaluate_research_eligibility(row, *, as_of):
    """Evaluate a complete candidate from a timestamped, approved evidence adapter.

    Probabilities and uncertainty must name their model version and cutoff. Quote
    arrays contain every mutually exclusive outcome; selected_index identifies our
    bet. The contract is data, never instructions or a substitute for source checks.
    """
    row = optional_settlement_review(row)
    row = prepare_value_candidate(row, as_of=as_of)
    gates = {k: False for k in GATES}
    details = {}
    result = {**row, 'rule_version': VERSION, 'gates': gates, 'gate_details': details,
              'automatic_wagering': False, 'publication_decision': 'pass', 'honesty_label': 'Pass'}
    try:
        sport = str(row['sport']).casefold()
        if sport not in SPORTS or row['market'] not in MARKETS:
            raise ValueError('Unsupported sport or market')
        if not all(row.get(k) for k in ('event_id', 'home_name', 'away_name', 'period', 'selection_name', 'settlement_rule')):
            raise ValueError('Incomplete event or settlement identity')
        if row['market'] in {'spreads', 'totals', 'player_prop'} and row.get('line') is None:
            raise ValueError('Exact line missing')
        if row['market'] == 'player_prop':
            if row.get('prop_distribution', {}).get('status') == 'blocked':
                raise ValueError('Prop distribution blocked: ' + row['prop_distribution']['reason'])
            if not row.get('prop_stat') or row.get('direction') not in {'over', 'under'}:
                raise ValueError('Player prop statistic or direction missing')
        start = timestamp(row['start_time'])
        if as_of >= start:
            raise ValueError('Pregame window closed')
        book = str(row['book_of_record'])
        odds = decimal(row['decimal_odds'])
        price_time = timestamp(row['price_time'])
        gates['current_price'] = (D(1) + D(100)/D(300) <= odds <= D(11)
                                 and as_of - timedelta(minutes=60) <= price_time <= as_of
                                 and row.get('market_status') == 'open')
        gates['material_information'] = row.get('material_information_complete') is True
        gates['conflict_integrity'] = row.get('conflict_clear') is True
        gates['exposure'] = row.get('exposure_clear') is True
        gates['manual_approval'] = row.get('manual_wager_approved') is True
        required = ['official_status', 'injury_availability', 'lineup_or_participants',
                    'rest_travel', 'statistics_analytics', 'transactions_or_changes',
                    'multi_book_prices', 'line_movement']
        if sport in {'nfl', 'cfb', 'mlb', 'epl', 'atp', 'wta'}:
            required.append('weather_environment')
        if sport == 'mma':
            required.append('weigh_in_status')
        evidence = row.get('evidence', [])
        limits = {'official_status':180, 'injury_availability':90 if sport in {'nba','wnba'} else 360 if sport=='mma' else 180,
                  'lineup_or_participants':90 if sport in {'nba','wnba','epl'} else 180,
                  'rest_travel':720, 'statistics_analytics':1440, 'transactions_or_changes':360 if sport=='mma' else 720,
                  'weather_environment':180, 'weigh_in_status':360,
                  'multi_book_prices':60, 'line_movement':60}
        verified = {}
        for item in evidence:
            cat = item.get('category')
            if cat not in limits or not item.get('source_url') or not item.get('source_key'):
                continue
            updated, collected = timestamp(item['source_updated_at']), timestamp(item['collected_at'])
            if (item.get('status') == 'verified' and as_of-timedelta(minutes=limits[cat]) <= updated <= collected <= as_of):
                if cat in {'official_status','lineup_or_participants','weigh_in_status'} and item.get('official') is not True:
                    continue
                verified[cat] = item
        metric_gaps = advanced_metric_gaps(row)
        factors = row.get('factor_analysis') or {}
        modeled = set((row.get('model') or {}).get('factor_components', []))
        for name, item in factors.items():
            if item.get('model_component') not in modeled:
                metric_gaps.append(name+'_not_in_projection')
            try:
                if not as_of-timedelta(hours=24) <= timestamp(item['source_updated_at']) <= as_of:
                    metric_gaps.append(name+'_stale')
            except (KeyError, ValueError, TypeError):
                metric_gaps.append(name+'_timestamp')
        gates['research'] = (all(cat in verified for cat in required)
                             and len({v['source_key'] for v in verified.values()}) >= 3
                             and not metric_gaps)
        if row['market']=='player_prop':
            trend=row.get('trend',{})
            gates['research'] = gates['research'] and int(trend.get('sample_size',0))>=8 and trend.get('same_competition') is True and 0 < int(trend.get('season_span',0)) <= 3 and bool(trend.get('source_url'))
            gates['material_information'] = gates['material_information'] and row.get('role_confirmed') is True
        if row['market'] in {'spreads','totals','player_prop'} and row.get('line') is not None and decimal(row['line'])==decimal(row['line']).to_integral_value() and 'push_probability' not in row['model']:
            gates['material_information'] = False
            details['material_information'] = 'Whole-number line requires explicit push estimate'
        research_gaps = [cat for cat in required if cat not in verified]
        details['research'] = (', '.join(research_gaps + metric_gaps)
                               or 'Source categories and sport-specific advanced metrics checked')
        lineup = verified.get('lineup_or_participants')
        if lineup:
            lineup_at = timestamp(lineup['source_updated_at'])
            for cat in ['injury_availability', 'transactions_or_changes', 'official_status']:
                news = verified.get(cat)
                if news and news.get('material_change') and timestamp(news['source_updated_at']) > lineup_at:
                    gates['material_information'] = False
                    details['material_information'] = 'New material news requires lineup and downstream re-check'
        model = row['model']
        soccer_gaps = soccer_outcome_gaps(row)
        if soccer_gaps:
            raise ValueError('Soccer model must estimate home win, draw, and away win separately. Missing or unusable: ' + ', '.join(soccer_gaps) + '. No probability was invented.')
        p0, u, push = decimal(model['probability']), decimal(model['uncertainty']), decimal(model.get('push_probability',0))
        if not (0 <= p0 <= 1 and 0 <= u <= 1 and 0 <= push < 1 and p0 + push <= 1):
            raise ValueError('Invalid model probabilities')
        cutoff = timestamp(model['prediction_time'])
        gates['conservative_probability'] = bool(model.get('version') and model.get('uncertainty_method') and cutoff <= as_of and cutoff >= price_time and all(timestamp(v['collected_at']) <= cutoff for v in verified.values()))
        # Revised input model probability is unconditional win probability.
        factor_uncertainty = sum((decimal(v.get('uncertainty_effect', 0)) for v in factors.values()), D(0))
        u = max(u, factor_uncertainty)
        p = max(D(0), p0/(1-push) - u)
        q = 1/odds
        edge = p-q
        ev = (1-push) * (p*odds-1)
        minimum = 1/p if p>0 else D('Infinity')
        gates['required_edge'] = edge > 0
        gates['positive_ev'] = ev > 0
        gates['acceptable_price'] = odds > minimum
        gates['model_eligibility'] = model.get('approved') is True and int(model.get('oos_count',0)) >= 100 and model.get('calibration') == 'acceptable'
        if model.get('rejected') is True:
            raise ValueError('Rejected model cannot publish')
        quote_probs = []
        seen = set()
        record_found = False
        accepted_quotes, _ = matching_quotes(row, as_of=as_of)
        for quote in accepted_quotes:
            name = quote['book']
            quote_probs.append(decimal(quote['no_vig_probability']))
            seen.add(name)
            if name == book and decimal(quote['decimal_odds']) == odds:
                record_found = True
        public_reference = row.get('price_scope') == 'public_reference'
        gates['market_match'] = record_found and len(seen)>=2 and (row.get('book_accessible') is True or public_reference)
        details['market_match'] = f'{len(seen)} fresh matching books; four-book floor; book of record verified={record_found}'
        attempts = row.get('source_attempts', [])
        attempts_complete = bool(row.get('all_approved_sources_attempted') is True and attempts and all(x.get('book') and x.get('result') for x in attempts))
        stability = row.get('stability') == 'robust'
        if row.get('stability') in {'fragile','unstable'}:
            gates['conflict_integrity'] = False
            details['conflict_integrity'] = 'Edge fails reasonable sensitivity changes'
        # Same scoring contract as original, without invented narrative bonuses.
        edge_score = 20 if edge>=D('.10') else 18 if edge>=D('.08') else 16 if edge>=D('.07') else 14 if edge>=D('.06') else 12 if edge>=D('.05') else 8 if edge>=D('.03') else 4 if edge>0 else 0
        ev_score = 10 if ev>=D('.25') else 9 if ev>=D('.15') else 7 if ev>=D('.10') else 5 if ev>=D('.05') else 2 if ev>0 else 0
        n=int(model.get('oos_count',0))
        validation = (25 if n>=500 and model.get('stable') else 20 if n>=250 and model.get('clv')=='favorable' else 15) if gates['model_eligibility'] else 10 if n>0 else 0
        score=edge_score+ev_score+validation+(20 if gates['research'] else 0)+(15 if len(seen)>=3 else 10 if len(seen)==2 else 5 if len(seen)==1 else 0)+(10 if stability else 5)
        blocked=[key for key in GATES if key not in {'model_eligibility','manual_approval'} and not gates[key]]
        if blocked:
            decision='pass'
        elif public_reference or len(seen)<4 or not attempts_complete:
            decision='conditional_pick'
        elif not gates['model_eligibility'] or score<75 or row.get('settlement_assumed'):
            decision='experimental'
        else:
            decision='official_pick'
        honesty = 'experimental (unvalidated model)' if not gates['model_eligibility'] else 'strongest edge' if edge>=D('.05') and score>=85 else 'positive edge' if edge>0 else 'no edge'
        if row.get('settlement_assumed'):
            honesty += ' | settlement assumptions unverified'
        result.update(publication_decision=decision, honesty_label=honesty, score=score,
            confidence='High' if decision=='official_pick' and score>=85 and edge>=D('.05') else 'Medium' if decision=='official_pick' else 'Experimental' if decision=='experimental' else 'Conditional' if decision=='conditional_pick' else 'Pass',
            model_probability=str(p0), conservative_probability=str(p), push_probability=str(push), break_even_probability=str(q),
            probability_edge=str(edge), ev_per_dollar=str(ev), minimum_decimal_odds=str(minimum),
            consensus_probability=str(median(quote_probs)) if quote_probs else None,
            first_blocker=blocked[0] if blocked else 'execution_price_unverified' if public_reference else 'confluence' if decision=='conditional_pick' else None,
            manual_wager_approval_required=True, matching_book_count=len(seen),
            largest_risk=row.get('largest_risk'), recheck_time=row.get('recheck_time'),
            advanced_metric_gaps=metric_gaps,
            edge_tier="Strongest" if edge>=D(".05") and decision=="official_pick" and score>=85 else "Standard positive edge" if edge>0 else "Pass",
            analysis_level=len(STRATEGY_STAGES) if decision in {'official_pick','conditional_pick','experimental'} else 3)
        audit = audit_candidate(result)
        result['analysis_level'] = audit.level
        if decision == 'official_pick' and not audit.level30_complete:
            result.update(publication_decision='pass', confidence='Pass',
                          first_blocker=audit.first_blocker)
    except (KeyError, ValueError, TypeError, InvalidOperation, ZeroDivisionError) as exc:
        result.update(publication_decision='pass', first_blocker=str(exc))
    return result


def evaluate_research(row, *, as_of):
    """Evaluate eligibility plus every independent diagnostic, without early stop."""
    from .full_evaluation import full_evaluation
    # Keep disclosed settlement normalization even if downstream preparation fails.
    row = optional_settlement_review(row)
    try:
        result = _evaluate_research_eligibility(row, as_of=as_of)
    except (KeyError, ValueError, TypeError, ArithmeticError, AttributeError) as exc:
        result = {**row, 'publication_decision':'pass', 'automatic_wagering':False,
                  'first_blocker':'Candidate preparation failed: '+str(exc)}
    result['full_evaluation'] = full_evaluation(result, as_of=as_of)
    for key,value in result['full_evaluation']['computed_values'].items():
        if result.get(key) is None: result[key]=value
    blocker = str(result.get('first_blocker') or '')
    if 'Pregame window closed' in blocker or 'wrong game' in blocker.casefold() or 'Soccer model must estimate' in blocker or 'Rejected model' in blocker:
        result['data_error'] = blocker
    from .research_first import apply_research_first
    result = apply_research_first(result, as_of)
    result['evaluation_complete'] = True
    result['rule_version'] = VERSION
    return result


def literal_pick(row):
    """Plain bet text. A signed number is never left to stand for the market."""
    market = row.get('market', row.get('market_key'))
    name = str(row.get('selection_name', 'Unknown selection'))
    line = row.get('line', row.get('points'))
    sport = str(row.get('sport') or '')
    period = str(row.get('period') or 'full_game').replace('_', ' ')
    if market == 'spreads':
        kind = 'games spread' if sport in {'atp', 'wta'} else 'run line' if sport == 'mlb' else 'puck line' if sport == 'nhl' else 'point spread'
        if line is None:
            return f'{name} {kind} — line unavailable, {period}'
        signed = float(line)
        side = 'receives' if signed > 0 else 'lays' if signed < 0 else 'is pick'
        extra = ' This is the game handicap, not the total games.' if sport in {'atp', 'wta'} else ''
        return f'{name} {signed:+g} {kind}, {period}. {name} {side} {abs(signed):g}. Not a moneyline.{extra}'
    if market == 'totals':
        kind = 'total games' if sport in {'atp', 'wta'} else 'total runs' if sport == 'mlb' else 'total goals' if sport in {'nhl', 'epl'} or sport.startswith('soccer') else 'total points'
        return f'{name} {line} {kind}, {period}. This is the over/under, not a side spread.'
    if market == 'player_prop':
        return f"{name} {row.get('direction','')} {line} {row.get('prop_stat','')} prop, {period}".strip()
    return f'{name} moneyline, {period}. To win outright. Not a spread or total.'


def pick_line(row, *, grade=None):
    sport = SPORTS.get(str(row.get('sport','')).lower(), '🏅 '+str(row.get('sport','')).upper())
    home, away = row.get('home_name','Unknown'), row.get('away_name','Unknown')
    start = row.get('start_time', row.get('commence_time'))
    try:
        tip = et_time(start) if start else 'Start ET unavailable'
    except (ValueError, TypeError):
        tip = 'Start ET unavailable'
    try:
        price = american(row['decimal_odds']) if row.get('decimal_odds') else str(row.get('american_odds','Price unavailable'))
    except (ValueError, TypeError, InvalidOperation):
        price = 'Price unavailable'
    suffix = ''
    if grade:
        key=grade.upper();suffix=' - '+{'WIN':'✅ WIN','LOSS':'❌ LOSS','PUSH':'➖ PUSH','VOID':'↩️ VOID'}.get(key,key)
    try:
        pick = literal_pick(row)
    except (ValueError, TypeError):
        pick = 'Selection line unavailable'
    return f"{sport} | {away} vs {home} | {tip} | Bet: {pick} | Price: {price}{suffix}"


def research_card(rows, *, title='MIDDAY LOCK', as_of, tickets=None):
    from .research_first import ESTIMATE_LABEL
    rows = list(rows)
    tiers = {'tier1': [], 'tier2': [], 'tier3': [], 'test': [], 'watchlist': [], 'pass': []}
    for row in rows:
        tiers.setdefault(row.get('research_tier') or 'watchlist', []).append(row)
    period = [r for r in rows if str(r.get('period') or 'full_game') not in {'full_game', 'game', 'match'}]
    lines = [title, '', 'Research only. Any action still needs manual approval. No automatic execution.',
             'Checked: ' + et_time(as_of.isoformat()),
             f"Slate: {len(rows)} reviewed | Tier 1 {len(tiers['tier1'])} | Tier 2 {len(tiers['tier2'])} | Tier 3 {len(tiers['tier3'])} | Test {len(tiers['test'])}", '']
    if not tiers['tier1'] and not tiers['tier2']:
        lines.extend(['No official picks today.', ''])
    lines.extend(['BEST OF THE DAY', ''])
    best = [r for r in rows if r.get('research_tier') in {'tier1', 'tier2', 'tier3'}]
    best.sort(key=lambda r: ({'tier1': 0, 'tier2': 1, 'tier3': 2}.get(r.get('research_tier'), 9), -int(r.get('research_score') or 0)))
    if not best:
        lines.append('No current Tier 1-3 selection.')
    for row in best[:3]:
        lines.append(f"{row.get('confidence')} {row.get('research_score')}/100 | {pick_line(row)}")
    lines.append('')
    for name, key in (('TIER 1', 'tier1'), ('TIER 2', 'tier2'), ('TIER 3', 'tier3'), ('TEST-ONLY', 'test')):
        lines.extend([name, ''])
        shown = tiers[key][:3]
        if not shown:
            lines.append('None.')
        for row in shown:
            lines.append(pick_line(row))
            lines.append(f"Score: {row.get('research_score')}/100 | Open: {', '.join(row.get('open_items') or ['none'])}")
            if row.get('price_scope') == 'public_reference':
                lines.append('Primary-book price unverified')
            if row.get('probability_edge') is not None:
                lines.append('Price bonus only: edge ' + str(row.get('probability_edge')) + ' | EV ' + str(row.get('ev_per_dollar')))
            if row.get('settlement_note'):
                lines.append(row['settlement_note'])
        lines.append('')
    lines.extend(['PERIOD MARKETS', ''])
    if not period:
        lines.append('None.')
    for row in period[:3]:
        lines.append(pick_line(row))
    lines.extend(['', 'PLUS-MONEY PARLAYS', ''])
    published = 0
    for ticket in tickets or [t for r in rows for t in r.get('parlay_tickets', [])]:
        legs = ticket.get('legs', [])
        graded = evaluate_ticket(legs, decimal_odds=ticket.get('decimal_odds'), joint_probability=ticket.get('joint_probability'), correlation_method=ticket.get('correlation_method'), story=ticket.get('story'))
        if graded.get('decision') == 'pass' or not graded.get('estimated'):
            continue
        try:
            plus_money = decimal(graded.get('decimal_odds', 0)) >= 2
        except (ValueError, TypeError, InvalidOperation):
            continue
        if not plus_money:
            continue
        published += 1
        lines.append(ESTIMATE_LABEL + ' | ' + american(graded['decimal_odds']))
        lines.extend(pick_line(x) for x in legs)
    if not published:
        lines.append('No qualifying verified ticket.')
    lines.extend(['', f"Watchlist: {len(tiers['watchlist'])} | Passes: {len(tiers['pass'])}",
                  'Missing: ' + ', '.join(dict.fromkeys(item for row in rows for item in (row.get('research_missing') or [])[:1])) or 'none',
                  'Coverage: research-first grade controls; price and model validation are bonus only.'])
    return '\n'.join(lines)

def evaluate_ticket(legs, *, decimal_odds=None, joint_probability=None, correlation_method=None, story=None):
    """Tier 1-3 legs can form a ticket. Combined price is an estimate, not a book quote."""
    from .research_first import ESTIMATE_LABEL
    allowed = {'official_pick', 'conditional_pick', 'experimental'}
    if not legs or len(legs)>20 or any(x.get('publication_decision') not in allowed for x in legs):
        return {'decision':'pass', 'reason':'Every leg must be Tier 1-3 or test-only; maximum 20', 'estimated': False}
    identities = [market_identity(x) for x in legs]
    if len(set(identities)) != len(identities):
        return {'decision':'pass','reason':'Duplicate ticket leg'}
    if len({x['event_id'] for x in legs}) < len(legs):
        grouped={}
        for x in legs:grouped.setdefault(x['event_id'],set()).add(x.get('market',x.get('market_key')))
        if any('spreads' in m and any(k in m for k in ('moneyline','moneylines','match_moneyline')) for m in grouped.values()):
            return {'decision':'pass','reason':'Same-game moneyline plus spread is prohibited'}
        if not story:
            return {'decision':'pass','reason':'Same-game ticket requires a stated game-script story'}
    prices = []
    for leg in legs:
        try:
            prices.append(decimal(leg.get('decimal_odds')))
        except (ValueError, TypeError, InvalidOperation):
            return {'decision':'pass','reason':'A leg has no public price to estimate', 'estimated': False}
    odds = decimal(decimal_odds) if decimal_odds is not None else 1
    if decimal_odds is None:
        odds = 1
        for price in prices:
            odds *= price
    experimental = len(legs) > 6 or any(x.get('publication_decision') != 'official_pick' for x in legs)
    result = {'decision': 'experimental' if experimental else 'official_pick', 'honesty_label': ESTIMATE_LABEL,
              'estimated': True, 'leg_count': len(legs), 'story': story or 'not measured', 'decimal_odds': str(odds),
              'edge_tier': 'estimated combined'}
    if joint_probability is None:
        return result
    try:
        joint = decimal(joint_probability)
        if not 0 <= joint <= 1 or odds <= 1:
            raise ValueError('Invalid ticket probability or odds')
        ev = joint * odds - 1
        edge = joint - 1 / odds
        result.update(joint_probability=str(joint), ev_per_dollar=str(ev), probability_edge=str(edge),
                      edge_tier='price bonus only' if ev <= 0 or edge <= 0 else 'price bonus')
        return result
    except (ValueError, TypeError, InvalidOperation) as exc:
        return {'decision':'pass','reason':str(exc), 'estimated': False}


def result_card(legs):
    """Use actual placed-ticket legs and book settlement, including voids."""
    allowed={'WIN','LOSS','PUSH','VOID'}
    grades=[str(x['grade']).upper() for x in legs]
    if not legs or any(x not in allowed for x in grades):raise ValueError('All legs must be settled')
    wins=grades.count('WIN');losses=grades.count('LOSS')
    state='LOSS' if losses else 'WIN' if wins else 'PUSH' if 'PUSH' in grades else 'VOID'
    icon={'WIN':'✅','LOSS':'❌','PUSH':'➖','VOID':'↩️'}[state]
    return '\n'.join([f'{icon} {state} {wins}/{len(legs)}','',*[pick_line(x,grade=x['grade']) for x in legs],
                      '',f"Pushes: {grades.count('PUSH')} | Voids: {grades.count('VOID')}"])
