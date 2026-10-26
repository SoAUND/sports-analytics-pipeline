import json
from pathlib import Path
import tempfile
import unittest

from sports_analytics_pipeline.venue_sync import VenueObservation, synchronize_catalogs, synchronize_rows


class VenueSyncTests(unittest.TestCase):
    def test_exact_reviewed_name_safely_links_provider_id(self):
        rows = [
            {
                "venue_id": "rocket_arena",
                "official_name": "Rocket Arena",
                "sports": ["nba"],
                "aliases": ["Rocket Mortgage FieldHouse"],
            }
        ]
        updated, actions = synchronize_rows(
            rows,
            [VenueObservation("nba", "espn", "123", "Rocket Mortgage FieldHouse")],
            sport="nba",
        )
        self.assertEqual(updated[0]["provider_ids"], ["espn:123"])
        self.assertEqual(actions[0].status, "linked_provider_id")

    def test_trusted_stable_provider_id_updates_name_and_keeps_old_alias(self):
        rows = [
            {
                "venue_id": "arena",
                "official_name": "Old Sponsor Arena",
                "sports": ["mlb"],
                "aliases": [],
                "provider_ids": ["mlb_statsapi:123"],
            }
        ]
        updated, actions = synchronize_rows(
            rows,
            [VenueObservation("mlb", "mlb_statsapi", "123", "New Sponsor Arena")],
            sport="mlb",
        )
        self.assertEqual(updated[0]["official_name"], "New Sponsor Arena")
        self.assertIn("Old Sponsor Arena", updated[0]["aliases"])
        self.assertEqual(actions[0].status, "safe_name_update")

    def test_untrusted_provider_name_change_becomes_alias_not_display_name(self):
        rows = [
            {
                "venue_id": "arena",
                "official_name": "Current Arena",
                "sports": ["nba"],
                "aliases": [],
                "provider_ids": ["espn:123"],
            }
        ]
        updated, actions = synchronize_rows(
            rows,
            [VenueObservation("nba", "espn", "123", "Historical Arena")],
            sport="nba",
        )
        self.assertEqual(updated[0]["official_name"], "Current Arena")
        self.assertIn("Historical Arena", updated[0]["aliases"])
        self.assertEqual(actions[0].status, "added_provider_name_alias")

    def test_new_id_and_new_name_are_pending_and_never_added(self):
        rows = [
            {"venue_id": "known", "official_name": "Known Arena", "sports": ["mma"]}
        ]
        updated, actions = synchronize_rows(
            rows,
            [VenueObservation("mma", "espn", "999", "Pop-Up Arena")],
            sport="mma",
        )
        self.assertEqual(updated, rows)
        self.assertEqual(actions[0].status, "pending_review")
        self.assertIsNone(actions[0].venue_id)

    def test_name_only_source_may_match_but_never_renames(self):
        rows = [{"stadium_id": "known", "official_name": "Known Stadium"}]
        updated, actions = synchronize_rows(
            rows,
            [VenueObservation("nfl", "nflverse", None, "Known Stadium")],
            sport="nfl",
        )
        self.assertEqual(updated[0]["official_name"], "Known Stadium")
        self.assertEqual(actions[0].status, "matched_reviewed_name")

    def test_verified_venue_can_be_safely_linked_for_another_sport(self):
        rows = [
            {
                "venue_id": "shared_arena",
                "official_name": "Shared Arena",
                "sports": ["nba"],
                "aliases": [],
            }
        ]
        updated, actions = synchronize_rows(
            rows,
            [VenueObservation("mma", "espn", "456", "Shared Arena")],
            sport="mma",
        )
        self.assertEqual(updated[0]["sports"], ["mma", "nba"])
        self.assertEqual(updated[0]["provider_ids"], ["espn:456"])
        self.assertEqual(actions[0].status, "linked_provider_id_and_sport")

    def test_catalog_write_is_opt_in(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "src/sports_analytics_pipeline/data"
            path.mkdir(parents=True)
            catalog = [
                {
                    "venue_id": "arena",
                    "official_name": "Arena",
                    "sports": ["nba"],
                    "aliases": [],
                }
            ]
            target = path / "multisport_venues.json"
            target.write_text(json.dumps(catalog), encoding="utf-8")
            observation = VenueObservation("nba", "espn", "1", "Arena")
            synchronize_catalogs(root, [observation], sports=["nba"], apply=False)
            self.assertNotIn("provider_ids", json.loads(target.read_text())[0])
            synchronize_catalogs(root, [observation], sports=["nba"], apply=True)
            self.assertEqual(json.loads(target.read_text())[0]["provider_ids"], ["espn:1"])


if __name__ == "__main__":
    unittest.main()
