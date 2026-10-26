from datetime import datetime, timezone
import gzip
import io
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.collectors.base import MappingTeamResolver
from sports_analytics_pipeline.collectors.http import HttpResponse, safe_url
from sports_analytics_pipeline.collectors.nflverse import (
    MappingStadiumResolver,
    NflverseCollector,
    parse_injuries_csv,
    parse_rosters_csv,
    parse_schedules_csv,
)
from sports_analytics_pipeline.collectors.nws import NwsCollector, parse_weather_payloads
from sports_analytics_pipeline.collectors.official_nfl import OfficialNflCollector
from sports_analytics_pipeline.collectors.the_odds_api import (
    PRESEASON_SPORT_KEY,
    TheOddsApiCollector,
)


NOW = datetime(2026, 8, 12, 20, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, params or {}, headers or {}))
        if not self.responses:
            raise AssertionError("Unexpected HTTP call")
        body, response_headers, received_at = self.responses.pop(0)
        if isinstance(body, str):
            body = body.encode()
        return HttpResponse(200, body, response_headers, received_at)


class Phase2CollectorTests(unittest.TestCase):
    def setUp(self):
        self.teams = MappingTeamResolver(
            {
                "Buffalo Bills": "BUF",
                "Miami Dolphins": "MIA",
                "BUF": "BUF",
                "MIA": "MIA",
                "LAR": "LAR",
                "SF": "SF",
            }
        )
        self.stadiums = MappingStadiumResolver(
            {
                "Highmark Stadium": "highmark_stadium_2026",
                "SoFi Stadium": "sofi_stadium",
            }
        )

    def test_safe_url_redacts_api_key(self):
        result = safe_url("https://example.test/data?apiKey=secret&markets=spreads")
        self.assertNotIn("secret", result)
        self.assertIn("markets=spreads", result)

    def test_current_odds_client_builds_approved_request_and_reads_quota(self):
        payload = """[
          {"id":"event-1","sport_key":"americanfootball_nfl",
           "commence_time":"2026-09-13T17:00:00Z",
           "home_team":"Buffalo Bills","away_team":"Miami Dolphins",
           "bookmakers":[{"key":"book-a","title":"Book A",
             "last_update":"2026-09-13T15:00:00Z",
             "markets":[{"key":"spreads","last_update":"2026-09-13T15:00:00Z",
               "outcomes":[{"name":"Buffalo Bills","price":1.91,"point":-3},
                           {"name":"Miami Dolphins","price":1.91,"point":3}]}]}]}
        ]"""
        received = datetime(2026, 9, 13, 15, 1, tzinfo=timezone.utc)
        transport = FakeTransport([(payload, {"x-requests-remaining": "499"}, received)])
        collector = TheOddsApiCollector(
            api_key="secret",
            resolver=self.teams,
            transport=transport,
            bookmakers=("book-a",),
            regions=(),
        )
        batch = collector.collect_current_spreads()
        self.assertEqual(len(batch.quotes), 2)
        self.assertEqual(batch.metadata["requests_remaining"], 499)
        _, params, _ = transport.calls[0]
        self.assertEqual(params["markets"], "spreads")
        self.assertEqual(params["oddsFormat"], "decimal")
        self.assertEqual(params["bookmakers"], "book-a")

    def test_historical_snapshot_cannot_follow_requested_time(self):
        wrapper = """{
          "timestamp":"2025-09-13T15:05:00Z","previous_timestamp":null,"next_timestamp":null,
          "data":[]
        }"""
        transport = FakeTransport([(wrapper, {}, NOW)])
        collector = TheOddsApiCollector(
            api_key="secret", resolver=self.teams, transport=transport
        )
        with self.assertRaises(ValueError):
            collector.collect_historical_spreads(
                datetime(2025, 9, 13, 15, 0, tzinfo=timezone.utc)
            )

    def test_preseason_odds_client_uses_separate_preseason_feed(self):
        payload = """[
          {"id":"preseason-event-1","sport_key":"americanfootball_nfl_preseason",
           "commence_time":"2026-08-14T23:00:00Z",
           "home_team":"Buffalo Bills","away_team":"Miami Dolphins",
           "bookmakers":[{"key":"book-a","title":"Book A",
             "last_update":"2026-08-14T22:50:00Z",
             "markets":[{"key":"spreads","last_update":"2026-08-14T22:50:00Z",
               "outcomes":[{"name":"Buffalo Bills","price":1.91,"point":-3},
                           {"name":"Miami Dolphins","price":1.91,"point":3}]}]}]}
        ]"""
        received = datetime(2026, 8, 14, 22, 51, tzinfo=timezone.utc)
        transport = FakeTransport([(payload, {"x-requests-remaining": "498"}, received)])
        collector = TheOddsApiCollector(
            api_key="secret",
            resolver=self.teams,
            transport=transport,
            bookmakers=("book-a",),
            regions=(),
            sport_key=PRESEASON_SPORT_KEY,
        )
        batch = collector.collect_current_spreads()
        self.assertEqual(len(batch.quotes), 2)
        self.assertEqual(batch.metadata["endpoint"], "current_preseason_odds")
        url, _, _ = transport.calls[0]
        self.assertIn("americanfootball_nfl_preseason", url)

    def test_nflverse_schedule_roster_and_injury_parsers(self):
        schedule_csv = (
            "game_id,season,game_type,week,gameday,gametime,away_team,away_score,"
            "home_team,home_score,location,stadium,roof,surface\n"
            "2026_01_MIA_BUF,2026,REG,1,2026-09-13,13:00,MIA,,BUF,,Home,"
            "Highmark Stadium,outdoors,grass\n"
        )
        schedules = parse_schedules_csv(
            schedule_csv,
            season=2026,
            team_resolver=self.teams,
            stadium_resolver=self.stadiums,
            collected_at=NOW,
        )
        self.assertEqual(len(schedules.records), 1)
        self.assertEqual(schedules.records[0].kickoff_time.hour, 17)
        self.assertEqual(schedules.records[0].season_type, "regular")

        roster_csv = (
            "season,team,position,depth_chart_position,status,full_name,gsis_id\n"
            "2026,BUF,QB,QB,ACT,Example Quarterback,00-0000001\n"
        )
        rosters = parse_rosters_csv(
            roster_csv, season=2026, team_resolver=self.teams, collected_at=NOW
        )
        self.assertEqual(rosters.records[0].team_id, "BUF")

        injury_csv = (
            "season,game_type,team,week,gsis_id,position,full_name,"
            "report_primary_injury,report_status,practice_status,date_modified\n"
            "2026,REG,BUF,1,00-0000001,QB,Example Quarterback,Ankle,Questionable,Limited,"
            "2026-09-11T20:00:00Z\n"
        )
        injuries = parse_injuries_csv(
            injury_csv, season=2026, team_resolver=self.teams, collected_at=NOW
        )
        self.assertEqual(injuries.records[0].game_status, "Questionable")

    def test_unknown_schedule_venue_is_warning_not_silent_match(self):
        csv_text = (
            "game_id,season,game_type,week,gameday,gametime,away_team,home_team,location,stadium\n"
            "game-x,2026,REG,1,2026-09-13,13:00,MIA,BUF,Home,Unknown Field\n"
        )
        batch = parse_schedules_csv(
            csv_text,
            season=2026,
            team_resolver=self.teams,
            stadium_resolver=self.stadiums,
            collected_at=NOW,
        )
        self.assertEqual(batch.records, ())
        self.assertIn("Unknown stadium alias", batch.warnings[0])

    def test_nflverse_la_code_resolves_to_rams_without_global_la_alias(self):
        csv_text = (
            "game_id,season,game_type,week,gameday,gametime,away_team,home_team,location,stadium\n"
            "2026_01_SF_LA,2026,REG,1,2026-09-10,20:35,SF,LA,Neutral,SoFi Stadium\n"
        )
        batch = parse_schedules_csv(
            csv_text,
            season=2026,
            team_resolver=self.teams,
            stadium_resolver=self.stadiums,
            collected_at=NOW,
        )
        self.assertEqual(batch.warnings, ())
        self.assertEqual(batch.records[0].home_team_id, "LAR")

    def test_play_by_play_archive_header_is_validated(self):
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb") as archive:
            archive.write(b"game_id,play_id,home_team,away_team\ngame-1,1,BUF,MIA\n")
        transport = FakeTransport([(buffer.getvalue(), {}, NOW)])
        collector = NflverseCollector(
            transport=transport, team_resolver=self.teams, stadium_resolver=self.stadiums
        )
        batch = collector.collect_play_by_play_archive(2026)
        self.assertEqual(batch.dataset_name, "play_by_play")

    def test_nws_weather_selects_kickoff_hour_and_alerts(self):
        kickoff = datetime(2026, 9, 13, 17, 30, tzinfo=timezone.utc)
        points = {"properties": {"forecastHourly": "https://api.weather.gov/gridpoints/BUF/1,1/forecast/hourly"}}
        hourly = {
            "properties": {
                "updated": "2026-09-13T15:00:00Z",
                "periods": [{
                    "startTime": "2026-09-13T17:00:00Z",
                    "endTime": "2026-09-13T18:00:00Z",
                    "temperature": 64,
                    "temperatureUnit": "F",
                    "windSpeed": "12 mph",
                    "shortForecast": "Rain Showers",
                    "detailedForecast": "Wind gusts as high as 24 mph.",
                    "probabilityOfPrecipitation": {"value": 40},
                }],
            }
        }
        alerts = {"features": [{"id": "alert-1", "properties": {"event": "Wind Advisory", "severity": "Moderate"}}]}
        batch = parse_weather_payloads(
            latitude=42.7,
            longitude=-78.8,
            kickoff_time=kickoff,
            points_payload=points,
            hourly_payload=hourly,
            alerts_payload=alerts,
            collected_at=datetime(2026, 9, 13, 15, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(batch.record.temperature_f, 64)
        self.assertEqual(batch.record.wind_gust_mph, 24)
        self.assertEqual(batch.record.precipitation_probability, 0.4)
        self.assertEqual(batch.record.alerts[0]["event"], "Wind Advisory")

    def test_nws_rejects_placeholder_user_agent(self):
        with self.assertRaises(ValueError):
            NwsCollector(
                transport=FakeTransport([]),
                user_agent="SportsAnalyticsPipeline (contact: replace-with-real-contact)",
            )

    def test_official_page_check_does_not_guess_player_records(self):
        transport = FakeTransport([("<html><title>NFL Injury Report</title></html>", {}, NOW)])
        check = OfficialNflCollector(transport=transport).check("injuries")
        self.assertEqual(check.access_status, "confirmed")
        self.assertEqual(len(check.content_sha256), 64)


if __name__ == "__main__":
    unittest.main()
