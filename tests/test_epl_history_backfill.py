from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.collectors.http import HttpResponse
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.epl_history_backfill import (
    backfill_epl_result_features,
    derive_event_features,
    parse_football_data_matches,
)


UTC = timezone.utc
TARGET = datetime(2026, 8, 10, 19, tzinfo=UTC)
COLLECTED = datetime(2026, 8, 25, 2, tzinfo=UTC)


def archived_match(
    match_id,
    kickoff,
    home,
    away,
    home_score,
    away_score,
    *,
    last_updated=None,
):
    return {
        "id": match_id,
        "utcDate": kickoff.isoformat().replace("+00:00", "Z"),
        "lastUpdated": (last_updated or kickoff + timedelta(hours=3)).isoformat().replace(
            "+00:00", "Z"
        ),
        "status": "FINISHED",
        "homeTeam": {"id": match_id * 10 + 1, "name": home},
        "awayTeam": {"id": match_id * 10 + 2, "name": away},
        "score": {"fullTime": {"home": home_score, "away": away_score}},
    }


def archive_payload():
    matches = []
    for index in range(5):
        matches.append(
            archived_match(
                100 + index,
                datetime(2026, 5, 20 - index * 4, 15, tzinfo=UTC),
                "Arsenal FC",
                f"Home Opponent {index}",
                2,
                1,
            )
        )
        matches.append(
            archived_match(
                200 + index,
                datetime(2026, 5, 19 - index * 4, 15, tzinfo=UTC),
                f"Away Opponent {index}",
                "Chelsea FC",
                1,
                1,
            )
        )
    matches.append(archived_match(999, TARGET, "Arsenal FC", "Chelsea FC", 2, 1))
    return {"matches": matches}


class FakeTransport:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        competition = url.rstrip("/").split("/")[-2]
        key = (competition, str((params or {})["season"]))
        payload = self.payloads.get(key, {"matches": []})
        return HttpResponse(
            status=200,
            body=json.dumps(payload).encode("utf-8"),
            headers={},
            received_at=COLLECTED,
        )


class EplHistoryBackfillTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        initialize_schema(self.db)
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
                'England - Premier League', 'target-1', ?, 'settled',
                'Arsenal', 'Chelsea', 2, 1, 'home', '{}', '{}', NULL, NULL,
                NULL, ?, ?, 0, 1
            )
            """,
            (TARGET.isoformat(), COLLECTED.isoformat(), COLLECTED.isoformat()),
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_parser_ignores_unfinished_and_incomplete_matches(self):
        payload = archive_payload()
        payload["matches"].append(
            {
                "id": 5000,
                "utcDate": "2026-08-11T19:00:00Z",
                "lastUpdated": "2026-08-11T18:00:00Z",
                "status": "SCHEDULED",
                "homeTeam": {"name": "A"},
                "awayTeam": {"name": "B"},
                "score": {"fullTime": {"home": None, "away": None}},
            }
        )
        parsed = parse_football_data_matches(payload)
        self.assertEqual(len(parsed), 11)

    def test_derives_four_features_only_from_safe_prior_results(self):
        event = self.db.execute(
            "SELECT * FROM multisport_historical_events WHERE source_event_id='target-1'"
        ).fetchone()
        archive = parse_football_data_matches(archive_payload())
        rows, blockers = derive_event_features(event, archive)
        values = {row["feature_name"]: row["feature_value"] for row in rows}
        self.assertEqual(blockers, [])
        self.assertEqual(
            set(values),
            {
                "recent5_points_diff",
                "home_away_strength_diff",
                "rest_days_diff",
                "fixture_congestion_diff",
            },
        )
        self.assertEqual(values["recent5_points_diff"], 10.0)
        self.assertEqual(values["home_away_strength_diff"], 1.0)
        self.assertEqual(values["fixture_congestion_diff"], 0.0)
        self.assertTrue(all(row["source_updated_at"] < TARGET.isoformat() for row in rows))

    def test_future_updated_prior_result_is_excluded(self):
        payload = archive_payload()
        payload["matches"][0]["lastUpdated"] = "2026-08-11T00:00:00Z"
        event = self.db.execute(
            "SELECT * FROM multisport_historical_events WHERE source_event_id='target-1'"
        ).fetchone()
        rows, blockers = derive_event_features(event, parse_football_data_matches(payload))
        self.assertIn("recent5_requires_five_safe_prior_matches_per_team", blockers)
        self.assertIn("home_away_strength_requires_five_safe_venue_matches_per_team", blockers)
        self.assertNotIn("recent5_points_diff", {row["feature_name"] for row in rows})

    def test_bounded_backfill_imports_evidence_and_is_idempotent(self):
        transport = FakeTransport({
            ("PL", "2025"): archive_payload(),
            ("PL", "2026"): {"matches": []},
            ("ELC", "2025"): {"matches": []},
        })
        result = backfill_epl_result_features(
            self.db,
            api_key="secret",
            transport=transport,
            collected_at=COLLECTED,
            max_requests=4,
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["requests_made"], 3)
        self.assertEqual(result["requested_archives"], [
            {"competition": "PL", "season": 2025},
            {"competition": "PL", "season": 2026},
            {"competition": "ELC", "season": 2025},
        ])
        self.assertEqual(result["matched_events"], 1)
        self.assertEqual(
            result["event_verification_counts"],
            {"verified": 1, "unmatched": 0},
        )
        self.assertEqual(result["feature_evidence_accepted"], 4)
        self.assertEqual(result["remaining_features_not_provided"], [
            "availability_impact_diff", "xg_diff", "xga_diff"
        ])
        stored = self.db.execute(
            "SELECT feature_name FROM multisport_historical_feature_evidence ORDER BY feature_name"
        ).fetchall()
        self.assertEqual(len(stored), 4)
        verification = self.db.execute(
            """
            SELECT verification_status, verifier_event_id
            FROM multisport_historical_event_verifications
            WHERE source_event_id='target-1'
            """
        ).fetchone()
        self.assertEqual(verification["verification_status"], "verified")
        self.assertEqual(verification["verifier_event_id"], "999")

        second = backfill_epl_result_features(
            self.db,
            api_key="secret",
            transport=transport,
            collected_at=COLLECTED,
            max_requests=4,
        )
        self.assertEqual(second["feature_evidence_accepted"], 0)
        self.assertEqual(second["feature_evidence_duplicates"], 4)

    def test_championship_history_unlocks_promoted_team_features(self):
        self.db.execute(
            """
            UPDATE multisport_historical_events
            SET away_name='Coventry City'
            WHERE source_event_id='target-1'
            """
        )
        self.db.commit()
        premier_prior = []
        championship_prior = []
        for index in range(5):
            premier_prior.append(
                archived_match(
                    300 + index,
                    datetime(2026, 5, 20 - index * 4, 15, tzinfo=UTC),
                    "Arsenal FC",
                    f"Premier Opponent {index}",
                    2,
                    1,
                )
            )
            championship_prior.append(
                archived_match(
                    400 + index,
                    datetime(2026, 5, 19 - index * 4, 15, tzinfo=UTC),
                    f"Championship Opponent {index}",
                    "Coventry City FC",
                    1,
                    1,
                )
            )
        target = archived_match(1999, TARGET, "Arsenal FC", "Coventry City FC", 2, 1)
        transport = FakeTransport({
            ("PL", "2025"): {"matches": premier_prior},
            ("PL", "2026"): {"matches": [target]},
            ("ELC", "2025"): {"matches": championship_prior},
        })

        result = backfill_epl_result_features(
            self.db,
            api_key="secret",
            transport=transport,
            collected_at=COLLECTED,
            max_requests=4,
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["matched_events"], 1)
        self.assertEqual(result["feature_evidence_accepted"], 4)
        self.assertEqual(result["blocker_counts"], {})
        self.assertEqual(result["premier_league_archive_matches"], 6)
        self.assertEqual(result["championship_archive_matches"], 5)
        source_rows = self.db.execute(
            """
            SELECT source_key, evidence_json
            FROM multisport_historical_feature_evidence
            WHERE source_event_id='target-1'
            """
        ).fetchall()
        self.assertEqual(len(source_rows), 4)
        self.assertTrue(all("championship" in row["source_key"] for row in source_rows))
        self.assertTrue(all("ELC" in row["evidence_json"] for row in source_rows))

    def test_championship_target_cannot_verify_an_epl_event(self):
        event = self.db.execute(
            "SELECT * FROM multisport_historical_events WHERE source_event_id='target-1'"
        ).fetchone()
        combined = parse_football_data_matches(
            archive_payload(),
            competition_code="ELC",
            source_url="https://api.football-data.org/v4/competitions/ELC/matches",
        )
        rows, blockers = derive_event_features(
            event,
            combined,
            verification_archive=[],
        )
        self.assertEqual(rows, [])
        self.assertEqual(
            blockers,
            ["target_match_not_found_in_football_data_archive"],
        )


if __name__ == "__main__":
    unittest.main()
