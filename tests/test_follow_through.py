import unittest
from datetime import datetime, timezone

from sports_analytics_pipeline.follow_through import apply_follow_through, evidence_from_connection, fip_from_counts, mlb_game


AS_OF = datetime(2026, 10, 6, 14, tzinfo=timezone.utc)


class FollowThroughTests(unittest.TestCase):
    def test_published_lineup_becomes_evidence(self):
        row = {'connected_research': {'snapshot': {'lineups_confirmed': True, 'lineup_source': 'https://statsapi.mlb.com/box', 'lineup_collected_at': AS_OF.isoformat()}, 'teams': {}}}
        categories = {item['category'] for item in evidence_from_connection(row, AS_OF.isoformat())}
        self.assertIn('lineup_or_participants', categories)

    def test_unpublished_lineup_is_not_invented(self):
        row = {'connected_research': {'snapshot': {'lineups_confirmed': False, 'lineup_source': 'https://statsapi.mlb.com/box'}, 'teams': {}}}
        self.assertFalse(any(item['category'] == 'lineup_or_participants' for item in evidence_from_connection(row, AS_OF.isoformat())))

    def test_fip_uses_official_counts_only(self):
        self.assertAlmostEqual(fip_from_counts({'inningsPitched': '9.0', 'homeRuns': 1, 'baseOnBalls': 3, 'hitBatsmen': 0, 'strikeOuts': 9}), ((13 + 9 - 18) / 9) + 3.10)

    def test_every_sport_records_follow_through(self):
        class Collector:
            def get(self, url, as_of, attempts, ttl=60):
                attempts.append(url)
                return {'injuries': []}, as_of
        rows = apply_follow_through([
            {'event_id': 'nba', 'sport': 'nba', 'home_name': 'A', 'away_name': 'B'},
            {'event_id': 'wta', 'sport': 'wta', 'home_name': 'A', 'away_name': 'B'},
        ], Collector(), as_of=AS_OF)
        self.assertEqual([row['follow_through']['status'] for row in rows], ['completed', 'completed'])
        self.assertTrue(any('basketball' in url for url in rows[0]['source_attempts']))
        self.assertIn('wta follow-through attempted', rows[1]['follow_notes'][0])
        payload = {'dates': [{'games': [{'teams': {'home': {'team': {'name': 'Atlanta Braves'}}, 'away': {'team': {'name': 'Los Angeles Dodgers'}}}}]}]}
        self.assertIsNotNone(mlb_game(payload, 'Atlanta Braves', 'Los Angeles Dodgers'))
        self.assertIsNone(mlb_game(payload, 'Atlanta Braves', 'New York Mets'))
        rows = apply_follow_through([{'event_id': '1', 'evidence': []}], None, as_of=AS_OF)
        self.assertEqual(rows[0]['follow_through']['status'], 'completed')
        self.assertIn('not invented', rows[0]['follow_through']['note'])


if __name__ == '__main__':
    unittest.main()
