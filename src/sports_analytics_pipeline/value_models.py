"""Exact-market shopping and evidence-backed empirical prop distributions.

No network access, inferred injuries, synthetic history or real-money execution.
Consensus is a price benchmark, NEVER an independent probability model.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import floor, sqrt
from statistics import median

from .advanced_features import prop_input_ready


def _time(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return result.astimezone(timezone.utc)


def _number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Non-finite value')
    return result


def exact_market_key(row):
    keys = ('event_id', 'market', 'period', 'line', 'participant', 'settlement_rule')
    identity = tuple(row.get(k) for k in keys)
    if row.get('market') == 'player_prop':
        identity += (row.get('prop_stat'), str(row.get('direction', '')).lower())
    return identity


def matching_quotes(row, *, as_of):
    """Fresh complete outcome groups; conflicts at the same book fail closed."""
    valid, rejected = {}, []
    for quote in row.get('quotes', []):
        if not isinstance(quote, dict):
            rejected.append({'book': '', 'reason': 'quote must be an object'})
            continue
        book = str(quote.get('book') or '')
        try:
            if not book or exact_market_key(quote) != exact_market_key(row):
                raise ValueError('exact market mismatch')
            if row.get('market') == 'player_prop' and (
                    not row.get('prop_stat') or row.get('direction') not in {'over', 'under'}):
                raise ValueError('prop statistic/direction missing')
            updated, collected = _time(quote['source_updated_at']), _time(quote['collected_at'])
            if not as_of - timedelta(minutes=60) <= updated <= collected <= as_of:
                raise ValueError('stale or future quote')
            if quote.get('status') != 'open':
                raise ValueError('market not open')
            outcomes = quote['outcomes']
            if len(outcomes) != int(row['outcome_count']) or len(outcomes) < 2:
                raise ValueError('incomplete outcome group')
            names = [x['name'] for x in outcomes]
            if len(set(names)) != len(names):
                raise ValueError('duplicate outcomes')
            prices = [_number(x['decimal_odds']) for x in outcomes]
            if any(p <= 1 for p in prices):
                raise ValueError('invalid price')
            index = int(quote['selected_index'])
            if index < 0 or index >= len(prices) or names[index] != row['selection_name']:
                raise ValueError('selected outcome mismatch')
            if row.get('market') == 'player_prop':
                # Explicit sides prevent a second over price masquerading as the under.
                directions = [x.get('direction') for x in outcomes]
                if set(directions) != {'over', 'under'} or directions[index] != row['direction']:
                    raise ValueError('prop over/under outcome mismatch')
            raw = [1 / p for p in prices]
            record = {'book': book, 'decimal_odds': str(prices[index]),
                      'no_vig_probability': str(raw[index] / sum(raw)),
                      'overround': str(sum(raw) - 1), 'price_time': updated.isoformat(),
                      'source_url': quote.get('source_url'), 'quote': quote}
            valid.setdefault(book, []).append(record)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            rejected.append({'book': book, 'reason': str(exc)})
    result = []
    for book, records in valid.items():
        newest = max(r['price_time'] for r in records)
        latest = [r for r in records if r['price_time'] == newest]
        if len({(r['decimal_odds'], r['no_vig_probability']) for r in latest}) != 1:
            rejected.append({'book': book, 'reason': 'conflicting simultaneous quotes'})
            continue
        result.append(latest[0])
    return result, rejected


def price_shop(row, *, as_of):
    """Audit every exact quote. Accessibility is explicit, never inferred by book."""
    quotes, rejected = matching_quotes(row, as_of=as_of)
    model = row.get('model') or {}
    probability = None
    try:
        win, uncertainty = (_number(model[k]) for k in ('probability', 'uncertainty'))
        push = _number(model.get('push_probability', 0))
        if not (0 <= win <= 1 and 0 <= push < 1 and win + push <= 1 and 0 <= uncertainty <= 1):
            raise ValueError('invalid probabilities')
        factor_risk = sum((_number(x.get('uncertainty_effect', 0))
                           for x in row.get('factor_analysis', {}).values()), Decimal(0))
        probability = max(Decimal(0), win / (1 - push) - max(uncertainty, factor_risk))
    except (KeyError, ValueError, TypeError, ArithmeticError):
        push = Decimal(0)
    accessible = set(row.get('accessible_books', []))
    if row.get('book_accessible') is True and row.get('price_scope') != 'public_reference':
        accessible.add(row.get('book_of_record'))
    for record in quotes:
        odds = _number(record['decimal_odds'])
        # Quotes without provenance can benchmark but cannot trigger a book switch.
        record['execution_verified'] = record['book'] in accessible and bool(record['source_url'])
        record['break_even_probability'] = str(1 / odds)
        peers = [_number(x['no_vig_probability']) for x in quotes if x['book'] != record['book']]
        record['leave_one_book_out_consensus'] = str(median(peers)) if peers else None
        if probability is not None:
            record.update(probability_edge=str(probability - 1 / odds),
                          ev_per_dollar=str((1 - push) * (probability * odds - 1)))
    ranked = sorted(quotes, key=lambda q: (-_number(q['decimal_odds']), q['book']))
    public = [{k: v for k, v in q.items() if k != 'quote'} for q in ranked]
    execution = [q for q in public if q['execution_verified']]
    return {'status': 'complete' if len(quotes) >= 2 else 'insufficient_matching_books',
            'matching_book_count': len(quotes), 'quotes': public, 'rejected_quotes': rejected,
            'median_no_vig_consensus': str(median([_number(q['no_vig_probability']) for q in quotes])) if quotes else None,
            'best_public_price': public[0] if public else None,
            'best_verified_execution_price': execution[0] if execution else None,
            'minimum_decimal_odds': str(1 / probability) if probability else None,
            'probability_source': 'independent_model' if probability is not None else 'missing',
            'consensus_is_independent_model': False}


def _wilson_lower(probability, sample_size):
    # One-sided 95% Wilson lower bound. Mixture scenarios do not multiply sample size.
    z = 1.6448536269514722
    n = sample_size
    return max(0.0, (probability + z*z/(2*n) - z*sqrt(
        probability*(1-probability)/n + z*z/(4*n*n))) / (1 + z*z/n))


def prop_distribution(row, *, as_of):
    """Opportunity mixture over observed production rates; empirical, unvalidated.

    History must already be opponent/role-screened by a source adapter. Explicit
    matchup multipliers and opportunity scenarios are inputs, not generated facts.
    Integer outcomes use stochastic rounding, preserving expectation and pushes.
    """
    data = row['prop_distribution_input']
    if _time(row['start_time']) <= as_of:
        raise ValueError('Pregame window closed')
    for key in ('event_id', 'participant', 'prop_stat', 'period'):
        if data.get(key) != row.get(key) or not row.get(key):
            raise ValueError('Prop history identity mismatch: ' + key)
    if row.get('direction') not in {'over', 'under'}:
        raise ValueError('Prop direction must be over or under')
    if data.get('same_competition') is not True or data.get('comparable_role') is not True:
        raise ValueError('Prop history must match competition and role')
    if data.get('role_confirmed') is not True:
        raise ValueError('Prop role is not confirmed')
    for category, minutes in (('history', 1440), ('role', 90), ('matchup', 1440)):
        evidence = data[category + '_evidence']
        if evidence.get('status') != 'verified' or not evidence.get('source_url'):
            raise ValueError('Missing verified ' + category + ' evidence')
        if not as_of - timedelta(minutes=minutes) <= _time(evidence['source_updated_at']) <= _time(evidence['collected_at']) <= as_of:
            raise ValueError('Stale or future ' + category + ' evidence')
    history = data['observations']
    if not 8 <= len(history) <= 10000:
        raise ValueError('Prop distribution requires 8–10000 observations')
    events, seasons, rates = set(), set(), []
    for observation in history:
        if any(observation.get(k) != row.get(k) for k in ('participant', 'prop_stat', 'period')):
            raise ValueError('Observation player/statistic/period mismatch')
        if observation.get('opportunity_unit') != data['opportunity_unit'] or not data['opportunity_unit']:
            raise ValueError('Observation opportunity unit mismatch')
        event = observation['event_id']
        if event in events or event == row['event_id'] or _time(observation['played_at']) >= as_of or _time(observation['played_at']) > _time(data['history_evidence']['collected_at']):
            raise ValueError('Duplicate or non-historical prop observation')
        if _time(observation['played_at']) < as_of - timedelta(days=3*366):
            raise ValueError('Prop observation exceeds three-year history')
        if observation.get('competition') != data['competition'] or observation.get('role') != data['role']:
            raise ValueError('Observation competition/role mismatch')
        value, opportunity = float(_number(observation['value'])), float(_number(observation['opportunity']))
        if (value < 0 and data.get('stat_support') not in {'signed_integer', 'signed_continuous'}) or opportunity <= 0:
            raise ValueError('Invalid observed value/opportunity')
        events.add(event)
        seasons.add(observation['season'])
        rates.append(value / opportunity)
    if len(seasons) > 3:
        raise ValueError('Prop history exceeds three seasons')
    scenarios = data['opportunity_scenarios']
    if not scenarios or len(scenarios) > 100:
        raise ValueError('Missing or excessive opportunity scenarios')
    weights = [_number(s['weight']) for s in scenarios]
    if any(w <= 0 for w in weights) or abs(sum(weights) - 1) > Decimal('.000001'):
        raise ValueError('Opportunity weights must sum to one')
    multiplier = float(_number(data['matchup_multiplier']))
    risk = float(_number(data['systematic_uncertainty']))
    if not .25 <= multiplier <= 4 or not 0 < risk < 1:
        raise ValueError('Invalid matchup multiplier or missing uncertainty allowance')
    if data.get('stat_support') not in {'integer', 'continuous', 'signed_integer', 'signed_continuous'}:
        raise ValueError('Explicit statistic support required')
    histogram = {}
    for scenario, weight in zip(scenarios, weights):
        opportunity = float(_number(scenario['opportunity']))
        if opportunity < 0:
            raise ValueError('Negative projected opportunity')
        for rate in rates:
            value = rate * opportunity * multiplier
            mass = float(weight) / len(rates)
            if data['stat_support'] in {'integer', 'signed_integer'}:
                low = floor(value)
                parts = ((low, 1 - (value-low)), (low+1, value-low))
            else:
                parts = ((value, 1.0),)
            for outcome, fraction in parts:
                histogram[outcome] = histogram.get(outcome, 0.0) + mass * fraction
    line = float(_number(row['line']))
    over = sum(m for x, m in histogram.items() if x > line)
    under = sum(m for x, m in histogram.items() if x < line)
    push = sum(m for x, m in histogram.items() if x == line)
    win = over if row['direction'] == 'over' else under
    if push >= 1 - 1e-12:
        raise ValueError('All projected outcomes push')
    push = max(0.0, min(push, 1 - win))
    # Decimal evaluator requires the unconditional win+push mass not exceed 1.
    win = min(Decimal(str(win)), 1 - Decimal(str(push)))
    win = float(win)
    conditional = win / (1 - push)
    lower = max(0.0, _wilson_lower(conditional, len(rates)*(1-push)) - risk)
    def quantile(percentile):
        cumulative = 0.0
        for value, mass in sorted(histogram.items()):
            cumulative += mass
            if cumulative >= percentile:
                return value
        return max(histogram)
    return {'status': 'computed_unvalidated', 'method': 'empirical_opportunity_mixture_v1',
            'sample_size': len(rates), 'scenario_count': len(scenarios),
            'opportunity_unit': data['opportunity_unit'], 'season_span': len(seasons),
            'mean': sum(x*m for x, m in histogram.items()),
            'standard_deviation': sqrt(max(0, sum(x*x*m for x, m in histogram.items()) - sum(x*m for x, m in histogram.items())**2)),
            'p10': quantile(.1), 'median': quantile(.5), 'p90': quantile(.9),
            'over_probability': over, 'under_probability': under, 'push_probability': push,
            'conservative_conditional_probability': lower,
            'sources': [data[k+'_evidence']['source_url'] for k in ('history', 'role', 'matchup')],
            'model': {'version': 'empirical_opportunity_mixture_v1', 'probability': win,
                      'push_probability': push, 'uncertainty': conditional-lower,
                      'prediction_time': as_of.isoformat(),
                      'uncertainty_method': 'one-sided-95-Wilson plus explicit systematic risk; effective historical sample, not scenario count',
                      'approved': False, 'oos_count': 0, 'calibration': 'unvalidated',
                      'factor_components': data.get('factor_components', [])}}


def prepare_value_candidate(row, *, as_of):
    """Runtime entry point; independently supplied models remain untouched."""
    result = deepcopy(row)
    if row.get('market') == 'player_prop' and 'prop_distribution_input' in row:
        ready, reason = prop_input_ready(row)
        if not ready:
            result['prop_distribution'] = {'status': 'unavailable', 'reason': reason}
        else:
            try:
                distribution = prop_distribution(row, as_of=as_of)
                result['prop_distribution'] = distribution
                result['model'] = distribution['model']
                result['role_confirmed'] = True
                result['trend'] = {'sample_size': distribution['sample_size'],
                                   'season_span': distribution['season_span'], 'same_competition': True,
                                   'source_url': distribution['sources'][0]}
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                result['prop_distribution'] = {'status': 'blocked', 'reason': str(exc)}
                # A requested distribution must not silently fall back to an old average.
                result.pop('model', None)
    elif row.get('market') == 'player_prop':
        result['prop_distribution'] = {'status': 'external_model' if row.get('model') else 'missing',
                                       'reason': 'No empirical history/opportunity input supplied'}
    try:
        result['price_shopping'] = price_shop(result, as_of=as_of)
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        result['price_shopping'] = {'status': 'blocked', 'reason': str(exc),
                                  'best_verified_execution_price': None}
    if result.get('auto_shop_execution') is True:
        best = result['price_shopping']['best_verified_execution_price']
        if best:
            result['original_execution_quote'] = {k: result.get(k) for k in ('book_of_record', 'decimal_odds', 'price_time')}
            result.update(book_of_record=best['book'], decimal_odds=best['decimal_odds'],
                          price_time=best['price_time'], price_scope='verified_execution', book_accessible=True)
    return result


def shop_public_board(events, *, as_of):
    """Benchmark standard markets already fetched by discovery; zero extra requests.

    Provider settlement rules are unverified. This inventory is not a pick feed,
    does not manufacture models, and cannot certify execution accessibility.
    """
    groups = {}
    for event in events:
        try:
            if not event.get('id') or _time(event['commence_time']) <= as_of:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        for book in event.get('bookmakers', []):
            for market in book.get('markets', []):
                key = market.get('key')
                if key not in {'h2h', 'spreads', 'totals'}:
                    continue
                outcomes = market.get('outcomes', [])
                count = 3 if key == 'h2h' and (len(outcomes) == 3 or str(event.get('sport_key', '')).startswith('soccer_')) else 2
                if len(outcomes) != count:
                    continue
                for index, selected in enumerate(outcomes):
                    name = selected.get('name')
                    line = selected.get('point') if key != 'h2h' else None
                    if not name or (key != 'h2h' and line is None):
                        continue
                    if key == 'totals' and any(x.get('point') != line for x in outcomes):
                        continue
                    if key == 'spreads':
                        try:
                            if sum(_number(x['point']) for x in outcomes) != 0:
                                continue
                        except (KeyError, TypeError, ValueError, ArithmeticError):
                            continue
                    identity = {'event_id': event.get('id'), 'market': 'moneyline' if key == 'h2h' else key,
                                'period': 'provider_full_game', 'line': line, 'participant': name,
                                'settlement_rule': 'unverified_provider_market', 'selection_name': name,
                                'outcome_count': count}
                    group = groups.setdefault(exact_market_key(identity), {**identity, 'quotes': [],
                        'home_name': event.get('home_team'), 'away_name': event.get('away_team'),
                        'start_time': event.get('commence_time'), 'sport': event.get('sport_key'),
                        'price_scope': 'public_reference', 'book_accessible': False})
                    group['quotes'].append({**identity, 'book': book.get('key'), 'status': 'open',
                        'source_updated_at': market.get('last_update', book.get('last_update')),
                        'collected_at': as_of.isoformat(), 'selected_index': index,
                        'outcomes': [{'name': x.get('name'), 'decimal_odds': x.get('price')} for x in outcomes]})
    inventory = []
    for group in groups.values():
        shopping = price_shop(group, as_of=as_of)
        inventory.append({**group,
                          'price_shopping': shopping, 'publication_decision': 'inventory_only',
                          'missing': ['independent probability model', 'verified settlement and execution price', 'complete research']})
    return inventory
