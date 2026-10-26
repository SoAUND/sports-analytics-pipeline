import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import unittest
from functools import partial
from math import isclose


def assert_close(actual, expected):
    assert isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9), (actual, expected)


def cases(names, values):
    def decorate(function):
        function.test_cases = [(v,) if ',' not in names else tuple(v) for v in values]
        return function
    return decorate

from sports_analytics_pipeline.value_models import price_shop, prop_distribution, prepare_value_candidate, shop_public_board
from sports_analytics_pipeline.revised_strategy import evaluate_research

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


def candidate():
    row = {'event_id': 'next', 'market': 'player_prop', 'period': 'full_game',
           'participant': 'player-1', 'prop_stat': 'points', 'direction': 'over',
           'line': 15.5, 'settlement_rule': 'points-including-ot', 'selection_name': 'Player over',
           'start_time': (NOW+timedelta(hours=3)).isoformat(), 'outcome_count': 2,
           'book_of_record': 'hardrockbet_fl', 'book_accessible': True,
           'decimal_odds': 1.9, 'price_time': NOW.isoformat(),
           'model': {'probability': .7, 'uncertainty': .03, 'push_probability': 0}}
    row['quotes'] = []
    for book, price in [('hardrockbet_fl', 1.9), ('draftkings', 2.1), ('fanduel', 2.05)]:
        row['quotes'].append({**{k:row[k] for k in ('event_id', 'market', 'period', 'participant', 'prop_stat', 'direction', 'line', 'settlement_rule')},
                              'book': book, 'status': 'open', 'source_url': 'https://example.test/'+book,
                              'source_updated_at': NOW.isoformat(), 'collected_at': NOW.isoformat(),
                              'selected_index': 0, 'outcomes': [
                                  {'name': 'Player over', 'direction': 'over', 'decimal_odds': price},
                                  {'name': 'Player under', 'direction': 'under', 'decimal_odds': 1.9}]})
    return row


def distribution_input(values=None):
    values = values if values is not None else [10, 14, 16, 18, 20, 22, 24, 26]
    evidence = {'status': 'verified', 'source_url': 'https://example.test/history',
                'source_updated_at': NOW.isoformat(), 'collected_at': NOW.isoformat()}
    return {'event_id': 'next', 'participant': 'player-1', 'prop_stat': 'points', 'period': 'full_game',
            'same_competition': True, 'comparable_role': True, 'role_confirmed': True,
            'competition': 'nba', 'role': 'starter', 'opportunity_unit': 'minutes',
            'stat_support': 'integer', 'history_evidence': dict(evidence),
            'role_evidence': dict(evidence), 'matchup_evidence': dict(evidence),
            'matchup_multiplier': 1, 'systematic_uncertainty': .03,
            'opportunity_scenarios': [{'opportunity': 30, 'weight': 1}],
            'observations': [{'event_id': str(i), 'played_at': (NOW-timedelta(days=i+1)).isoformat(),
                              'season': '2026', 'competition': 'nba', 'role': 'starter',
                              'participant': 'player-1', 'prop_stat': 'points', 'period': 'full_game', 'opportunity_unit': 'minutes',
                              'value': value, 'opportunity': 30} for i, value in enumerate(values)]}


def test_exact_price_shop_no_vig_and_ev():
    row = candidate()
    original = copy.deepcopy(row)
    result = price_shop(row, as_of=NOW)
    assert result['best_public_price']['book'] == 'draftkings'
    assert result['best_verified_execution_price']['book'] == 'hardrockbet_fl'
    assert result['consensus_is_independent_model'] is False
    assert result['matching_book_count'] == 3
    dk = result['best_public_price']
    assert_close(float(dk['no_vig_probability']), (1/2.1)/(1/2.1+1/1.9))
    assert_close(float(dk['ev_per_dollar']), .67*2.1-1)
    assert row == original


@cases('field,value', [('line', 16.5), ('prop_stat', 'rebounds'),
    ('period', 'first_half'), ('participant', 'player-2'), ('direction', 'under'),
    ('settlement_rule', 'no-ot')])
def test_mismatched_quotes_not_shopped(field, value):
    row = candidate()
    row['quotes'][1][field] = value
    result = price_shop(row, as_of=NOW)
    assert result['matching_book_count'] == 2
    assert result['best_public_price']['book'] == 'fanduel'


