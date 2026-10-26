from datetime import date, datetime, timezone
from pathlib import Path
import json
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.collectors.base import MappingTeamResolver
from sports_analytics_pipeline.collectors.http import HttpResponse
from sports_analytics_pipeline.collectors.therundown import TheRundownCollector, parse_spreads


NOW = datetime(2026, 9, 13, 15, 1, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {}
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, params or {}, headers or {}))
        return HttpResponse(
            200,
            json.dumps(self.payload).encode(),
            self.headers,
            NOW,
        )


def payload(*, status="STATUS_SCHEDULED", second_side=True, main_line=True):
    participants = [
        {
            "id": 1,
            "type": "TYPE_TEAM",
            "name": "Miami Dolphins",
            "lines": [{
                "id": "away-line",
                "value": "+3",
                "prices": {
                    "19": {
                        "id": "price-away",
                        "price": -110,
                        "is_main_line": main_line,
                        "updated_at": "2026-09-13T15:00:00Z",
                        "closed_at": "",
                    }
                },
            }],
        }
    ]
    if second_side:
        participants.append(
            {
                "id": 2,
                "type": "TYPE_TEAM",
                "name": "Buffalo Bills",
                "lines": [{
                    "id": "home-line",
                    "value": "-3",
                    "prices": {
                        "19": {
                            "id": "price-home",
                            "price": -110,
                            "is_main_line": main_line,
                            "updated_at": "2026-09-13T15:00:00Z",
                            "closed_at": "",
                        }
                    },
                }],
            }
        )
    return {
        "events": [{
            "event_id": "rundown-event-1",
            "sport_id": 2,
            "event_date": "2026-09-13T17:00:00Z",
            "score": {"event_status": status},
            "teams": [
                {
                    "team_id": 1,
                    "name": "Miami Dolphins",
                    "is_away": True,
                    "is_home": False,
                },
                {
                    "team_id": 2,
                    "name": "Buffalo Bills",
                    "is_away": False,
                    "is_home": True,
                },
            ],
            "markets": [{
                "id": 10,
                "market_id": 2,
                "period_id": 0,
                "participants": participants,
            }],
        }]
    }


class TheRundownTests(unittest.TestCase):
    def setUp(self):
        self.teams = MappingTeamResolver(
            {"Buffalo Bills": "BUF", "Miami Dolphins": "MIA"}
        )

    def test_client_uses_header_auth_and_approved_filters(self):
        transport = FakeTransport(
            payload(),
            {
                "x-datapoints-remaining": "19994",
                "x-datapoints-used": "6",
                "x-datapoints-limit": "20000",
                "x-datapoints": "6",
            },
        )
        collector = TheRundownCollector(
            api_key="private-key",
            resolver=self.teams,
            transport=transport,
            event_date=date(2026, 9, 13),
            sport_id=2,
        )
        batch = collector.collect_current_spreads()
        self.assertEqual(len(batch.quotes), 2)
        url, params, headers = transport.calls[0]
        self.assertNotIn("private-key", url)
        self.assertNotIn("key", params)
        self.assertEqual(headers, {"X-TheRundown-Key": "private-key"})
        self.assertEqual(params["market_ids"], "2")
        self.assertEqual(params["affiliate_ids"], "19,22,23")
        self.assertEqual(params["main_line"], "true")
        self.assertEqual(params["hide_closed"], "true")
        self.assertEqual(batch.metadata["datapoints_remaining"], 19994)

    def test_parser_normalizes_american_prices_and_preserves_provider(self):
        batch = parse_spreads(
            payload(), self.teams, expected_sport_id=2, collected_at=NOW
        )
        self.assertEqual(batch.provider_key, "therundown")
        self.assertEqual({quote.bookmaker_key for quote in batch.quotes}, {"draftkings"})
        self.assertEqual({quote.outcome_team_id for quote in batch.quotes}, {"BUF", "MIA"})
        self.assertTrue(all(str(quote.decimal_odds).startswith("1.909") for quote in batch.quotes))

    def test_non_pregame_event_is_skipped(self):
        batch = parse_spreads(
            payload(status="STATUS_IN_PROGRESS"),
            self.teams,
            expected_sport_id=2,
            collected_at=NOW,
        )
        self.assertEqual(batch.quotes, ())
        self.assertIn("non-pregame", batch.warnings[0])

    def test_incomplete_two_sided_market_is_skipped(self):
        batch = parse_spreads(
            payload(second_side=False),
            self.teams,
            expected_sport_id=2,
            collected_at=NOW,
        )
        self.assertEqual(batch.quotes, ())
        self.assertIn("incomplete", batch.warnings[0])

    def test_alternate_line_is_not_accepted(self):
        batch = parse_spreads(
            payload(main_line=False),
            self.teams,
            expected_sport_id=2,
            collected_at=NOW,
        )
        self.assertEqual(batch.quotes, ())
        self.assertTrue(any("alternate" in warning for warning in batch.warnings))

    def test_unsupported_sport_id_is_rejected_before_request(self):
        transport = FakeTransport(payload())
        with self.assertRaises(ValueError):
            TheRundownCollector(
                api_key="private-key",
                resolver=self.teams,
                transport=transport,
                event_date=date(2026, 9, 13),
                sport_id=1,
            )
        self.assertEqual(transport.calls, [])

    def test_malformed_price_is_skipped_without_becoming_a_quote(self):
        broken = payload()
        broken["events"][0]["markets"][0]["participants"][0]["lines"][0]["prices"]["19"]["price"] = None
        batch = parse_spreads(
            broken, self.teams, expected_sport_id=2, collected_at=NOW
        )
        self.assertEqual(batch.quotes, ())
        self.assertTrue(any("malformed" in warning for warning in batch.warnings))


if __name__ == "__main__":
    unittest.main()
