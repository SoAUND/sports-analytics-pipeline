import json
import unittest
from datetime import datetime, timedelta, timezone

from sports_analytics_pipeline.collectors.base import MappingTeamResolver
from sports_analytics_pipeline.collectors.http import HttpResponse
from sports_analytics_pipeline.collectors.mlb_official import (
    MlbRecentResultsCollector,
    parse_mlb_recent_results,
)
from sports_analytics_pipeline.collectors.storage import CollectorRepository
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import alias_mapping, seed_reference_data
from sports_analytics_pipeline.mlb_live_paper import _context_state
from sports_analytics_pipeline.mlb_research import build_and_store_mlb_research_context

UTC = timezone.utc
NOW = datetime(2026, 8, 16, 20, 0, tzinfo=UTC)


class FakeTransport:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        return HttpResponse(
            status=200,
            body=json.dumps(self.payload).encode(),
            headers={},
            received_at=NOW,
        )


class MLBStructuredResearchStep4CTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)
        seed_reference_data(self.db, include_mlb=True)
        self.teams = MappingTeamResolver(alias_mapping(self.db, "mlb"))

    def tearDown(self):
        self.db.close()

    def recent_payload(self):
        games = []
        for index in range(12):
            day = 2 + index
            games.append({
                "gamePk": 810000 + index,
                "gameDate": f"2026-08-{day:02d}T23:05:00Z",
                "gameType": "R",
                "season": "2026",
                "status": {"abstractGameState": "Final", "detailedState": "Final"},
                "teams": {
                    "home": {"team": {"name": "Arizona Diamondbacks"}, "score": 5 if index % 2 == 0 else 2},
                    "away": {"team": {"name": "Houston Astros"}, "score": 2 if index % 2 == 0 else 4},
                },
            })
        games.append({
            "gamePk": 899999,
            "gameDate": "2026-08-13T23:05:00Z",
            "gameType": "S",
            "season": "2026",
            "status": {"abstractGameState": "Final", "detailedState": "Final"},
            "teams": {
                "home": {"team": {"name": "Arizona Diamondbacks"}, "score": 5},
                "away": {"team": {"name": "Houston Astros"}, "score": 4},
            },
        })
        games.append({
            "gamePk": 899998,
            "gameDate": "2026-08-14T23:05:00Z",
            "gameType": "R",
            "season": "2026",
            "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
            "teams": {
                "home": {"team": {"name": "Arizona Diamondbacks"}},
                "away": {"team": {"name": "Houston Astros"}},
            },
        })
        return {"dates": [{"date": "2026-08-13", "games": games}]}

    def test_recent_results_parser_keeps_only_final_regular_games(self):
        batch = parse_mlb_recent_results(
            self.recent_payload(),
            team_resolver=self.teams,
            collected_at=NOW,
            start_date="2026-07-26",
            end_date="2026-08-15",
        )
        self.assertEqual(len(batch.games), 12)
        self.assertTrue(all(game.home_score != game.away_score for game in batch.games))
        self.assertEqual(batch.games[0].home_team_id, "mlb_ari")

    def test_recent_results_collector_uses_one_official_date_range_request(self):
        transport = FakeTransport(self.recent_payload())
        batch = MlbRecentResultsCollector(
            transport=transport, team_resolver=self.teams
        ).collect_before("2026-08-16", lookback_days=21)
        url, params, headers = transport.calls[0]
        self.assertEqual(url, "https://statsapi.mlb.com/api/v1/schedule")
        self.assertEqual(params["sportId"], "1")
        self.assertEqual(params["gameType"], "R")
        self.assertEqual(params["startDate"], "07/26/2026")
        self.assertEqual(params["endDate"], "08/15/2026")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(batch.provider_key, "mlb_schedule")

    def test_recent_results_storage_is_idempotent(self):
        batch = parse_mlb_recent_results(
            self.recent_payload(),
            team_resolver=self.teams,
            collected_at=NOW,
            start_date="2026-07-26",
            end_date="2026-08-15",
        )
        repo = CollectorRepository(self.db)
        self.assertEqual(repo.persist_mlb_recent_results(batch), 12)
        self.assertEqual(repo.persist_mlb_recent_results(batch), 0)
        count = self.db.execute("SELECT COUNT(*) FROM historical_mlb_games").fetchone()[0]
        self.assertEqual(count, 12)

    def _insert_player(self, player_id, name, team_id):
        self.db.execute(
            """
            INSERT INTO players(player_id,sport,player_name,primary_team_id,position,roster_status)
            VALUES (?,'mlb',?,?, 'P','active')
            """,
            (player_id, name, team_id),
        )

    def _seed_upcoming(self, *, stadium="chase_field", context_time=None, with_history=True):
        context_time = context_time or (NOW - timedelta(minutes=10))
        kickoff = NOW + timedelta(hours=2)
        self.db.execute(
            """
            INSERT INTO games(game_id,source_game_id,sport,season,season_type,week,home_team_id,away_team_id,kickoff_time,stadium_id,status,created_at,updated_at)
            VALUES ('mlb_upcoming','999001','mlb',2026,'regular',0,'mlb_ari','mlb_hou',?,?, 'scheduled',?,?)
            """,
            (kickoff.isoformat(), stadium, (NOW-timedelta(hours=1)).isoformat(), (NOW-timedelta(hours=1)).isoformat()),
        )
        self._insert_player("mlb_person_1", "Home Starter", "mlb_ari")
        self._insert_player("mlb_person_2", "Away Starter", "mlb_hou")
        self.db.execute(
            """
            INSERT INTO mlb_game_context(
                game_id,confirmed_home_pitcher_id,confirmed_away_pitcher_id,
                home_lineup_status,away_lineup_status,home_batting_order_json,
                away_batting_order_json,collected_at,source_notes_json
            ) VALUES ('mlb_upcoming','mlb_person_1','mlb_person_2','confirmed','confirmed',?,?,?, '[]')
            """,
            (json.dumps([f"h{i}" for i in range(9)]), json.dumps([f"a{i}" for i in range(9)]), context_time.isoformat()),
        )
        for check_type, source_key in (
            ("injuries", "mlb_injuries"),
            ("lineups", "mlb_lineups"),
            ("transactions", "mlb_transactions"),
            ("probable_pitchers", "mlb_probable_pitchers"),
        ):
            self.db.execute(
                """
                INSERT INTO source_checks(game_id,source_key,check_type,access_status,checked_at,source_url)
                VALUES ('mlb_upcoming',?,?, 'confirmed',?,?)
                """,
                (check_type and source_key, check_type, (NOW-timedelta(minutes=5)).isoformat(), f"https://www.mlb.com/{check_type}"),
            )
        self.db.execute(
            """
            INSERT INTO collection_runs(collection_run_id,source_key,endpoint_name,started_at,finished_at,status)
            VALUES ('tx-run','mlb_transactions','transactions_2026-08-16',?,?, 'succeeded')
            """,
            ((NOW-timedelta(minutes=8)).isoformat(), (NOW-timedelta(minutes=7)).isoformat()),
        )
        self.db.execute(
            "INSERT INTO sportsbooks(sportsbook_key,display_name,accessible,consensus_eligible) VALUES ('hardrockbet_fl','Primary Sportsbook',1,1)"
        )
        for team, opponent, price in (("mlb_ari","mlb_hou",1.91),("mlb_hou","mlb_ari",1.95)):
            self.db.execute(
                """
                INSERT INTO odds_snapshots(
                    game_id,sportsbook_key,market_key,period,settlement_rule,outcome_team_id,
                    opponent_team_id,decimal_odds,american_odds,source_updated_at,collected_at,
                    market_status,is_accessible,is_consensus_eligible
                ) VALUES ('mlb_upcoming','hardrockbet_fl','moneylines','full_game','includes_extra_innings',?,?,?, -110,?,?, 'open',1,1)
                """,
                (team, opponent, price, (NOW-timedelta(minutes=4)).isoformat(), (NOW-timedelta(minutes=4)).isoformat()),
            )
        if with_history:
            self.db.execute(
                """
                INSERT INTO raw_ingests(
                    source_key,endpoint_name,collected_at,content_sha256,payload_json,http_status
                ) VALUES ('mlb_schedule','recent_results_2026-07-26_2026-08-15',?,'recent-hash','{}',200)
                """,
                ((NOW-timedelta(minutes=6)).isoformat(),),
            )
            for index in range(10):
                played = NOW - timedelta(days=index+1)
                ari_home = index % 2 == 0
                self.db.execute(
                    """
                    INSERT INTO historical_mlb_games(
                        source_game_id,season,kickoff_time,home_team_id,away_team_id,home_score,away_score,source_key,collected_at
                    ) VALUES (?,?,?,?,?,?,?,'mlb_schedule',?)
                    """,
                    (
                        f"ari-{index}", 2026, played.isoformat(),
                        "mlb_ari" if ari_home else "mlb_lad",
                        "mlb_lad" if ari_home else "mlb_ari",
                        5 if index % 3 else 2, 2 if index % 3 else 5,
                        (NOW-timedelta(hours=2)).isoformat(),
                    ),
                )
                self.db.execute(
                    """
                    INSERT INTO historical_mlb_games(
                        source_game_id,season,kickoff_time,home_team_id,away_team_id,home_score,away_score,source_key,collected_at
                    ) VALUES (?,?,?,?,?,?,?,'mlb_schedule',?)
                    """,
                    (
                        f"hou-{index}", 2026, (played-timedelta(hours=1)).isoformat(),
                        "mlb_hou" if not ari_home else "mlb_tex",
                        "mlb_tex" if not ari_home else "mlb_hou",
                        4 if index % 2 else 1, 1 if index % 2 else 4,
                        (NOW-timedelta(hours=2)).isoformat(),
                    ),
                )
        self.db.commit()

    def test_complete_indoor_evidence_creates_reviewed_context(self):
        self._seed_upcoming()
        report = build_and_store_mlb_research_context(
            self.db, game_id="mlb_upcoming", as_of=NOW, stale_after_minutes=60
        )
        self.assertTrue(report["research_complete"])
        self.assertTrue(report["material_information_complete"])
        self.assertTrue(report["market_match_confirmed"])
        self.assertEqual(report["blockers"], [])
        context = self.db.execute(
            "SELECT * FROM evaluation_contexts WHERE game_id='mlb_upcoming'"
        ).fetchone()
        self.assertEqual(context["information_quality"], "full_official")
        mlb = self.db.execute("SELECT * FROM mlb_game_context WHERE game_id='mlb_upcoming'").fetchone()
        self.assertIsNotNone(mlb["home_recent_5"])
        self.assertIsNotNone(mlb["away_recent_10"])

    def test_missing_recent_form_fails_closed(self):
        self._seed_upcoming(with_history=False)
        report = build_and_store_mlb_research_context(
            self.db, game_id="mlb_upcoming", as_of=NOW
        )
        self.assertFalse(report["research_complete"])
        self.assertIn("official_recent_5_and_10_form_incomplete", report["blockers"])

    def test_transaction_after_lineup_collection_creates_conflict(self):
        self._seed_upcoming()
        self.db.execute(
            """
            INSERT INTO transaction_snapshots(
                player_id,player_name,from_team_id,to_team_id,transaction_type,transaction_date,
                description,official,source_key,source_updated_at,collected_at
            ) VALUES ('mlb_person_99','Late Player','mlb_ari','mlb_ari','Status Change','2026-08-16',
                      'Late official roster move',1,'mlb_transactions',?,?)
            """,
            ((NOW-timedelta(minutes=2)).isoformat(), (NOW-timedelta(minutes=2)).isoformat()),
        )
        self.db.commit()
        report = build_and_store_mlb_research_context(
            self.db, game_id="mlb_upcoming", as_of=NOW
        )
        self.assertFalse(report["research_complete"])
        self.assertEqual(report["conflict_status"], "conflict")
        self.assertIn("official_transaction_after_lineup_collection", report["blockers"])

    def test_stale_official_page_checks_fail_closed(self):
        self._seed_upcoming()
        self.db.execute(
            "UPDATE source_checks SET checked_at=? WHERE game_id='mlb_upcoming'",
            ((NOW-timedelta(hours=3)).isoformat(),),
        )
        self.db.commit()
        report = build_and_store_mlb_research_context(
            self.db, game_id="mlb_upcoming", as_of=NOW
        )
        self.assertFalse(report["research_complete"])
        self.assertTrue(any(item.startswith("missing_required_official_page_checks:") for item in report["blockers"]))

    def test_outdoor_weather_is_a_material_gate(self):
        self._seed_upcoming(stadium="yankee_stadium")
        report = build_and_store_mlb_research_context(
            self.db, game_id="mlb_upcoming", as_of=NOW
        )
        self.assertFalse(report["research_complete"])
        self.assertIn("outdoor_weather_missing", report["blockers"])

    def test_future_lineup_context_cannot_satisfy_cutoff(self):
        self._seed_upcoming(context_time=NOW + timedelta(minutes=5))
        state = _context_state(self.db, "mlb_upcoming", NOW)
        self.assertFalse(state["context_present"])
        report = build_and_store_mlb_research_context(
            self.db, game_id="mlb_upcoming", as_of=NOW
        )
        self.assertFalse(report["research_complete"])
        self.assertIn("structured_mlb_game_context_missing_by_cutoff", report["blockers"])


if __name__ == "__main__":
    unittest.main()