def test_stale_partial_conflicting_and_malformed_quotes_fail_individually():
    row = candidate()
    bad = copy.deepcopy(row['quotes'][0])
    bad['outcomes'][0]['decimal_odds'] = 2.5
    row['quotes'].append(bad)
    row['quotes'][1]['outcomes'].pop()
    row['quotes'][2]['source_updated_at'] = (NOW-timedelta(hours=2)).isoformat()
    row['quotes'].append({'book': 'malformed'})
    row['quotes'].append('not an object')
    result = price_shop(row, as_of=NOW)
    assert result['matching_book_count'] == 0
    assert len(result['rejected_quotes']) == 5


def test_market_consensus_cannot_generate_independent_ev():
    row = candidate()
    row.pop('model')
    result = price_shop(row, as_of=NOW)
    assert result['probability_source'] == 'missing'
    assert all('ev_per_dollar' not in x for x in result['quotes'])


def test_switch_requires_explicit_access_and_opt_in():
    row = candidate()
    assert prepare_value_candidate(row, as_of=NOW)['book_of_record'] == 'hardrockbet_fl'
    row['auto_shop_execution'] = True
    assert prepare_value_candidate(row, as_of=NOW)['book_of_record'] == 'hardrockbet_fl'
    row['accessible_books'] = ['draftkings']
    assert prepare_value_candidate(row, as_of=NOW)['book_of_record'] == 'draftkings'
    row['quotes'][1].pop('source_url')
    assert prepare_value_candidate(row, as_of=NOW)['book_of_record'] == 'hardrockbet_fl'


def test_over_under_and_push_sum_to_one():
    row = candidate()
    row.update(line=20, prop_distribution_input=distribution_input())
    result = prop_distribution(row, as_of=NOW)
    assert_close(result['over_probability'], 3/8)
    assert_close(result['under_probability'], 4/8)
    assert_close(result['push_probability'], 1/8)
    assert_close(sum(result[k] for k in ('over_probability', 'under_probability', 'push_probability')), 1)
    assert result['model']['approved'] is False
    assert result['conservative_conditional_probability'] < result['model']['probability']/(1-result['push_probability'])
    row['direction'] = 'under'
    assert_close(prop_distribution(row, as_of=NOW)['model']['probability'], .5)


def test_same_mean_different_consistency_changes_probability():
    row = candidate()
    row['prop_distribution_input'] = distribution_input([20]*8)
    stable = prop_distribution(row, as_of=NOW)
    row['prop_distribution_input'] = distribution_input([0, 40]*4)
    volatile = prop_distribution(row, as_of=NOW)
    assert stable['mean'] == volatile['mean']
    assert stable['over_probability'] > volatile['over_probability']
    assert stable['standard_deviation'] < volatile['standard_deviation']


def test_opportunity_role_and_matchup_affect_projection():
    row = candidate()
    row['prop_distribution_input'] = distribution_input()
    base = prop_distribution(row, as_of=NOW)
    row['prop_distribution_input']['opportunity_scenarios'] = [
        {'opportunity': 15, 'weight': .5}, {'opportunity': 30, 'weight': .5}]
    smaller_role = prop_distribution(row, as_of=NOW)
    assert smaller_role['mean'] < base['mean']
    row['prop_distribution_input']['matchup_multiplier'] = .8
    assert prop_distribution(row, as_of=NOW)['mean'] < smaller_role['mean']


def test_scenarios_do_not_inflate_evidence_sample_size():
    row = candidate()
    row['prop_distribution_input'] = distribution_input()
    base = prop_distribution(row, as_of=NOW)
    row['prop_distribution_input']['opportunity_scenarios'] = [{'opportunity':30, 'weight':.1}]*10
    result = prop_distribution(row, as_of=NOW)
    assert result['sample_size'] == 8
    assert_close(result['conservative_conditional_probability'], base['conservative_conditional_probability'])


