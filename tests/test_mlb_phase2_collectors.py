import json
import unittest
from datetime import datetime, timedelta, timezone

from sports_analytics_pipeline.collectors.base import MappingTeamResolver
from sports_analytics_pipeline.collectors.http import HttpResponse
from sports_analytics_pipeline.collectors.mlb_official import (
    MappingStadiumResolver,
    MlbScheduleCollector,
    OfficialMlbPageCollector,
    parse_mlb_schedule,
)
from sports_analytics_pipeline.collectors.storage import CollectorRepository
from sports_analytics_pipeline.collectors.the_odds_api import (
    MLB_SPORT_KEY,
    TheOddsApiCollector,
    parse_current_mlb_moneylines,
)
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import (
    alias_mapping,
    seed_reference_data,
    stadium_alias_mapping,
    stadium_provider_id_mapping,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 16, 16, 0, tzinfo=UTC)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if not self.responses:
            raise AssertionError("Unexpected HTTP request")
        return self.responses.pop(0)


def response(payload, *, received_at=NOW, status=200, headers=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return HttpResponse(status=status, body=body, headers=headers or {}, received_at=received_at)


class MLBPhase2CollectorTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)
        seed_reference_data(self.db, include_mlb=True)
        self.teams = MappingTeamResolver(alias_mapping(self.db, "mlb"))
        self.stadiums = MappingStadiumResolver(
            stadium_alias_mapping(self.db, "mlb"),
            provider_ids=stadium_provider_id_mapping("mlb"),
        )

    def tearDown(self):
        self.db.close()

    def moneyline_payload(self, *, last_update="2026-08-16T15:59:00Z"):
        return [
            {
                "id": "odds-event-1",
                "sport_key": "baseball_mlb",
                "commence_time": "2026-08-16T20:00:00Z",
                "home_team": "New York Yankees",
                "away_team": "Boston Red Sox",
                "bookmakers": [
                    {
                        "key": "hardrockbet",
                        "title": "Primary Sportsbook",
                        "last_update": last_update,
                        "markets": [
                            {
                                "key": "h2h",
                                "last_update": last_update,
                                "outcomes": [
                                    {"name": "New York Yankees", "price": 1.80},
                                    {"name": "Boston Red Sox", "price": 2.10},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]

    def schedule_payload(self):
        return {
            "dates": [
                {
                    "date": "2026-08-16",
                    "games": [
                        {
                            "gamePk": 777001,
                            "season": "2026",
                            "gameDate": "2026-08-16T20:00:00Z",
                            "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
                            "venue": {"id": 3313, "name": "Yankee Stadium"},
                            "teams": {
                                "home": {
                                    "team": {"name": "New York Yankees"},
                                    "probablePitcher": {"id": 1001, "fullName": "Home Starter"},
                                },
                                "away": {
                                    "team": {"name": "Boston Red Sox"},
                                    "probablePitcher": {"id": 1002, "fullName": "Away Starter"},
                                },
                            },
                        }
                    ],
                }
            ]
        }

    def test_mlb_moneyline_parser_normalizes_exact_two_sided_market(self):
        batch = parse_current_mlb_moneylines(self.moneyline_payload(), self.teams, NOW)
        self.assertEqual(len(batch.quotes), 2)
        self.assertEqual({q.market_key for q in batch.quotes}, {"moneylines"})
        self.assertEqual({q.sport for q in batch.quotes}, {"mlb"})
        self.assertTrue(all(q.points is None for q in batch.quotes))
        self.assertEqual({q.outcome_team_id for q in batch.quotes}, {"mlb_nyy", "mlb_bos"})

    def test_mlb_moneyline_parser_rejects_missing_or_unrelated_side(self):
        payload = self.moneyline_payload()
        payload[0]["bookmakers"][0]["markets"][0]["outcomes"] = [
            {"name": "New York Yankees", "price": 1.80}
        ]
        with self.assertRaises(ValueError):
            parse_current_mlb_moneylines(payload, self.teams, NOW)

        payload = self.moneyline_payload()
        payload[0]["bookmakers"][0]["markets"][0]["outcomes"][1]["name"] = "Los Angeles Dodgers"
        with self.assertRaises(ValueError):
            parse_current_mlb_moneylines(payload, self.teams, NOW)

    def test_mlb_moneyline_parser_rejects_future_source_update(self):
        with self.assertRaises(ValueError):
            parse_current_mlb_moneylines(
                self.moneyline_payload(last_update="2026-08-16T16:06:00Z"), self.teams, NOW
            )

    def test_odds_client_uses_mlb_h2h_and_preserves_quota_metadata(self):
        transport = FakeTransport([
            response(
                self.moneyline_payload(),
                headers={"x-requests-remaining": "499", "x-requests-used": "1", "x-requests-last": "1"},
            )
        ])
        collector = TheOddsApiCollector(
            api_key="secret",
            resolver=self.teams,
            transport=transport,
            bookmakers=("hardrockbet",),
            sport_key=MLB_SPORT_KEY,
        )
        batch = collector.collect_current_spreads()
        url, params, _headers = transport.calls[0]
        self.assertIn("/sports/baseball_mlb/odds/", url)
        self.assertEqual(params["markets"], "h2h")
        self.assertEqual(params["bookmakers"], "hardrockbet")
        self.assertEqual(batch.metadata["endpoint"], "current_mlb_odds")
        self.assertEqual(batch.metadata["requests_remaining"], 499)

    def test_schedule_parser_maps_game_venue_and_probable_pitchers(self):
        batch = parse_mlb_schedule(
            self.schedule_payload(),
            team_resolver=self.teams,
            stadium_resolver=self.stadiums,
            collected_at=NOW,
            expected_date="2026-08-16",
        )
        self.assertEqual(len(batch.games), 1)
        game = batch.games[0]
        self.assertEqual(game.source_game_id, "777001")
        self.assertEqual(game.home_team_id, "mlb_nyy")
        self.assertEqual(game.away_team_id, "mlb_bos")
        self.assertEqual(game.stadium_id, "yankee_stadium")
        self.assertEqual(game.probable_home_pitcher.player_id, "mlb_person_1001")
        self.assertEqual(game.probable_away_pitcher.player_name, "Away Starter")

    def test_schedule_parser_skips_unknown_venue_without_failing_batch(self):
        payload = self.schedule_payload()
        payload["dates"][0]["games"][0]["venue"].pop("id")
        payload["dates"][0]["games"][0]["venue"]["name"] = "Invented Baseball Park"
        batch = parse_mlb_schedule(
            payload,
            team_resolver=self.teams,
            stadium_resolver=self.stadiums,
            collected_at=NOW,
            expected_date="2026-08-16",
        )
        self.assertEqual(batch.games, ())
        self.assertIn("Unknown MLB stadium alias", batch.warnings[0])

    def test_schedule_parser_prefers_stable_provider_venue_id(self):
        payload = self.schedule_payload()
        payload["dates"][0]["games"][0]["venue"]["name"] = "Temporary Sponsor Label"
        batch = parse_mlb_schedule(
            payload,
            team_resolver=self.teams,
            stadium_resolver=self.stadiums,
            collected_at=NOW,
            expected_date="2026-08-16",
        )
        self.assertEqual(batch.games[0].stadium_id, "yankee_stadium")
        self.assertEqual(batch.warnings, ())

    def test_schedule_collector_uses_official_stats_service_read_only_request(self):
        transport = FakeTransport([response(self.schedule_payload())])
        batch = MlbScheduleCollector(
            transport=transport, team_resolver=self.teams, stadium_resolver=self.stadiums
        ).collect_date("2026-08-16")
        url, params, _headers = transport.calls[0]
        self.assertEqual(url, "https://statsapi.mlb.com/api/v1/schedule")
        self.assertEqual(params["sportId"], "1")
        self.assertEqual(params["date"], "2026-08-16")
        self.assertIn("probablePitcher", params["hydrate"])
        self.assertEqual(batch.provider_key, "mlb_schedule")

    def test_official_page_check_never_claims_structured_player_confirmation(self):
        html = b"<html><title>MLB Starting Lineups</title><body>lineup page</body></html>"
        transport = FakeTransport([response(html)])
        check = OfficialMlbPageCollector(transport=transport).check("lineups")
        self.assertEqual(check.access_status, "confirmed")
        self.assertEqual(check.source_key, "mlb_lineups")
        self.assertFalse(hasattr(check, "players"))
        self.assertFalse(hasattr(check, "batting_order"))

    def test_storage_persists_schedule_probables_and_unconfirmed_lineup_context(self):
        batch = parse_mlb_schedule(
            self.schedule_payload(),
            team_resolver=self.teams,
            stadium_resolver=self.stadiums,
            collected_at=NOW,
            expected_date="2026-08-16",
        )
        stored = CollectorRepository(self.db).persist_mlb_schedule(batch)
        self.assertEqual(stored, 1)
        game = self.db.execute("SELECT * FROM games WHERE game_id='mlb_777001'").fetchone()
        self.assertEqual(game["sport"], "mlb")
        context = self.db.execute("SELECT * FROM mlb_game_context WHERE game_id='mlb_777001'").fetchone()
        self.assertEqual(context["probable_home_pitcher_id"], "mlb_person_1001")
        self.assertEqual(context["home_lineup_status"], "unconfirmed")
        players = self.db.execute("SELECT COUNT(*) FROM players WHERE player_id LIKE 'mlb_person_%'").fetchone()[0]
        self.assertEqual(players, 2)

    def test_storage_matches_mlb_odds_only_to_mlb_game_and_keeps_null_spread(self):
        schedule = parse_mlb_schedule(
            self.schedule_payload(),
            team_resolver=self.teams,
            stadium_resolver=self.stadiums,
            collected_at=NOW,
            expected_date="2026-08-16",
        )
        repo = CollectorRepository(self.db)
        repo.persist_mlb_schedule(schedule)
        odds = parse_current_mlb_moneylines(self.moneyline_payload(), self.teams, NOW)
        odds = type(odds)(
            provider_key=odds.provider_key,
            collected_at=odds.collected_at,
            raw_payload=odds.raw_payload,
            quotes=odds.quotes,
            warnings=odds.warnings,
            metadata={"endpoint": "current_mlb_odds"},
        )
        stored, warnings = repo.persist_odds(
            odds, accessible_bookmakers=("hardrockbet",), consensus_bookmakers=("hardrockbet",)
        )
        self.assertEqual(stored, 2)
        self.assertEqual(warnings, ())
        rows = self.db.execute("SELECT * FROM odds_snapshots WHERE game_id='mlb_777001'").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["market_key"] == "moneylines" for row in rows))
        self.assertTrue(all(row["spread_points"] is None for row in rows))

    def test_current_mlb_odds_are_marked_stale_after_limit(self):
        schedule = parse_mlb_schedule(
            self.schedule_payload(),
            team_resolver=self.teams,
            stadium_resolver=self.stadiums,
            collected_at=NOW,
            expected_date="2026-08-16",
        )
        repo = CollectorRepository(self.db)
        repo.persist_mlb_schedule(schedule)
        payload = self.moneyline_payload(last_update="2026-08-16T14:00:00Z")
        odds = parse_current_mlb_moneylines(payload, self.teams, NOW)
        odds = type(odds)(
            provider_key=odds.provider_key,
            collected_at=odds.collected_at,
            raw_payload=odds.raw_payload,
            quotes=odds.quotes,
            warnings=odds.warnings,
            metadata={"endpoint": "current_mlb_odds"},
        )
        stored, _ = repo.persist_odds(odds, stale_after_minutes=60)
        self.assertEqual(stored, 2)
        statuses = {r[0] for r in self.db.execute("SELECT market_status FROM odds_snapshots")}
        self.assertEqual(statuses, {"stale"})

    def test_mlb_schedule_source_is_seeded_as_official(self):
        row = self.db.execute("SELECT * FROM sources WHERE source_key='mlb_schedule'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["official"], 1)
        self.assertEqual(row["priority"], 1)


if __name__ == "__main__":
    unittest.main()
