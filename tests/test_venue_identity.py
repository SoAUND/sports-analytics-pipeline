import unittest

from sports_analytics_pipeline.venue_identity import (
    SUPPORTED_VENUE_SPORTS,
    VENUE_RESOLUTION_MODE,
    VenueDirectory,
    multisport_venue_directory,
    normalize_venue_name,
)


class VenueIdentityTests(unittest.TestCase):
    def test_every_supported_sport_has_an_explicit_resolution_policy(self):
        self.assertEqual(set(VENUE_RESOLUTION_MODE), set(SUPPORTED_VENUE_SPORTS))
        for sport in ("mma", "atp", "wta"):
            self.assertIn("supplied", VENUE_RESOLUTION_MODE[sport])

    def test_normalization_handles_accents_punctuation_and_spacing(self):
        self.assertEqual(
            normalize_venue_name("  Arena Ciudad de México "),
            normalize_venue_name("Arena Ciudad de Mexico"),
        )
        self.assertEqual(normalize_venue_name("Crypto.com Arena"), "crypto com arena")

    def test_current_renamed_venues_and_aliases_resolve(self):
        directory = multisport_venue_directory()
        cases = (
            ("nba", "Rocket Mortgage FieldHouse", "rocket_arena"),
            ("nba", "Wells Fargo Center", "xfinity_mobile_arena"),
            ("nba", "Footprint Center", "mortgage_matchup_center"),
            ("wnba", "Entertainment and Sports Arena", "carefirst_arena"),
            ("epl", "Everton Stadium", "hill_dickinson_stadium"),
            ("mma", "Arena Ciudad de México", "arena_cdmx"),
            ("atp", "Queen's Club", "queens_club"),
            ("wta", "Indian Wells", "indian_wells_tennis_garden"),
        )
        for sport, source_name, expected in cases:
            with self.subTest(sport=sport, source_name=source_name):
                result = directory.resolve_optional(sport, source_name=source_name)
                self.assertTrue(result.resolved)
                self.assertEqual(result.venue.venue_id, expected)

    def test_provider_id_wins_over_changed_display_name(self):
        directory = VenueDirectory.from_records(
            [
                {
                    "venue_id": "stable_arena",
                    "official_name": "Stable Arena",
                    "sports": ["nba"],
                    "provider_ids": ["123"],
                }
            ]
        )
        result = directory.resolve_optional(
            "nba", source_id=123, source_name="Brand-New Sponsor Name"
        )
        self.assertEqual(result.matched_by, "provider_id")
        self.assertEqual(result.venue.venue_id, "stable_arena")

    def test_exact_tournament_provider_key_resolves_reviewed_venue(self):
        directory = multisport_venue_directory()
        for sport, provider_sport_key, venue_id, city in (
            ("atp", "tennis_atp_cincinnati_open", "lindner_family_tennis_center", "Mason"),
            ("wta", "tennis_wta_cincinnati_open", "lindner_family_tennis_center", "Mason"),
            ("wta", "tennis_wta_monterrey_open", "club_sonoma", "Monterrey"),
        ):
            with self.subTest(sport=sport):
                result = directory.resolve_optional(
                    sport,
                    provider_sport_key=provider_sport_key,
                )
                self.assertTrue(result.resolved)
                self.assertEqual(result.matched_by, "provider_sport_key")
                self.assertEqual(result.venue.venue_id, venue_id)
                self.assertEqual(result.venue.city, city)

    def test_tournament_key_never_overrides_a_conflicting_supplied_venue(self):
        result = multisport_venue_directory().resolve_optional(
            "wta",
            source_name="Temporary Relocated Venue",
            provider_sport_key="tennis_wta_cincinnati_open",
        )
        self.assertFalse(result.resolved)

    def test_unknown_venue_returns_unresolved_instead_of_guessing(self):
        result = multisport_venue_directory().resolve_optional(
            "wta", source_name="Unknown Temporary Court"
        )
        self.assertFalse(result.resolved)

    def test_epl_home_team_resolves_only_reviewed_stadium_mapping(self):
        directory = multisport_venue_directory()
        resolved = directory.resolve_reviewed_home("epl", "AFC Bournemouth")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.venue_id, "vitality_stadium")
        self.assertIsNone(directory.resolve_reviewed_home("epl", "Unknown United"))
        self.assertIsNone(directory.resolve_reviewed_home("nba", "Bournemouth"))


if __name__ == "__main__":
    unittest.main()
