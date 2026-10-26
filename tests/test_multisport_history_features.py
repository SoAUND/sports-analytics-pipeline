from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.multisport_features import required_feature_names
from sports_analytics_pipeline.multisport_history_features import (
    HistoricalFeatureError,
    SUPPORTED_SPORTS,
    binary_moneyline_market_feature,
    build_history_examples,
    import_feature_evidence,
)

UTC = timezone.utc
EVENT_TIME = datetime(2026, 8, 10, 19, tzinfo=UTC)
BUILT_AT = datetime(2026, 8, 29, 19, tzinfo=UTC)


def ml_payload(updated_at="2026-08-10T18:55:00Z"):
    return {
        "bookmakers": {
            "DraftKings": [{
                "name": "ML",
                "updatedAt": updated_at,
                "odds": [{"home": "1.80", "away": "2.10"}],
            }],
            "FanDuel": [{
                "name": "ML",
                "updatedAt": "2026-08-10T18:50:00Z",
                "odds": [{"home": "1.85", "away": "2.05"}],
            }],
        }
    }


class MultiSportHistoricalFeatureTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        initialize_schema(self.db)

    def tearDown(self):
        self.db.close()

    def seed_event(self, sport, *, verified=True):
        source_id = f"{sport}-1"
        self.db.execute(
            """
            INSERT INTO multisport_historical_events (
                provider_key, sport, provider_sport_slug, provider_league_slug,
                provider_league_name, source_event_id, event_time, status,
                home_name, away_name, home_score, away_score, result_role,
                scores_json, result_payload_json, odds_market_name, odds_payload_json,
                odds_collected_at, first_collected_at, last_collected_at,
                training_eligible, test_only
            ) VALUES (
                'odds_api_io', ?, ?, 'league', 'League', ?, ?, 'settled',
                'Home', 'Away', 1, 0, 'home', '{}', '{}', 'ML', ?, ?, ?, ?, 0, 1
            )
            """,
            (
                sport,
                sport,
                source_id,
                EVENT_TIME.isoformat(),
                json.dumps(ml_payload()),
                BUILT_AT.isoformat(),
                BUILT_AT.isoformat(),
                BUILT_AT.isoformat(),
            ),
        )
        if verified:
            verifier = {
                "nba": "official_nba_results_archive",
                "wnba": "official_wnba_results_archive",
                "mma": "official_ufc_results_archive",
                "atp": "official_atp_results_archive",
                "wta": "official_wta_results_archive",
            }[sport]
            self.db.execute(
                """
                INSERT INTO multisport_historical_event_verifications (
                    provider_key, source_event_id, sport, verifier_key,
                    verifier_event_id, verification_status, verified_at,
                    evidence_json, test_only
                ) VALUES ('odds_api_io', ?, ?, ?, 'official-1', 'verified', ?, '{}', 1)
                """,
                (source_id, sport, verifier, BUILT_AT.isoformat()),
            )
        self.db.commit()

    def evidence_rows(self, sport, *, omit_market=True):
        rows = []
        for index, name in enumerate(required_feature_names(sport)):
            if omit_market and name == "market_home_no_vig":
                continue
            rows.append({
                "provider_key": "odds_api_io",
                "source_event_id": f"{sport}-1",
                "sport": sport,
                "feature_name": name,
                "feature_value": 0.1 + index,
                "source_key": f"approved_{sport}_pre_event_archive",
                "source_url": f"https://example.test/{sport}/archive",
                "source_updated_at": "2026-08-10T18:40:00Z",
                "evidence": {"pre_event": True},
            })
        return rows

    def test_supported_sports_are_exactly_remaining_model_sports(self):
        self.assertEqual(set(SUPPORTED_SPORTS), {"nba", "wnba", "mma", "atp", "wta"})

    def test_binary_moneyline_removes_vig_and_uses_only_pre_event_quotes(self):
        probability, cutoff, details = binary_moneyline_market_feature(
            ml_payload(), event_time=EVENT_TIME
        )
        self.assertGreater(probability, 0.0)
        self.assertLess(probability, 1.0)
        self.assertLess(cutoff, EVENT_TIME)
        self.assertEqual(details["accepted_quotes"], 2)

    def test_future_only_moneyline_quotes_fail_closed(self):
        payload = ml_payload(updated_at="2026-08-10T19:01:00Z")
        payload["bookmakers"]["FanDuel"][0]["updatedAt"] = "2026-08-10T19:02:00Z"
        with self.assertRaises(HistoricalFeatureError):
            binary_moneyline_market_feature(payload, event_time=EVENT_TIME)

    def test_verified_atp_event_promotes_only_when_complete(self):
        self.seed_event("atp")
        imported = import_feature_evidence(
            self.db, self.evidence_rows("atp"), collected_at=BUILT_AT
        )
        self.assertEqual(imported["rejected"], 0)
        result = build_history_examples(
            self.db, sports=("atp",), built_at=BUILT_AT, minimum_examples=1
        )
        self.assertEqual(result["sports"]["atp"]["created"], 1)
        self.assertTrue(result["sports"]["atp"]["training_ready"])
        stored = self.db.execute(
            "SELECT * FROM multisport_training_examples WHERE sport='atp'"
        ).fetchone()
        self.assertLess(stored["source_cutoff"], stored["event_time"])

    def test_tennis_history_uses_controlled_tour_family_key(self):
        self.seed_event("atp")
        imported = import_feature_evidence(
            self.db, self.evidence_rows("atp"), collected_at=BUILT_AT
        )
        self.assertEqual(imported["rejected"], 0)
        result = build_history_examples(
            self.db, sports=("atp",), built_at=BUILT_AT, minimum_examples=1
        )
        self.assertEqual(result["sports"]["atp"]["created"], 1)
        row = self.db.execute(
            "SELECT provider_sport_key FROM multisport_training_examples WHERE sport='atp'"
        ).fetchone()
        self.assertEqual(row["provider_sport_key"], "tennis_atp_historical")

    def test_unverified_atp_event_is_quarantined(self):
        self.seed_event("atp", verified=False)
        result = build_history_examples(self.db, sports=("atp",), built_at=BUILT_AT)
        self.assertEqual(result["sports"]["atp"]["verified_records"], 0)
        self.assertEqual(result["sports"]["atp"]["quarantined_unverified"], 1)
        self.assertEqual(result["sports"]["atp"]["created"], 0)

    def test_missing_tennis_features_are_audited_not_fabricated(self):
        self.seed_event("wta")
        result = build_history_examples(self.db, sports=("wta",), built_at=BUILT_AT)
        self.assertEqual(result["sports"]["wta"]["created"], 0)
        self.assertEqual(result["sports"]["wta"]["blocked"], 1)
        self.assertGreater(len(result["sports"]["wta"]["missing_feature_patterns"]), 0)

    def test_basketball_game_winner_is_never_used_as_spread_cover_target(self):
        self.seed_event("wnba")
        imported = import_feature_evidence(
            self.db, self.evidence_rows("wnba", omit_market=False), collected_at=BUILT_AT
        )
        self.assertEqual(imported["rejected"], 0)
        result = build_history_examples(
            self.db, sports=("wnba",), built_at=BUILT_AT, minimum_examples=1
        )
        self.assertEqual(result["sports"]["wnba"]["created"], 0)
        self.assertEqual(
            result["sports"]["wnba"]["blocker_counts"][
                "spread_cover_outcome_verification_required"
            ],
            1,
        )

    def test_future_feature_evidence_is_rejected(self):
        self.seed_event("mma")
        rows = self.evidence_rows("mma")
        rows[0]["source_updated_at"] = "2026-08-10T19:00:00Z"
        result = import_feature_evidence(self.db, rows, collected_at=BUILT_AT)
        self.assertEqual(result["rejected"], 1)
        self.assertIn("strictly before", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