@cases('mutation', ['identity', 'future', 'duplicate', 'role', 'weight', 'missing', 'old', 'competition', 'uncertainty', 'wrong_player', 'unit'])
def test_bad_distribution_is_blocked_not_replaced_by_old_average(mutation):
    row = candidate()
    data = row['prop_distribution_input'] = distribution_input()
    if mutation == 'identity': data['prop_stat'] = 'rebounds'
    if mutation == 'future': data['observations'][0]['played_at'] = (NOW+timedelta(days=1)).isoformat()
    if mutation == 'duplicate': data['observations'][1]['event_id'] = data['observations'][0]['event_id']
    if mutation == 'role': data['role_confirmed'] = False
    if mutation == 'weight': data['opportunity_scenarios'][0]['weight'] = .5
    if mutation == 'missing': data.pop('matchup_evidence')
    if mutation == 'old': data['observations'][0]['played_at'] = (NOW-timedelta(days=1200)).isoformat()
    if mutation == 'competition': data['observations'][0]['competition'] = 'wnba'
    if mutation == 'uncertainty': data['systematic_uncertainty'] = 0
    if mutation == 'wrong_player': data['observations'][0]['participant'] = 'someone_else'
    if mutation == 'unit': data['observations'][0]['opportunity_unit'] = 'possessions'
    result = prepare_value_candidate(row, as_of=NOW)
    assert result['prop_distribution']['status'] == 'blocked'
    assert 'model' not in result


def test_distribution_integrates_into_research_evaluation_without_promoting_model():
    row = candidate()
    row.update(sport='nba', home_name='Home', away_name='Away', market_status='open',
               prop_distribution_input=distribution_input())
    result = evaluate_research(row, as_of=NOW)
    assert result['prop_distribution']['status'] == 'computed_unvalidated'
    assert 'model_probability' in result
    assert result['model']['approved'] is False
    assert result['publication_decision'] in {'pass','official_pick','conditional_pick','watchlist','experimental'}  # Other required research remains absent.
    assert result['price_shopping']['matching_book_count'] == 3


def test_stochastic_integer_rounding_preserves_mean_and_pushes():
    row = candidate()
    row.update(line=10, prop_distribution_input=distribution_input([21]*8))
    row['prop_distribution_input']['opportunity_scenarios'] = [{'opportunity':15, 'weight':1}]
    result = prop_distribution(row, as_of=NOW)
    assert_close(result['mean'], 10.5)
    assert_close(result['push_probability'], .5)
    assert Decimal(str(result['model']['probability'])) + Decimal(str(result['push_probability'])) <= 1


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name, function in sorted(globals().items()):
        if name.startswith('test_') and callable(function):
            for args in getattr(function, 'test_cases', [()]):
                run = partial(function, *args)
                run.__name__ = name
                suite.addTest(unittest.FunctionTestCase(run,
                    description=name + (str(args) if args else '')))
    return suite


def test_board_inventory_shops_all_returned_lines_without_model_or_access():
    event = {'id':'event', 'sport_key':'basketball_nba', 'home_team':'Home', 'away_team':'Away',
             'commence_time':(NOW+timedelta(hours=2)).isoformat(), 'bookmakers':[]}
    for book, price in [('a',2), ('b',2.2)]:
        event['bookmakers'].append({'key':book, 'last_update':NOW.isoformat(), 'markets':[
            {'key':'h2h','outcomes':[{'name':'Home','price':price},{'name':'Away','price':1.8}]},
            {'key':'spreads','outcomes':[{'name':'Home','point':-2.5,'price':price},{'name':'Away','point':2.5,'price':1.8}]},
            {'key':'totals','outcomes':[{'name':'Over','point':220.5,'price':price},{'name':'Under','point':220.5,'price':1.8}]}]})
    result = shop_public_board([event], as_of=NOW)
    assert len(result) == 6
    assert all(r['publication_decision']=='inventory_only' for r in result)
    assert all(r['price_shopping']['matching_book_count']==2 for r in result)
    assert all(r['price_shopping']['best_verified_execution_price'] is None for r in result)
    assert all(r['price_shopping']['probability_source']=='missing' for r in result)
    event['commence_time'] = (NOW-timedelta(minutes=1)).isoformat()
    assert shop_public_board([event], as_of=NOW) == []


def test_soccer_incomplete_three_way_group_does_not_hide_draw():
    event = {'id':'event', 'sport_key':'soccer_epl', 'commence_time':(NOW+timedelta(hours=2)).isoformat(),
             'bookmakers':[{'key':'a','last_update':NOW.isoformat(),'markets':[{'key':'h2h',
                'outcomes':[{'name':'Home','price':2},{'name':'Away','price':2}]}]}]}
    assert shop_public_board([event], as_of=NOW) == []
