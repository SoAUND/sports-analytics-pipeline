import json
import unittest
from datetime import datetime, timedelta, timezone

from sports_analytics_pipeline.collectors.http import HttpResponse
from sports_analytics_pipeline.collectors.multisport_live_research import (
    collect_automatic_live_research,
)
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.multisport_operations import bootstrap


UTC = timezone.utc
NOW = datetime(2026, 8, 22, 18, 0, tzinfo=UTC)


class FakeTransport:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def post_json(self, url, *, payload, headers=None):
        self.calls.append((url, dict(payload), dict(headers or {})))
        payload = self.payloads[url]
        return HttpResponse(
            status=200,
            body=json.dumps(payload).encode("utf-8"),
            headers={},
            received_at=NOW,
        )


class AutomaticLiveResearchCollectorTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)
        bootstrap(self.db, as_of=NOW)
        self.event_id = self._event("nba")

    def tearDown(self):
        self.db.close()

    def _event(self, sport):
        provider = {
            "nba": "basketball_nba",
            "epl": "soccer_epl",
        }[sport]
        participant_type = "team"
        for role in ("home", "away"):
            participant_id = f"{sport}:{role}"
            self.db.execute(
                """
                INSERT INTO multisport_participants (
                    participant_id, sport, provider_sport_key, source_participant_id,
                    full_name, participant_type, first_seen_at, last_seen_at, active_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'provider_listed')
                """,
                (
                    participant_id, sport, provider, role, f"{sport.upper()} {role}",
                    participant_type, NOW.isoformat(), NOW.isoformat(),
                ),
            )
        event_id = f"{sport}:{provider}:event"
        self.db.execute(
            """
            INSERT INTO multisport_events (
                event_id, sport, provider_sport_key, source_event_id, commence_time,
                home_participant_id, away_participant_id, status,
                first_seen_at, last_seen_at, test_only
            ) VALUES (?, ?, ?, 'event', ?, ?, ?, 'scheduled', ?, ?, 1)
            """,
            (
                event_id, sport, provider, (NOW + timedelta(hours=2)).isoformat(),
                f"{sport}:home", f"{sport}:away", NOW.isoformat(), NOW.isoformat(),
            ),
        )
        self.db.commit()
        return event_id

    def _row(self, **overrides):
        row = {
            "event_id": self.event_id,
            "sport": "nba",
            "category": "official_status",
            "source_key": "nba_official",
            "verification_status": "verified",
            "source_updated_at": NOW.isoformat(),
            "collected_at": NOW.isoformat(),
            "payload": {
                "event_id": self.event_id,
                "game_status": "scheduled",
                "material_change": False,
            },
        }
        row.update(overrides)
        return row

    def test_authenticated_official_feed_is_stored_and_deduplicated(self):
        url = "https://feeds.example/nba-official"
        transport = FakeTransport({url: {"observations": [self._row()]}})
        first = collect_automatic_live_research(
            self.db,
            sports=("nba",),
            as_of=NOW,
            feed_urls={"nba_official": url},
            bearer_token="feed-secret",
            transport=transport,
        )
        second = collect_automatic_live_research(
            self.db,
            sports=("nba",),
            as_of=NOW,
            feed_urls={"nba_official": url},
            bearer_token="feed-secret",
            transport=transport,
        )
        self.assertEqual(first["stored"], 1)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[0][2]["Authorization"], "Bearer feed-secret")
        request = transport.calls[0][1]
        self.assertEqual(request["contract_version"], 1)
        self.assertEqual(request["source_key"], "nba_official")
        self.assertEqual(request["events"][0]["event_id"], self.event_id)
        self.assertTrue(request["test_only"])
        row = self.db.execute(
            "SELECT * FROM multisport_research_checks WHERE event_id=?",
            (self.event_id,),
        ).fetchone()
        self.assertEqual(row["source_key"], "nba_official")
        self.assertEqual(row["official"], 1)
        self.assertEqual(row["source_url"], "https://official.nba.com/")
        self.assertNotIn("feed-secret", repr(first))
        self.assertNotIn(url, repr(first))

    def test_official_feed_without_authentication_is_blocked_before_request(self):
        url = "https://feeds.example/nba-official"
        transport = FakeTransport({url: [self._row()]})
        result = collect_automatic_live_research(
            self.db,
            sports=("nba",),
            as_of=NOW,
            feed_urls={"nba_official": url},
            bearer_token=None,
            transport=transport,
        )
        self.assertEqual(result["requests_made"], 0)
        self.assertEqual(result["stored"], 0)
        self.assertEqual(transport.calls, [])
        self.assertIn(
            "nba_official:authenticated_feed_required_for_official_source",
            result["errors"],
        )

    def test_category_mismatch_never_becomes_verified_research(self):
        url = "https://feeds.example/nba-official"
        transport = FakeTransport({url: [self._row(category="injury_availability")]})
        result = collect_automatic_live_research(
            self.db,
            sports=("nba",),
            as_of=NOW,
            feed_urls={"nba_official": url},
            bearer_token="feed-secret",
            transport=transport,
        )
        self.assertEqual(result["stored"], 0)
        self.assertTrue(any("category_mismatch" in error for error in result["errors"]))
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM multisport_research_checks").fetchone()[0],
            0,
        )

    def test_adapter_unavailable_is_reported_as_a_blocker_not_collected(self):
        url = "https://feeds.example/nba-official"
        transport = FakeTransport(
            {
                url: {
                    "status": "unavailable",
                    "reason": "source_specific_connector_not_yet_verified",
                    "observations": [],
                }
            }
        )
        result = collect_automatic_live_research(
            self.db,
            sports=("nba",),
            as_of=NOW,
            feed_urls={"nba_official": url},
            bearer_token="feed-secret",
            transport=transport,
        )
        self.assertEqual(result["status"], "completed_with_blockers")
        self.assertEqual(result["stored"], 0)
        self.assertEqual(result["errors"], ())
        self.assertEqual(result["sources"]["nba_official"]["status"], "unavailable")
        self.assertEqual(
            result["sources"]["nba_official"]["reason"],
            "source_specific_connector_not_yet_verified",
        )

    def test_adapter_event_rejection_reason_is_preserved_safely(self):
        url = "https://feeds.example/nba-official"
        transport = FakeTransport(
            {
                url: {
                    "status": "unavailable",
                    "observations": [],
                    "unavailable": [
                        {
                            "event_id": self.event_id,
                            "reason": "official_previous_match_not_found:NBA Home",
                        }
                    ],
                }
            }
        )
        result = collect_automatic_live_research(
            self.db,
            sports=("nba",),
            as_of=NOW,
            feed_urls={"nba_official": url},
            bearer_token="feed-secret",
            transport=transport,
        )
        self.assertEqual(
            result["sources"]["nba_official"]["unavailable_events"],
            (
                {
                    "event_id": self.event_id,
                    "reason": "official_previous_match_not_found:NBA Home",
                },
            ),
        )

    def test_adapter_event_rejection_reason_cannot_inject_free_form_text(self):
        url = "https://feeds.example/nba-official"
        transport = FakeTransport(
            {
                url: {
                    "status": "unavailable",
                    "observations": [],
                    "unavailable": [
                        {"event_id": self.event_id, "reason": "bad\nsecret=https://example"}
                    ],
                }
            }
        )
        result = collect_automatic_live_research(
            self.db,
            sports=("nba",),
            as_of=NOW,
            feed_urls={"nba_official": url},
            bearer_token="feed-secret",
            transport=transport,
        )
        self.assertEqual(
            result["sources"]["nba_official"]["unavailable_events"][0]["reason"],
            "adapter_reported_unavailable",
        )

    def test_unavailable_adapter_cannot_smuggle_observations(self):
        url = "https://feeds.example/nba-official"
        transport = FakeTransport(
            {
                url: {
                    "status": "unavailable",
                    "observations": [self._row()],
                }
            }
        )
        result = collect_automatic_live_research(
            self.db,
            sports=("nba",),
            as_of=NOW,
            feed_urls={"nba_official": url},
            bearer_token="feed-secret",
            transport=transport,
        )
        self.assertEqual(result["stored"], 0)
        self.assertEqual(result["sources"]["nba_official"]["status"], "failed")
        self.assertTrue(any("cannot include observations" in error for error in result["errors"]))

    def test_request_cap_stops_before_extra_configured_sources(self):
        epl_event = self._event("epl")
        urls = {
            "nba_official": "https://feeds.example/1",
            "nba_injury_report": "https://feeds.example/2",
            "premier_league": "https://feeds.example/3",
        }
        payloads = {
            urls["nba_official"]: [self._row()],
            urls["nba_injury_report"]: [
                self._row(
                    category="injury_availability",
                    source_key="nba_injury_report",
                )
            ],
            urls["premier_league"]: [
                self._row(
                    event_id=epl_event,
                    sport="epl",
                    source_key="premier_league",
                    payload={"event_id": epl_event, "game_status": "scheduled"},
                )
            ],
        }
        transport = FakeTransport(payloads)
        result = collect_automatic_live_research(
            self.db,
            sports=("nba", "epl"),
            as_of=NOW,
            feed_urls=urls,
            bearer_token="feed-secret",
            transport=transport,
            max_requests=2,
        )
        self.assertEqual(result["requests_made"], 2)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(result["errors"], ())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["deferred_sources"]), 1)

    def test_lower_frequency_sources_rotate_without_displacing_critical_sources(self):
        urls = {
            "nba_official": "https://feeds.example/official",
            "nba_stats": "https://feeds.example/stats",
            "nba_transactions": "https://feeds.example/transactions",
        }
        payloads = {url: {"observations": []} for url in urls.values()}
        first_transport = FakeTransport(payloads)
        second_transport = FakeTransport(payloads)
        first = collect_automatic_live_research(
            self.db,
            sports=("nba",),
            as_of=NOW,
            feed_urls=urls,
            bearer_token="feed-secret",
            transport=first_transport,
            max_requests=2,
        )
        second = collect_automatic_live_research(
            self.db,
            sports=("nba",),
            as_of=NOW + timedelta(hours=1),
            feed_urls=urls,
            bearer_token="feed-secret",
            transport=second_transport,
            horizon_minutes=360,
            max_requests=2,
        )
        self.assertEqual(first_transport.calls[0][1]["source_key"], "nba_official")
        self.assertEqual(second_transport.calls[0][1]["source_key"], "nba_official")
        self.assertNotEqual(first["deferred_sources"], second["deferred_sources"])

    def test_legacy_epl_feed_url_uses_canonical_source_and_reviewed_home_venue(self):
        event_id = self._event("epl")
        self.db.execute("UPDATE multisport_participants SET full_name='Bournemouth' WHERE participant_id='epl:home'")
        self.db.execute("UPDATE multisport_participants SET full_name='Everton' WHERE participant_id='epl:away'")
        self.db.commit()
        url = "https://feeds.example/legacy-fbref"
        row = {
            "event_id": event_id,
            "sport": "epl",
            "category": "statistics_analytics",
            "source_key": "premier_league_statistics",
            "verification_status": "verified",
            "source_updated_at": NOW.isoformat(),
            "collected_at": NOW.isoformat(),
            "payload": {
                "baseline_features": {
                    "recent5_points_diff": 1,
                    "home_away_strength_diff": 0.2,
                    "rest_days_diff": 0,
                    "fixture_congestion_diff": 0,
                },
                "material_change": False,
            },
        }
        transport = FakeTransport({url: {"observations": [row]}})
        result = collect_automatic_live_research(
            self.db,
            sports=("epl",),
            as_of=NOW,
            feed_urls={"fbref": url},
            bearer_token="feed-secret",
            transport=transport,
        )
        self.assertEqual(result["stored"], 1)
        request = transport.calls[0][1]
        self.assertEqual(request["source_key"], "premier_league_statistics")
        self.assertEqual(request["events"][0]["venue"]["venue_id"], "vitality_stadium")
        self.assertTrue(request["events"][0]["venue"]["reviewed"])


if __name__ == "__main__":
    unittest.main()
