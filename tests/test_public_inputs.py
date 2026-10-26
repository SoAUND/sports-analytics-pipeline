import unittest
from datetime import datetime, timezone

from sports_analytics_pipeline.public_inputs import espn_injury_out_count, espn_web_url, mlb_lineup_confirmed, nflverse_out_count, team_epa_per_play


AS_OF = datetime(2026, 10, 5, 23, tzinfo=timezone.utc)
CSV = """season,game_type,team,week,gsis_id,full_name,report_status,date_modified
2026,REG,ATL,4,1,Player Out,Out,2026-10-04T12:00:00+00:00
2026,REG,ATL,4,2,Player Q,Questionable,2026-10-04T12:00:00+00:00
2026,REG,NO,4,3,Other Out,Out,2026-10-04T12:00:00+00:00
2026,POST,ATL,4,4,Wrong Phase,Out,2026-10-04T12:00:00+00:00
"""


class PublicInputTests(unittest.TestCase):
    def test_out_count_ignores_questionable_and_other_phase(self):
        metric, names = nflverse_out_count(CSV, 'ATL', week=4, game_type='REG', phase='2', as_of=AS_OF,
                                            source_url='https://example.test/injuries', collected_at='2026-10-04T18:00:00+00:00')
        self.assertEqual(metric['value'], 1)
        self.assertEqual(names, ['Player Out'])
        self.assertEqual(metric['competition_phase'], '2')

    def test_future_injury_row_is_excluded(self):
        metric, names = nflverse_out_count(CSV, 'NO', week=4, game_type='REG', phase='2',
                                            as_of=datetime(2026, 10, 1, tzinfo=timezone.utc),
                                            source_url='https://example.test/injuries', collected_at='2026-10-04T18:00:00+00:00')
        self.assertEqual(metric['value'], 0)
        self.assertEqual(names, [])

    def test_espn_out_count_matches_team(self):
        payload = {'injuries': [{'team': {'displayName': 'Atlanta Falcons'},
                                  'injuries': [{'status': 'Out', 'athlete': {'displayName': 'A'}}]}]}
        metric, names = espn_injury_out_count(payload, 'Atlanta Falcons', phase='2', as_of=AS_OF,
                                               source_url='https://example.test/espn', collected_at='2026-10-05T12:00:00+00:00')
        self.assertEqual(names, ['A'])
        self.assertEqual(metric['unit'], 'players')

    def test_lineup_requires_both_orders(self):
        self.assertFalse(mlb_lineup_confirmed({'teams': {'home': {'battingOrder': [1]}, 'away': {}}}))
        self.assertTrue(mlb_lineup_confirmed({'teams': {'home': {'battingOrder': [1]}, 'away': {'battingOrder': [2]}}}))

    def test_epa_uses_prior_weeks_only(self):
        text = """season,week,team,season_type,attempts,carries,passing_epa,rushing_epa
2026,1,ATL,REG,30,20,6,2
2026,4,ATL,REG,30,20,90,10
"""
        record = team_epa_per_play(text, 'ATL', week=4, game_type='REG', phase='2', source_url='https://example.test/epa', collected_at='2026-10-05T12:00:00+00:00')
        self.assertAlmostEqual(record['value'], 8 / 50)
        self.assertEqual(record['sample_size'], 1)
        self.assertTrue(espn_web_url('https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries').startswith('https://site.web.api.espn.com/'))


if __name__ == '__main__':
    unittest.main()
