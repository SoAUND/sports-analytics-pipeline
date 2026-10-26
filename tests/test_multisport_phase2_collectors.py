import json
import unittest
from datetime import datetime, timezone

from sports_analytics_pipeline.collectors.http import HttpResponse
from sports_analytics_pipeline.collectors.multisport_odds import (
    MultiSportOddsCollector,
    ParticipantDirectory,
    parse_events,
    parse_current_odds,
    parse_participants,
)
from sports_analytics_pipeline.data.db import connect, initialize_schema, multisport_table_names
from sports_analytics_pipeline.multisport_research import (
    all_research_sports,
    record_research_check,
    research_gate,
    required_categories,
    research_sources,
    verify_source_coverage,
)
from sports_analytics_pipeline.multisport_storage import (
    persist_collection_cycle,
    seed_validation_state,
)
from sports_analytics_pipeline.sports_registry import validation_bucket


UTC = timezone.utc
NOW = datetime(2026, 8, 17, 22, 0, tzinfo=UTC)
STAMP = "2026-08-17T21:59:00Z"


class FakeTransport:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, dict(params or {})))
        path = url.split("/v4/", 1)[1]
        key = path.rstrip("/")
        try:
            payload = self.payloads[key]
        except KeyError as exc:
            raise AssertionError(f"Unexpected fake GET: {key}") from exc
        return HttpResponse(
            status=200,
            body=json.dumps(payload).encode("utf-8"),
            headers={
                "x-requests-remaining": "499",
                "x-requests-used": "1",
                "x-requests-last": "1",
            },
            received_at=NOW,
        )


def participant_payload(home="Boston Celtics", away="New York Knicks"):
    return [
        {"id": "p-home", "full_name": home},
        {"id": "p-away", "full_name": away},
    ]


def event_payload(sport_key, home="Boston Celtics", away="New York Knicks"):
    return [
        {
            "id": "event-1",
            "sport_key": sport_key,
            "sport_title": "Example",
            "commence_time": "2026-08-18T00:00:00Z",
            "home_team": home,
            "away_team": away,
        }
    ]


def odds_payload(
    sport_key,
    market_key,
    outcomes,
    home="Boston Celtics",
    away="New York Knicks",
):
    return [
        {
            "id": "event-1",
            "sport_key": sport_key,
            "commence_time": "2026-08-18T00:00:00Z",
            "home_team": home,
            "away_team": away,
            "bookmakers": [
                {
                    "key": "hardrockbet",
                    "title": "Primary Sportsbook",
                    "last_update": STAMP,
                    "markets": [
                        {
                            "key": market_key,
                            "last_update": STAMP,
                            "outcomes": outcomes,
                        }
                    ],
                }
            ],
        }
    ]


