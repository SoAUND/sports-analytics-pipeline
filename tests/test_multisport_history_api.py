from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.collectors.multisport_history_api import (
    EPL_OFFICIAL_VERIFIER_KEY,
    EVENTS_URL,
    LEAGUES_URL,
    ODDS_URL,
    SPORTS_URL,
    collect_historical_data,
    historical_collection_status,
    parse_historical_events,
    select_leagues,
)
from sports_analytics_pipeline.data.db import connect, initialize_schema


UTC = timezone.utc


class Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeTransport:
    def __init__(self):
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if url == SPORTS_URL:
            return Response([{"name": "Football", "slug": "football"}])
        if url == LEAGUES_URL:
            return Response(
                [
                    {
                        "eventsCount": 20,
                        "name": "England - Premier League",
                        "slug": "england-premier-league",
                    },
                    {"eventsCount": 10, "name": "Spain - La Liga", "slug": "spain-la-liga"},
                ]
            )
        if url == EVENTS_URL:
            return Response(
                [
                    {
                        "id": 123,
                        "home": "Arsenal",
                        "away": "Chelsea",
                        "homeId": 1,
                        "awayId": 2,
                        "date": "2026-08-10T19:00:00Z",
                        "status": "settled",
                        "sport": {"name": "Football", "slug": "football"},
                        "league": {
                            "name": "England - Premier League",
                            "slug": "england-premier-league",
                        },
                        "scores": {"home": 2, "away": 1, "periods": {}},
                    }
                ]
            )
        if url == ODDS_URL:
            return Response(
                {
                    "id": 123,
                    "home": "Arsenal",
                    "away": "Chelsea",
                    "status": "settled",
                    "bookmakers": {
                        "DraftKings": [
                            {
                                "name": "ML",
                                "updatedAt": "2026-08-10T18:59:00Z",
                                "odds": [{"home": "2.10", "draw": "3.40", "away": "3.60"}],
                            }
                        ]
                    },
                }
            )
        raise AssertionError(f"Unexpected URL: {url}")


class EmptyEventsTransport(FakeTransport):
    def get(self, url, *, params=None, headers=None):
        if url == EVENTS_URL:
            self.calls.append((url, dict(params or {}), dict(headers or {})))
            return Response([])
        return super().get(url, params=params, headers=headers)


class RotationTransport:
    LEAGUES = {
        "nfl": ("NFL Regular Season", "nfl-regular-season"),
        "mlb": ("MLB Regular Season", "mlb-regular-season"),
        "basketball": ("NBA", "nba"),
        "football": ("England - Premier League", "england-premier-league"),
        "mixed-martial-arts": ("UFC", "ufc"),
        "tennis": ("ATP Tour", "atp-tour"),
    }

    def __init__(self):
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        parameters = dict(params or {})
        self.calls.append((url, parameters, dict(headers or {})))
        if url == SPORTS_URL:
            return Response(
                [
                    {"name": "NFL", "slug": "nfl"},
                    {"name": "MLB", "slug": "mlb"},
                    {"name": "Basketball", "slug": "basketball"},
                    {"name": "Football", "slug": "football"},
                    {"name": "Mixed Martial Arts", "slug": "mixed-martial-arts"},
                    {"name": "Tennis", "slug": "tennis"},
                ]
            )
        if url == LEAGUES_URL:
            name, slug = self.LEAGUES[parameters["sport"]]
            return Response([{"eventsCount": 10, "name": name, "slug": slug}])
        if url == EVENTS_URL:
            return Response([])
        raise AssertionError(f"Unexpected URL: {url}")


class HistoricalCollectorTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        initialize_schema(self.db)

    def tearDown(self):
        self.db.close()

    def test_collects_results_and_bounded_closing_odds_without_training_rows(self):
        transport = FakeTransport()
        result = collect_historical_data(
            self.db,
            api_key="private-key",
            transport=transport,
            sports=("epl",),
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 12, tzinfo=UTC),
            bookmakers=("DraftKings",),
            include_odds=True,
            max_requests=10,
            max_leagues_per_sport=2,
            max_odds_events=1,
            collected_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["requests_made"], 4)
        self.assertEqual(result["events_upserted"], 1)
        self.assertEqual(result["odds_events_collected"], 1)
        self.assertEqual(result["training_examples_created"], 0)
        event = self.db.execute("SELECT * FROM multisport_historical_events").fetchone()
        self.assertEqual(event["sport"], "epl")
        self.assertEqual(event["result_role"], "home")
        self.assertEqual(event["odds_market_name"], "ML")
        self.assertEqual(event["training_eligible"], 0)
        self.assertEqual(event["test_only"], 1)
        self.assertNotIn("private-key", repr(result))
        status = historical_collection_status(self.db)
        self.assertEqual(status["sports"]["epl"]["settled_events"], 1)
        self.assertEqual(status["sports"]["epl"]["events_with_closing_odds"], 1)
        self.assertFalse(status["automatic_training_promotion"])

    def test_existing_odds_are_not_requested_again(self):
        transport = FakeTransport()
        arguments = dict(
            api_key="private-key",
            sports=("epl",),
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 12, tzinfo=UTC),
            bookmakers=("DraftKings",),
            include_odds=True,
            max_requests=10,
            max_leagues_per_sport=1,
            max_odds_events=1,
            collected_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        )
        collect_historical_data(self.db, transport=transport, **arguments)
        second = FakeTransport()
        result = collect_historical_data(self.db, transport=second, **arguments)
        self.assertEqual(result["odds_events_collected"], 0)
        self.assertFalse(any(url == ODDS_URL for url, _, _ in second.calls))
        count = self.db.execute("SELECT COUNT(*) FROM multisport_historical_events").fetchone()[0]
        self.assertEqual(count, 1)

    def test_stored_event_missing_odds_is_backfilled_without_rediscovery(self):
        arguments = dict(
            api_key="private-key",
            sports=("epl",),
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 12, tzinfo=UTC),
            bookmakers=("DraftKings",),
            max_requests=10,
            max_leagues_per_sport=1,
            max_odds_events=1,
            collected_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        )
        collect_historical_data(
            self.db,
            transport=FakeTransport(),
            include_odds=False,
            **arguments,
        )

        transport = EmptyEventsTransport()
        result = collect_historical_data(
            self.db,
            transport=transport,
            include_odds=True,
            **arguments,
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["odds_events_collected"], 1)
        self.assertEqual(transport.calls[0][0], ODDS_URL)
        stored = self.db.execute(
            "SELECT odds_payload_json FROM multisport_historical_events"
        ).fetchone()
        self.assertIsNotNone(stored["odds_payload_json"])

    def test_verified_epl_odds_mode_skips_unverified_events(self):
        arguments = dict(
            api_key="private-key",
            sports=("epl",),
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 12, tzinfo=UTC),
            bookmakers=("DraftKings",),
            max_requests=10,
            max_leagues_per_sport=1,
            max_odds_events=1,
            collected_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        )
        collect_historical_data(
            self.db,
            transport=FakeTransport(),
            include_odds=False,
            **arguments,
        )
        self.db.execute(
            """
            INSERT INTO multisport_historical_event_verifications (
                provider_key, source_event_id, sport, verifier_key,
                verifier_event_id, verification_status, verified_at,
                evidence_json, test_only
            ) VALUES ('odds_api_io', '123', 'epl', ?, NULL, 'unmatched', ?, '{}', 1)
            """,
            (EPL_OFFICIAL_VERIFIER_KEY, "2026-08-12T12:00:00+00:00"),
        )
        self.db.commit()

        skipped = EmptyEventsTransport()
        result = collect_historical_data(
            self.db,
            transport=skipped,
            include_odds=True,
            verified_epl_only=True,
            **arguments,
        )
        self.assertEqual(result["odds_events_collected"], 0)
        self.assertFalse(any(url == ODDS_URL for url, _, _ in skipped.calls))

        self.db.execute(
            """
            UPDATE multisport_historical_event_verifications
            SET verification_status='verified', verifier_event_id='official-123'
            WHERE source_event_id='123'
            """
        )
        self.db.commit()
        verified = EmptyEventsTransport()
        result = collect_historical_data(
            self.db,
            transport=verified,
            include_odds=True,
            verified_epl_only=True,
            **arguments,
        )
        self.assertEqual(result["odds_events_collected"], 1)
        self.assertEqual(verified.calls[0][0], ODDS_URL)
        self.assertTrue(result["verified_epl_odds_only"])

    def test_verified_epl_odds_mode_rejects_unsafe_combinations(self):
        with self.assertRaisesRegex(ValueError, "only the epl sport"):
            collect_historical_data(
                self.db,
                api_key="private-key",
                transport=FakeTransport(),
                sports=("epl", "wnba"),
                start=datetime(2026, 8, 1, tzinfo=UTC),
                end=datetime(2026, 8, 12, tzinfo=UTC),
                bookmakers=("DraftKings",),
                include_odds=True,
                verified_epl_only=True,
            )

    def test_odds_only_mode_skips_all_discovery_requests(self):
        arguments = dict(
            api_key="private-key",
            sports=("epl",),
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 12, tzinfo=UTC),
            bookmakers=("DraftKings",),
            max_requests=10,
            max_leagues_per_sport=1,
            max_odds_events=1,
            collected_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        )
        collect_historical_data(
            self.db,
            transport=FakeTransport(),
            include_odds=False,
            **arguments,
        )
        self.db.execute(
            """
            INSERT INTO multisport_historical_event_verifications (
                provider_key, source_event_id, sport, verifier_key,
                verifier_event_id, verification_status, verified_at,
                evidence_json, test_only
            ) VALUES ('odds_api_io', '123', 'epl', ?, 'official-123',
                      'verified', ?, '{}', 1)
            """,
            (EPL_OFFICIAL_VERIFIER_KEY, "2026-08-12T12:00:00+00:00"),
        )
        self.db.commit()

        transport = FakeTransport()
        result = collect_historical_data(
            self.db,
            transport=transport,
            include_odds=True,
            verified_epl_only=True,
            odds_only=True,
            **arguments,
        )

        self.assertEqual([url for url, _, _ in transport.calls], [ODDS_URL])
        self.assertEqual(result["requests_made"], 1)
        self.assertEqual(result["events_seen"], 0)
        self.assertEqual(result["events_upserted"], 0)
        self.assertEqual(result["odds_events_collected"], 1)
        self.assertTrue(result["odds_only"])

    def test_odds_only_mode_requires_odds_collection(self):
        with self.assertRaisesRegex(ValueError, "requires --include-odds"):
            collect_historical_data(
                self.db,
                api_key="private-key",
                transport=FakeTransport(),
                sports=("epl",),
                start=datetime(2026, 8, 1, tzinfo=UTC),
                end=datetime(2026, 8, 12, tzinfo=UTC),
                odds_only=True,
            )

    def test_request_budget_records_a_partial_run(self):
        result = collect_historical_data(
            self.db,
            api_key="private-key",
            transport=FakeTransport(),
            sports=("epl",),
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 12, tzinfo=UTC),
            max_requests=2,
            collected_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["requests_made"], 2)
        run = self.db.execute("SELECT * FROM multisport_history_runs").fetchone()
        self.assertEqual(run["status"], "partial")
        self.assertIn("budget", run["error_summary"].casefold())

    def test_non_settled_rows_are_rejected(self):
        payload = [
            {
                "id": 1,
                "home": "A",
                "away": "B",
                "date": "2026-08-10T19:00:00Z",
                "status": "live",
                "sport": {"slug": "football"},
                "league": {"slug": "england-premier-league"},
                "scores": {"home": 0, "away": 0},
            }
        ]
        with self.assertRaises(ValueError):
            parse_historical_events(
                payload,
                sport="epl",
                provider_sport_slug="football",
                expected_league_slug="england-premier-league",
            )

    def test_league_discovery_keeps_sports_separate(self):
        payload = [
            {"eventsCount": 20, "name": "NBA", "slug": "nba"},
            {"eventsCount": 10, "name": "WNBA", "slug": "wnba"},
        ]
        self.assertEqual(select_leagues(payload, sport="nba", maximum=5), (("nba", "NBA"),))
        self.assertEqual(select_leagues(payload, sport="wnba", maximum=5), (("wnba", "WNBA"),))

    def test_nfl_and_mlb_league_discovery_excludes_other_competitions(self):
        payload = [
            {"eventsCount": 20, "name": "NFL Regular Season", "slug": "nfl-regular-season"},
            {"eventsCount": 18, "name": "College Football", "slug": "ncaaf"},
            {"eventsCount": 30, "name": "MLB Regular Season", "slug": "mlb-regular-season"},
            {"eventsCount": 1, "name": "World Series", "slug": "world-series"},
            {"eventsCount": 1, "name": "Super Bowl", "slug": "super-bowl"},
            {"eventsCount": 12, "name": "KBO Korea", "slug": "kbo"},
        ]
        self.assertEqual(
            select_leagues(payload, sport="nfl", maximum=5),
            (
                ("nfl-regular-season", "NFL Regular Season"),
                ("super-bowl", "Super Bowl"),
            ),
        )
        self.assertEqual(
            select_leagues(payload, sport="mlb", maximum=5),
            (
                ("mlb-regular-season", "MLB Regular Season"),
                ("world-series", "World Series"),
            ),
        )

    def test_small_shared_budget_rotates_nfl_and_mlb_to_the_front(self):
        sports = ("nfl", "mlb", "nba", "wnba", "epl", "mma", "atp", "wta")
        first_transport = RotationTransport()
        first = collect_historical_data(
            self.db,
            api_key="private-key",
            transport=first_transport,
            sports=sports,
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 2, tzinfo=UTC),
            max_requests=3,
            max_leagues_per_sport=1,
            collected_at=datetime(2026, 8, 2, 12, tzinfo=UTC),
        )
        self.assertEqual(first["processing_order"][0], "nfl")
        self.assertEqual(first["requests_made"], 3)
        first_league_call = next(call for call in first_transport.calls if call[0] == LEAGUES_URL)
        self.assertEqual(first_league_call[1]["sport"], "nfl")

        second_transport = RotationTransport()
        second = collect_historical_data(
            self.db,
            api_key="private-key",
            transport=second_transport,
            sports=sports,
            start=datetime(2026, 8, 2, tzinfo=UTC),
            end=datetime(2026, 8, 3, tzinfo=UTC),
            max_requests=3,
            max_leagues_per_sport=1,
            collected_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
        )
        self.assertEqual(second["processing_order"][0], "mlb")
        self.assertEqual(second["rotation_offset"], 1)
        second_league_call = next(call for call in second_transport.calls if call[0] == LEAGUES_URL)
        self.assertEqual(second_league_call[1]["sport"], "mlb")

    def test_duplicate_sports_are_rejected_before_spending_requests(self):
        transport = RotationTransport()
        with self.assertRaises(ValueError):
            collect_historical_data(
                self.db,
                api_key="private-key",
                transport=transport,
                sports=("nfl", "nfl"),
                start=datetime(2026, 8, 1, tzinfo=UTC),
                end=datetime(2026, 8, 2, tzinfo=UTC),
            )
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
