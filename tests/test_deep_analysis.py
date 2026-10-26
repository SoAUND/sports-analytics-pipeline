import unittest

from sports_analytics_pipeline.deep_analysis import (
    MARKET_FAMILIES, advanced_metric_gaps, audit_candidate,
    discoverable_markets, report_section, required_factor_names, required_metrics,
    soccer_outcome_gaps, stored_three_way_probabilities,
)


class DeepAnalysisContractTests(unittest.TestCase):
    def test_discovery_never_becomes_a_pick(self):
        row={'event_id':'e1','sport':'nfl','market':'moneyline','market_status':'open',
             'price_time':'2026-10-04T15:00:00+00:00','publication_decision':'watchlist'}
        audit=audit_candidate(row)
        self.assertEqual(audit.level,3)
        self.assertEqual(report_section(row),'Leans / Watchlist')
        self.assertIn('epa_per_play',audit.missing_metrics)

    def test_nfl_props_require_team_and_opportunity_metrics(self):
        required=set(required_metrics('nfl','player_prop'))
        self.assertIn('epa_per_play',required)
        self.assertIn('expected_routes',required)
        self.assertIn('man_zone_splits',required)
        self.assertIn('teammate_injury_role_change',required)

    def test_unknown_or_absent_metrics_fail_closed(self):
        row={'sport':'mlb','market':'totals','advanced_metrics':{'xera':3.2}}
        gaps=advanced_metric_gaps(row)
        self.assertIn('bullpen_workload',gaps)
        self.assertIn('batting_order',gaps)

    def test_market_inventory_reports_unsupported_families(self):
        inventory=discoverable_markets(['h2h','spreads','totals'])
        self.assertEqual(set(inventory),set(MARKET_FAMILIES))
        self.assertTrue(inventory['moneyline'])
        self.assertFalse(inventory['player_props'])
        self.assertFalse(inventory['alternate_lines'])

    def test_official_label_requires_level_30(self):
        row={'event_id':'e1','sport':'wnba','market':'spreads','market_status':'open',
             'price_time':'2026-10-04T15:00:00+00:00','publication_decision':'official_pick'}
        self.assertEqual(report_section(row),'Passes')

    def test_missing_draw_is_not_stored_as_zero(self):
        self.assertIsNone(stored_three_way_probabilities({"home": 0.6, "away": 0.4}))
        stored = stored_three_way_probabilities({"home": 0.5, "draw": 0.2, "away": 0.3})
        self.assertEqual(stored["draw"], 0.2)
        gaps = soccer_outcome_gaps({"sport": "epl", "market": "three_way_moneyline", "participant": "home", "model": {"probability": 0.6}})
        self.assertEqual(gaps, ("home", "draw", "away"))

    def test_tennis_and_mma_use_performance_consistency(self):
        self.assertIn("performance_consistency", required_factor_names("atp"))
        self.assertNotIn("team_consistency", required_factor_names("mma"))
        self.assertIn("team_consistency", required_factor_names("nba"))


if __name__=='__main__':
    unittest.main()