class MultiSportPhase2CollectorTests(unittest.TestCase):
    def test_exact_tennis_tournament_key_supplies_reviewed_venue(self):
        participants = parse_participants(
            participant_payload("Player A", "Player B"),
            sport="atp",
            provider_sport_key="tennis_atp_cincinnati_open",
        )
        payload = event_payload(
            "tennis_atp_cincinnati_open",
            "Player A",
            "Player B",
        )
        events = parse_events(
            payload,
            sport="atp",
            provider_sport_key="tennis_atp_cincinnati_open",
            participants=ParticipantDirectory(participants),
        )
        self.assertEqual(events[0].venue_id, "lindner_family_tennis_center")
        self.assertEqual(events[0].venue_name, "Lindner Family Tennis Center")

    def test_exact_monterrey_key_supplies_reviewed_wta_venue(self):
        participants = parse_participants(
            participant_payload("Clara Tauson", "Renata Zarazua"),
            sport="wta",
            provider_sport_key="tennis_wta_monterrey_open",
        )
        payload = event_payload(
            "tennis_wta_monterrey_open",
            "Clara Tauson",
            "Renata Zarazua",
        )
        events = parse_events(
            payload,
            sport="wta",
            provider_sport_key="tennis_wta_monterrey_open",
            participants=ParticipantDirectory(participants),
        )
        self.assertEqual(events[0].venue_id, "club_sonoma")
        self.assertEqual(events[0].venue_name, "Club Sonoma")

    def test_event_venue_alias_is_canonicalized_without_becoming_required(self):
        participants = parse_participants(
            participant_payload(), sport="nba", provider_sport_key="basketball_nba"
        )
        payload = event_payload("basketball_nba")
        payload[0]["venue"] = {"name": "Rocket Mortgage FieldHouse"}
        events = parse_events(
            payload,
            sport="nba",
            provider_sport_key="basketball_nba",
            participants=ParticipantDirectory(participants),
        )
        self.assertEqual(events[0].venue_id, "rocket_arena")
        self.assertEqual(events[0].venue_name, "Rocket Arena")

    def test_unknown_event_venue_is_preserved_and_never_blocks_event(self):
        participants = parse_participants(
            participant_payload("Fighter A", "Fighter B"),
            sport="mma",
            provider_sport_key="mma_mixed_martial_arts",
        )
        payload = event_payload("mma_mixed_martial_arts", "Fighter A", "Fighter B")
        payload[0]["venue"] = "One-Night Pop-Up Venue"
        events = parse_events(
            payload,
            sport="mma",
            provider_sport_key="mma_mixed_martial_arts",
            participants=ParticipantDirectory(participants),
        )
        self.assertIsNone(events[0].venue_id)
        self.assertEqual(events[0].venue_name, "One-Night Pop-Up Venue")

    def test_nba_spread_is_normalized_with_exact_opposite_points(self):
        participants = parse_participants(
            participant_payload(), sport="nba", provider_sport_key="basketball_nba"
        )
        batch = parse_current_odds(
            odds_payload(
                "basketball_nba",
                "spreads",
                [
                    {"name": "Boston Celtics", "price": 1.91, "point": -5.5},
                    {"name": "New York Knicks", "price": 1.91, "point": 5.5},
                ],
            ),
            sport="nba",
            provider_sport_key="basketball_nba",
            participants=ParticipantDirectory(participants),
            collected_at=NOW,
        )
        self.assertEqual(len(batch.quotes), 2)
        self.assertEqual({q.outcome_role for q in batch.quotes}, {"home", "away"})
        self.assertEqual({float(q.points) for q in batch.quotes}, {-5.5, 5.5})
        self.assertTrue(all(q.market_key == "spreads" for q in batch.quotes))
        self.assertTrue(all(q.settlement_rule == "pending_sportsbook_rule_verification" for q in batch.quotes))

    def test_spread_parser_rejects_non_opposite_points(self):
        participants = parse_participants(
            participant_payload(), sport="wnba", provider_sport_key="basketball_wnba"
        )
        with self.assertRaises(ValueError):
            parse_current_odds(
                odds_payload(
                    "basketball_wnba",
                    "spreads",
                    [
                        {"name": "Boston Celtics", "price": 1.91, "point": -4.5},
                        {"name": "New York Knicks", "price": 1.91, "point": 5.5},
                    ],
                ),
                sport="wnba",
                provider_sport_key="basketball_wnba",
                participants=ParticipantDirectory(participants),
                collected_at=NOW,
            )

    def test_invalid_price_skips_only_that_bookmaker_market(self):
        participants = parse_participants(
            participant_payload("Clara Tauson", "Renata Zarazua"),
            sport="wta",
            provider_sport_key="tennis_wta_monterrey_open",
        )
        payload = odds_payload(
            "tennis_wta_monterrey_open",
            "h2h",
            [
                {"name": "Clara Tauson", "price": 1.0},
                {"name": "Renata Zarazua", "price": 2.2},
            ],
            home="Clara Tauson",
            away="Renata Zarazua",
        )
        payload[0]["bookmakers"].append(
            {
                "key": "validbook",
                "title": "Valid Book",
                "last_update": STAMP,
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": STAMP,
                        "outcomes": [
                            {"name": "Clara Tauson", "price": 1.7},
                            {"name": "Renata Zarazua", "price": 2.15},
                        ],
                    }
                ],
            }
        )

        batch = parse_current_odds(
            payload,
            sport="wta",
            provider_sport_key="tennis_wta_monterrey_open",
            participants=ParticipantDirectory(participants),
            collected_at=NOW,
        )

        self.assertEqual(len(batch.quotes), 2)
        self.assertEqual({quote.bookmaker_key for quote in batch.quotes}, {"validbook"})
        self.assertEqual(len(batch.warnings), 1)
        self.assertIn("Skipped invalid price market", batch.warnings[0])
        self.assertIn("hardrockbet", batch.warnings[0])

    def test_epl_three_way_market_requires_draw(self):
        participants = parse_participants(
            participant_payload("Arsenal", "Chelsea"),
            sport="epl",
            provider_sport_key="soccer_epl",
        )
        batch = parse_current_odds(
            odds_payload(
                "soccer_epl",
                "h2h",
                [
                    {"name": "Chelsea", "price": 3.4},
                    {"name": "Draw", "price": 3.2},
                    {"name": "Arsenal", "price": 2.1},
                ],
                home="Arsenal",
                away="Chelsea",
            ),
            sport="epl",
            provider_sport_key="soccer_epl",
            participants=ParticipantDirectory(participants),
            collected_at=NOW,
        )
        self.assertEqual(len(batch.quotes), 3)
        draw = next(q for q in batch.quotes if q.outcome_role == "draw")
        self.assertIsNone(draw.outcome_participant_id)
        self.assertEqual(draw.market_key, "three_way_moneyline")

    def test_epl_parser_rejects_two_way_h2h(self):
        participants = parse_participants(
            participant_payload("Arsenal", "Chelsea"),
            sport="epl",
            provider_sport_key="soccer_epl",
        )
        with self.assertRaises(ValueError):
            parse_current_odds(
                odds_payload(
                    "soccer_epl",
                    "h2h",
                    [
                        {"name": "Chelsea", "price": 2.4},
                        {"name": "Arsenal", "price": 1.7},
                    ],
                    home="Arsenal",
                    away="Chelsea",
                ),
                sport="epl",
                provider_sport_key="soccer_epl",
                participants=ParticipantDirectory(participants),
                collected_at=NOW,
            )

    def test_atp_and_wta_keys_cannot_cross_tours(self):
        parse_participants(
            participant_payload("Player A", "Player B"),
            sport="atp",
            provider_sport_key="tennis_atp_cincinnati_open",
        )
        with self.assertRaises(ValueError):
            parse_participants(
                participant_payload("Player A", "Player B"),
                sport="atp",
                provider_sport_key="tennis_wta_cincinnati_open",
            )

    def test_collect_sport_discovers_and_collects_current_nba(self):
        payloads = {
            "sports": [
                {"key": "basketball_nba", "active": True},
                {"key": "basketball_wnba", "active": True},
            ],
            "sports/basketball_nba/participants": participant_payload(),
            "sports/basketball_nba/events": event_payload("basketball_nba"),
            "sports/basketball_nba/odds": odds_payload(
                "basketball_nba",
                "spreads",
                [
                    {"name": "Boston Celtics", "price": 1.91, "point": -3.5},
                    {"name": "New York Knicks", "price": 1.91, "point": 3.5},
                ],
            ),
        }
        transport = FakeTransport(payloads)
        collector = MultiSportOddsCollector(api_key="secret", transport=transport)
        bundle = collector.collect_sport("nba")
        self.assertEqual(bundle.active_provider_keys, ("basketball_nba",))
        self.assertEqual(len(bundle.participant_batches), 1)
        self.assertEqual(len(bundle.event_batches), 1)
        self.assertEqual(len(bundle.odds_batches), 1)
        odds_call = next(call for call in transport.calls if call[0].rstrip("/").endswith("/odds"))
        self.assertEqual(odds_call[1]["markets"], "spreads")
        self.assertNotIn("secret", repr(bundle))

    def test_collect_tennis_discovers_multiple_active_tournaments(self):
        payloads = {
            "sports": [
                {"key": "tennis_atp_cincinnati_open", "active": True},
                {"key": "tennis_atp_us_open", "active": True},
                {"key": "tennis_wta_cincinnati_open", "active": True},
            ]
        }
        for key in ("tennis_atp_cincinnati_open", "tennis_atp_us_open"):
            payloads[f"sports/{key}/participants"] = participant_payload("Player A", "Player B")
            payloads[f"sports/{key}/events"] = event_payload(key, "Player A", "Player B")
            payloads[f"sports/{key}/odds"] = odds_payload(
                key,
                "h2h",
                [
                    {"name": "Player A", "price": 1.6},
                    {"name": "Player B", "price": 2.3},
                ],
                home="Player A",
                away="Player B",
            )
        collector = MultiSportOddsCollector(api_key="secret", transport=FakeTransport(payloads))
        bundle = collector.collect_sport("atp")
        self.assertEqual(
            bundle.active_provider_keys,
            ("tennis_atp_cincinnati_open", "tennis_atp_us_open"),
        )
        self.assertEqual(len(bundle.odds_batches), 2)

    def test_inactive_sport_returns_empty_test_bundle(self):
        collector = MultiSportOddsCollector(
            api_key="secret",
            transport=FakeTransport({"sports": [{"key": "basketball_nba", "active": False}]}),
        )
        bundle = collector.collect_sport("nba")
        self.assertEqual(bundle.active_provider_keys, ())
        self.assertTrue(bundle.warnings)


