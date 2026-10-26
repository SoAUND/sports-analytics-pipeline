from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.multisport_features import (
    EPL_BASELINE_FEATURE_VERSION,
    required_feature_names,
)
from sports_analytics_pipeline.multisport_history_builder import (
    MarketFeatureError,
    build_history_examples,
    epl_market_features,
    import_feature_evidence,
)


UTC = timezone.utc
EVENT_TIME = datetime(2026, 8, 10, 19, tzinfo=UTC)
BUILT_AT = datetime(2026, 8, 25, 1, tzinfo=UTC)


def odds_payload(*, updated_at="2026-08-10T18:55:00Z"):
    return {
        "id": 123,
        "bookmakers": {
            "DraftKings": [{
                "name": "ML",
                "updatedAt": updated_at,
                "odds": [{"home": "2.10", "draw": "3.40", "away": "3.60"}],
            }],
            "FanDuel": [{
                "name": "ML",
                "updatedAt": "2026-08-10T18:50:00Z",
                "odds": [{"home": "2.00", "draw": "3.50", "away": "3.80"}],
            }],
        },
    }


class HistoricalExampleBuilderTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        initialize_schema(self.db)

    def tearDown(self):
        self.db.close()

    def seed_event(self, *, payload=None, verified=True):
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
                'odds_api_io', 'epl', 'football', 'england-premier-league',
                'England - Premier League', '123', ?, 'settled',
                'Arsenal', 'Chelsea', 2, 1, 'home', '{}', '{}', 'ML', ?,
                ?, ?, ?, 0, 1
            )
            """,
            (
                EVENT_TIME.isoformat(),
                json.dumps(payload if payload is not None else odds_payload()),
                BUILT_AT.isoformat(), BUILT_AT.isoformat(), BUILT_AT.isoformat(),
            ),
        )
        if verified:
            self.db.execute(
                """
                INSERT INTO multisport_historical_event_verifications (
                    provider_key, source_event_id, sport, verifier_key,
                    verifier_event_id, verification_status, verified_at,
                    evidence_json, test_only
                ) VALUES (
                    'odds_api_io', '123', 'epl',
                    'football_data_epl_results_archive', '999', 'verified', ?, '{}', 1
                )
                """,
                (BUILT_AT.isoformat(),),
            )
        self.db.commit()

    def evidence_rows(
        self,
        *,
        source_updated_at="2026-08-10T18:45:00Z",
        baseline_only=False,
    ):
        values = {
            "xg_diff": 0.35,
            "xga_diff": 0.20,
            "recent5_points_diff": 3.0,
            "home_away_strength_diff": 0.25,
            "availability_impact_diff": -0.10,
            "rest_days_diff": 1.0,
            "fixture_congestion_diff": -1.0,
        }
        if baseline_only:
            values = {
                name: value
                for name, value in values.items()
                if name in {
                    "recent5_points_diff",
                    "home_away_strength_diff",
                    "rest_days_diff",
                    "fixture_congestion_diff",
                }
            }
        return [
            {
                "provider_key": "odds_api_io",
                "source_event_id": "123",
                "sport": "epl",
                "feature_name": name,
                "feature_value": value,
                "source_key": "approved_epl_archive",
                "source_url": "https://example.test/epl/archive",
                "source_updated_at": source_updated_at,
                "evidence": {"home": "Arsenal", "away": "Chelsea"},
            }
            for name, value in values.items()
        ]

    def test_odds_only_are_reported_but_never_promoted(self):
        self.seed_event()
        result = build_history_examples(self.db, sports=("epl",), built_at=BUILT_AT)
        epl = result["sports"]["epl"]
        self.assertEqual(epl["scanned"], 1)
        self.assertEqual(epl["created"], 0)
        self.assertEqual(epl["blocked"], 1)
        self.assertEqual(epl["feature_coverage"]["market_home_no_vig"], 1)
        self.assertEqual(epl["feature_coverage"]["recent5_points_diff"], 0)
        self.assertEqual(epl["training_examples_total"], 0)
        self.assertEqual(
            epl["market_diagnostics"]["status_counts"]["usable_pre_event_1x2"], 1
        )
        audit = self.db.execute(
            "SELECT * FROM multisport_history_example_audits"
        ).fetchone()
        self.assertEqual(audit["status"], "blocked")
        self.assertIn("recent5_points_diff", json.loads(audit["missing_features_json"]))

    def test_unverified_event_is_quarantined_before_feature_building(self):
        self.seed_event(verified=False)
        result = build_history_examples(self.db, sports=("epl",), built_at=BUILT_AT)
        epl = result["sports"]["epl"]
        self.assertEqual(epl["records_seen"], 1)
        self.assertEqual(epl["scanned"], 0)
        self.assertEqual(epl["quarantined_unverified"], 1)
        self.assertEqual(epl["created"], 0)

    def test_complete_pre_event_evidence_creates_one_example(self):
        self.seed_event()
        imported = import_feature_evidence(
            self.db,
            self.evidence_rows(baseline_only=True),
            collected_at=BUILT_AT,
        )
        self.assertEqual(imported["accepted"], 4)
        self.assertEqual(imported["training_examples_created"], 0)

        result = build_history_examples(self.db, sports=("epl",), built_at=BUILT_AT)
        self.assertEqual(result["sports"]["epl"]["created"], 1)
        self.assertEqual(result["sports"]["epl"]["training_examples_total"], 1)
        stored = self.db.execute(
            "SELECT * FROM multisport_training_examples WHERE sport='epl'"
        ).fetchone()
        features = json.loads(stored["features_json"])
        self.assertEqual(stored["feature_version"], EPL_BASELINE_FEATURE_VERSION)
        self.assertEqual(
            set(features),
            set(
                required_feature_names(
                    "epl",
                    feature_version=EPL_BASELINE_FEATURE_VERSION,
                )
            ),
        )
        self.assertEqual(
            result["sports"]["epl"]["enhanced_features_deferred"],
            ["availability_impact_diff", "xg_diff", "xga_diff"],
        )
        self.assertAlmostEqual(
            features["market_home_no_vig"]
            + features["market_draw_no_vig"]
            + features["market_away_no_vig"],
            1.0,
        )
        self.assertLess(stored["source_cutoff"], stored["event_time"])

        second = build_history_examples(self.db, sports=("epl",), built_at=BUILT_AT)
        self.assertEqual(second["sports"]["epl"]["created"], 0)
        self.assertEqual(second["sports"]["epl"]["existing"], 1)

    def test_future_feature_evidence_is_rejected(self):
        self.seed_event()
        result = import_feature_evidence(
            self.db,
            self.evidence_rows(source_updated_at="2026-08-10T19:00:00Z"),
            collected_at=BUILT_AT,
        )
        self.assertEqual(result["accepted"], 0)
        self.assertEqual(result["rejected"], 7)
        self.assertTrue(all("strictly before" in error for error in result["errors"]))

    def test_future_market_quote_is_not_used(self):
        self.seed_event(payload=odds_payload(updated_at="2026-08-10T19:01:00Z"))
        values, cutoff, details = epl_market_features(
            odds_payload(updated_at="2026-08-10T19:01:00Z"),
            event_time=EVENT_TIME,
        )
        self.assertEqual(details["accepted_quotes"], 1)
        self.assertEqual(details["bookmakers"], ["FanDuel"])
        self.assertEqual(
            details["rejection_reason_counts"]["quote_at_or_after_event_time"], 1
        )
        self.assertLess(cutoff, EVENT_TIME)
        self.assertAlmostEqual(sum(values.values()), 1.0)

    def test_all_future_quotes_report_the_exact_market_blocker(self):
        payload = odds_payload(updated_at="2026-08-10T19:01:00Z")
        payload["bookmakers"]["FanDuel"][0]["updatedAt"] = "2026-08-10T19:02:00Z"
        with self.assertRaises(MarketFeatureError) as raised:
            epl_market_features(payload, event_time=EVENT_TIME)
        self.assertEqual(
            raised.exception.details["rejection_reason_counts"][
                "quote_at_or_after_event_time"
            ],
            2,
        )


if __name__ == "__main__":
    unittest.main()
