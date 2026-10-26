import unittest
from datetime import timedelta
from tests.test_revised_strategy import NOW, evidence_candidate
from sports_analytics_pipeline.research_first import score_research
from sports_analytics_pipeline.revised_strategy import evaluate_research


class ResearchFirstTests(unittest.TestCase):
    def test_complete_sourced_row_is_tier1_without_approved_model(self):
        row = evaluate_research(evidence_candidate(), as_of=NOW)
        self.assertFalse(row['gates']['model_eligibility'])
        self.assertEqual(row['research_tier'], 'tier1')
        self.assertEqual(row['publication_decision'], 'official_pick')
        self.assertGreaterEqual(row['research_score'], 80)

    def test_missing_evidence_is_not_filled(self):
        row = evidence_candidate()
        row['evidence'] = []
        row['quotes'] = []
        graded = score_research(row, NOW)
        self.assertEqual(graded['research_factors']['information'], 0)
        self.assertIn('information', graded['research_missing'])
        self.assertEqual(graded['research_tier'], 'watchlist')

    def test_experimental_league_stays_test_only_with_score(self):
        row = evidence_candidate()
        row['competition_phase'] = 'preseason'
        graded = evaluate_research(row, as_of=NOW)
        self.assertEqual(graded['research_tier'], 'test')
        self.assertGreaterEqual(graded['research_score'], 80)

    def test_stale_evidence_scores_zero(self):
        row = evidence_candidate()
        for item in row['evidence']:
            item['source_updated_at'] = (NOW - timedelta(days=3)).isoformat()
        graded = evaluate_research(row, as_of=NOW)
        self.assertEqual(graded['research_factors']['information'], 0)
        self.assertTrue(any('stale' in item for item in graded['research_missing']))

    def test_prop_logs_must_be_recent_same_competition_and_side_aware(self):
        row = evidence_candidate()
        row.update(market='player_prop', prop_stat='points', direction='under', line=20.5, competition='nba', participant='Home Team', role='starter')
        row['game_logs'] = [
            {'game_id': f'g{n}', 'player': 'Home Team', 'role': 'starter', 'stat': 'points', 'competition': 'nba', 'date': NOW.isoformat(), 'actual': 18, 'status': 'completed', 'comparable': True, 'source_url': 'https://example.test/log'}
            for n in range(5)
        ]
        graded = evaluate_research(row, as_of=NOW)
        self.assertEqual(graded['prop_record']['hits'], 5)
        incomplete = dict(row['game_logs'][0])
        incomplete.update(game_id='open', status='in_progress', source_url='', comparable=None, role='')
        row['game_logs'] = [incomplete]
        self.assertEqual(evaluate_research(row, as_of=NOW)['prop_record']['games'], 0)
        old = dict(graded['prop_record'])
        self.assertEqual(old['misses'], 0)
        base = row['game_logs'][0] if False else {
            'player': 'Home Team', 'role': 'starter', 'stat': 'points', 'competition': 'nba',
            'actual': 18, 'status': 'completed', 'comparable': True, 'source_url': 'https://example.test/log'
        }
        row['game_logs'] = [
            {**base, 'game_id': 'old', 'date': (NOW - timedelta(days=60)).isoformat()},
            {**base, 'game_id': 'other', 'competition': 'gleague', 'date': NOW.isoformat()},
        ]
        short = evaluate_research(row, as_of=NOW)
        self.assertEqual(short['research_tier'], 'watchlist')
        short['gate_details'] = {'material_information': 'New material news requires lineup and downstream re-check'}
        from sports_analytics_pipeline.research_first import apply_research_first
        apply_research_first(short, NOW)
        self.assertEqual(short['research_tier'], 'watchlist')

        row = evidence_candidate()
        row.update(market='player_prop', prop_stat='points', direction='over', line=20.5)
        graded = evaluate_research(row, as_of=NOW)
        self.assertEqual(graded['research_tier'], 'watchlist')
        self.assertIn('five comparable games', graded['research_missing'])

        row = evidence_candidate()
        row['starter_status'] = 'probable'
        graded = evaluate_research(row, as_of=NOW)
        self.assertEqual(graded['research_tier'], 'tier3')
        self.assertIn('probable or unconfirmed role', graded['open_items'])
        self.assertNotEqual(graded['publication_decision'], 'pass')