class MultiSportPhase2StorageAndResearchTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)

    def tearDown(self):
        self.db.close()

    def _nba_batches(self):
        payloads = {
            "sports": [{"key": "basketball_nba", "active": True}],
            "sports/basketball_nba/participants": participant_payload(),
            "sports/basketball_nba/events": event_payload("basketball_nba"),
            "sports/basketball_nba/odds": odds_payload(
                "basketball_nba",
                "spreads",
                [
                    {"name": "Boston Celtics", "price": 1.91, "point": -2.5},
                    {"name": "New York Knicks", "price": 1.91, "point": 2.5},
                ],
            ),
        }
        collector = MultiSportOddsCollector(api_key="secret", transport=FakeTransport(payloads))
        bundle = collector.collect_sport("nba")
        return (
            bundle.participant_batches[0],
            bundle.event_batches[0],
            bundle.odds_batches[0],
        )

    def test_additive_multisport_tables_exist_without_widening_legacy_tables(self):
        tables = {
            row[0]
            for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertTrue(multisport_table_names().issubset(tables))
        teams_sql = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='teams'"
        ).fetchone()[0]
        self.assertIn("sport IN ('nfl', 'mlb')", teams_sql)

    def test_collection_cycle_persists_test_only_participants_events_and_quotes(self):
        participants, events, odds = self._nba_batches()
        counts = persist_collection_cycle(
            self.db,
            participants=participants,
            events=events,
            odds=odds,
        )
        self.assertEqual(counts, {"participants": 2, "events": 1, "quotes": 2})
        event = self.db.execute("SELECT * FROM multisport_events").fetchone()
        self.assertEqual(event["sport"], "nba")
        self.assertEqual(event["test_only"], 1)
        quotes = self.db.execute("SELECT * FROM multisport_odds_snapshots").fetchall()
        self.assertEqual(len(quotes), 2)
        self.assertTrue(all(row["test_only"] == 1 for row in quotes))

    def test_each_sport_gets_separate_locked_validation_state(self):
        self.assertEqual(seed_validation_state(self.db, as_of=NOW), 6)
        rows = self.db.execute(
            "SELECT validation_bucket, sport, target_decisions, approved, status FROM multisport_validation_state"
        ).fetchall()
        self.assertEqual(len(rows), 6)
        self.assertEqual(len({row["validation_bucket"] for row in rows}), 6)
        self.assertTrue(all(row["target_decisions"] == 100 for row in rows))
        self.assertTrue(all(row["approved"] == 0 for row in rows))
        self.assertTrue(all(row["status"] == "test_only" for row in rows))
        self.assertNotEqual(validation_bucket("atp"), validation_bucket("wta"))

    def test_research_directory_covers_every_mandatory_category(self):
        for sport in all_research_sports():
            complete, missing = verify_source_coverage(sport)
            self.assertTrue(complete, (sport, missing))
            self.assertTrue(research_sources(sport))

    def test_research_gate_blocks_until_every_category_is_verified(self):
        participants, events, odds = self._nba_batches()
        persist_collection_cycle(
            self.db,
            participants=participants,
            events=events,
            odds=odds,
        )
        event_id = self.db.execute("SELECT event_id FROM multisport_events").fetchone()[0]
        ready, blockers = research_gate(self.db, event_id=event_id, sport="nba")
        self.assertFalse(ready)
        self.assertGreater(len(blockers), 0)

        source_by_category = {spec.category: spec for spec in research_sources("nba")}
        for category in required_categories("nba"):
            spec = source_by_category[category]
            record_research_check(
                self.db,
                event_id=event_id,
                sport="nba",
                category=category,
                source_key=spec.source_key,
                verification_status="verified",
                official=spec.official,
                source_url=spec.url,
                source_updated_at=NOW,
                collected_at=NOW,
            )
        ready, blockers = research_gate(self.db, event_id=event_id, sport="nba")
        self.assertTrue(ready)
        self.assertEqual(blockers, ())

    def test_conflicting_research_check_blocks_pick_gate(self):
        participants, events, odds = self._nba_batches()
        persist_collection_cycle(
            self.db,
            participants=participants,
            events=events,
            odds=odds,
        )
        event_id = self.db.execute("SELECT event_id FROM multisport_events").fetchone()[0]
        source_by_category = {spec.category: spec for spec in research_sources("nba")}
        for category in required_categories("nba"):
            spec = source_by_category[category]
            status = "conflict" if category == "injury_availability" else "verified"
            record_research_check(
                self.db,
                event_id=event_id,
                sport="nba",
                category=category,
                source_key=spec.source_key,
                verification_status=status,
                official=spec.official,
                collected_at=NOW,
            )
        ready, blockers = research_gate(self.db, event_id=event_id, sport="nba")
        self.assertFalse(ready)
        self.assertIn("injury_availability:conflict", blockers)


if __name__ == "__main__":
    unittest.main()
