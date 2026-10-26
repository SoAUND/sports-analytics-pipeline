import unittest
from datetime import datetime, timedelta, timezone

from sports_analytics_pipeline.advanced_features import apply_advanced_features, prop_input_ready, score_features
from sports_analytics_pipeline.connected_research import baseline_projection


AS_OF = datetime(2026, 10, 5, 22, 0, tzinfo=timezone.utc)


def observation(event, points, allowed, side='home', phase='F'):
    return {
        'event_id': event, 'played_at': '2026-09-01T20:00:00+00:00',
        'collected_at': '2026-09-02T04:00:00+00:00', 'competition_phase': phase,
        'score_for': points, 'score_against': allowed, 'home_away': side,
        'source_url': 'https://example.test/history',
    }


def history(home_n=8, away_n=8, phase='F'):
    return {
        'snapshot': {'competition_phase': phase, 'source_url': 'https://example.test/event'},
        'teams': {
            'home': {'name': 'New York Yankees', 'observations': [observation(f'h{i}', 4, 2, phase=phase) for i in range(home_n)]},
            'away': {'name': 'Tampa Bay Rays', 'observations': [observation(f'a{i}', 3, 3, 'away', phase) for i in range(away_n)]},
        },
    }


def row():
    return {
        'sport': 'mlb', 'market': 'moneyline', 'selection_name': 'New York Yankees',
        'home_name': 'New York Yankees', 'away_name': 'Tampa Bay Rays', 'participant': 'home',
    }


class AdvancedFeatureTests(unittest.TestCase):
    def test_score_feature_rejects_future_game(self):
        future = observation('x', 1, 1)
        future['played_at'] = (AS_OF + timedelta(hours=1)).isoformat()
        with self.assertRaises(ValueError):
            score_features([future], phase='F', as_of=AS_OF)

    def test_imbalanced_same_phase_widens_uncertainty_without_mixing(self):
        model = baseline_projection(row(), history(8, 8), as_of=AS_OF)
        uneven_model = dict(model, sample_size_per_side={'home': 1, 'away': 9}, uncertainty=model['uncertainty'])
        uneven = apply_advanced_features(uneven_model, history(8, 8), row(), as_of=AS_OF)
        self.assertGreater(uneven['uncertainty'], model['uncertainty'])
        self.assertEqual(uneven['factor_analysis']['phase_sample_balance']['probability_effect'], 0)
        self.assertFalse(uneven['approved'])
        self.assertEqual(uneven['oos_count'], 0)
        self.assertIn('Phases were not mixed', uneven['factor_analysis']['phase_sample_balance']['reason'])

    def test_shared_phase_feature_moves_probability_and_records_source(self):
        base = history()
        for side, value in (('home', 1.5), ('away', -0.5)):
            base['teams'][side]['advanced_metrics'] = {'expected_era': {
                'value': value, 'sample_size': 10, 'unit': 'runs', 'competition_phase': 'F',
                'collected_at': '2026-10-04T12:00:00+00:00', 'source_url': 'https://example.test/era',
            }}
        model = apply_advanced_features(baseline_projection(row(), base, as_of=AS_OF), base, row(), as_of=AS_OF)
        self.assertIn('expected_era', model['factor_analysis'])
        self.assertNotEqual(model['feature_probability_shift'], 0)
        self.assertEqual(model['factor_analysis']['expected_era']['provenance'], ['https://example.test/era'])
        self.assertFalse(model['approved'])
        self.assertLessEqual(abs(model['feature_probability_shift']), 0.06)

    def test_missing_feature_is_not_invented(self):
        model = apply_advanced_features(baseline_projection(row(), history(), as_of=AS_OF), history(), row(), as_of=AS_OF)
        self.assertIn('expected_era', model['advanced_metrics_missing'])
        self.assertNotIn('expected_era', model['factor_analysis'])

    def test_mixed_phase_baseline_still_refuses(self):
        mixed = history()
        mixed['teams']['away']['observations'][0]['competition_phase'] = 'R'
        with self.assertRaises(ValueError):
            baseline_projection(row(), mixed, as_of=AS_OF)

    def test_prop_input_requires_confirmed_role(self):
        ready, reason = prop_input_ready({'prop_distribution_input': {
            'history': [{}], 'opportunity_scenarios': [{}], 'matchup': {},
            'role': {'status': 'projected', 'source_url': 'https://example.test/role'},
        }})
        self.assertFalse(ready)
        self.assertIn('confirmed role', reason)


if __name__ == '__main__':
    unittest.main()
