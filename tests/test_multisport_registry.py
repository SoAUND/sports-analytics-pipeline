import unittest

from sports_analytics_pipeline.sports_registry import (
    SPORT_MARKETS,
    discover_active_provider_keys,
    get_sport_market,
    validation_bucket,
)


class MultiSportRegistryTests(unittest.TestCase):
    def test_requested_sports_are_registered_and_test_only(self):
        self.assertEqual(set(SPORT_MARKETS), {"nba", "wnba", "epl", "mma", "atp", "wta"})
        for spec in SPORT_MARKETS.values():
            self.assertTrue(spec.test_only)
            self.assertEqual(spec.validation_target, 100)

    def test_exact_provider_keys_match_official_odds_api_keys(self):
        self.assertTrue(get_sport_market("nba").matches_provider_sport_key("basketball_nba"))
        self.assertTrue(get_sport_market("wnba").matches_provider_sport_key("basketball_wnba"))
        self.assertTrue(get_sport_market("epl").matches_provider_sport_key("soccer_epl"))
        self.assertTrue(get_sport_market("mma").matches_provider_sport_key("mma_mixed_martial_arts"))
        self.assertFalse(get_sport_market("nba").matches_provider_sport_key("basketball_wnba"))

    def test_tennis_uses_tournament_key_families(self):
        atp = get_sport_market("atp")
        wta = get_sport_market("wta")
        self.assertTrue(atp.matches_provider_sport_key("tennis_atp_cincinnati_open"))
        self.assertTrue(atp.matches_provider_sport_key("tennis_atp_us_open"))
        self.assertFalse(atp.matches_provider_sport_key("tennis_wta_cincinnati_open"))
        self.assertTrue(wta.matches_provider_sport_key("tennis_wta_cincinnati_open"))
        self.assertFalse(wta.matches_provider_sport_key("tennis_atp_cincinnati_open"))

    def test_first_markets_are_narrow_and_league_specific(self):
        self.assertEqual(get_sport_market("nba").provider_market_key, "spreads")
        self.assertEqual(get_sport_market("wnba").provider_market_key, "spreads")
        self.assertEqual(get_sport_market("epl").provider_market_key, "h2h")
        self.assertEqual(get_sport_market("epl").outcome_count, 3)
        self.assertTrue(get_sport_market("epl").allows_draw)
        for sport in ("mma", "atp", "wta"):
            self.assertEqual(get_sport_market(sport).provider_market_key, "h2h")
            self.assertEqual(get_sport_market(sport).outcome_count, 2)
            self.assertFalse(get_sport_market(sport).allows_draw)

    def test_active_key_discovery_filters_inactive_and_other_sports(self):
        payload = [
            {"key": "tennis_atp_cincinnati_open", "active": True},
            {"key": "tennis_atp_us_open", "active": False},
            {"key": "tennis_wta_cincinnati_open", "active": True},
            {"key": "basketball_nba", "active": True},
        ]
        self.assertEqual(
            discover_active_provider_keys(payload, "atp"),
            ("tennis_atp_cincinnati_open",),
        )
        self.assertEqual(
            discover_active_provider_keys(payload, "wta"),
            ("tennis_wta_cincinnati_open",),
        )
        self.assertEqual(discover_active_provider_keys(payload, "nba"), ("basketball_nba",))

    def test_each_sport_market_has_an_independent_validation_bucket(self):
        buckets = {validation_bucket(sport) for sport in SPORT_MARKETS}
        self.assertEqual(len(buckets), len(SPORT_MARKETS))
        self.assertIn("nba:spreads:full_game", buckets)
        self.assertIn("wnba:spreads:full_game", buckets)
        self.assertIn("epl:three_way_moneyline:full_game", buckets)
        self.assertIn("atp:match_moneyline:full_match", buckets)
        self.assertIn("wta:match_moneyline:full_match", buckets)

    def test_unknown_sports_are_rejected(self):
        with self.assertRaises(ValueError):
            get_sport_market("unknown")


if __name__ == "__main__":
    unittest.main()
