import unittest
from datetime import datetime, timezone

from sports_analytics_pipeline.collectors.http import HttpResponse
from sports_analytics_pipeline.collectors.multisport_structured_results import (
    StructuredResultsCollector,
    parse_finalized_event,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 2, 23, 0, tzinfo=UTC)


def team(name, score=None):
    row = {"names": {"long": name}}
    if score is not None:
        row["score"] = score
    return row


class FakeTransport:
    def __init__(self):
        self.params = None
        self.calls = 0

    def get(self, _url, *, params=None, headers=None):
        self.params = params
        self.calls += 1
        return HttpResponse(
            status=200,
            body=b'{"data":[]}',
            headers={},
            received_at=NOW,
        )



class StructuredResultCollectorTests(unittest.TestCase):
    def test_live_mma_query_is_current_sport_scoped_and_cached(self):
        transport = FakeTransport()
        collector = StructuredResultsCollector(api_key="test-key", transport=transport)
        for home_name in ("Player One", "Another Player"):
            self.assertIsNone(
                collector.collect_event(
                    sport="mma",
                    local_home_name=home_name,
                    local_away_name="Fighter Two",
                    commence_time=NOW,
                )
            )
        self.assertEqual(transport.params["sportID"], "MMA")
        self.assertEqual(transport.params["started"], "true")
        self.assertEqual(transport.params["limit"], "30")
        self.assertNotIn("startsAfter", transport.params)
        self.assertNotIn("finalized", transport.params)
        self.assertEqual(transport.calls, 1)

    def test_tennis_result_matches_reversed_provider_orientation(self):
        event = {
            "eventID": "sgo-tennis-1",
            "teams": {
                "home": team("Botic van de Zandschulp", "0"),
                "away": team("Jan Choinski", "2"),
            },
            "status": {
                "finalized": True,
                "ended": True,
                "displayLong": "Final",
                "periods": {"started": ["1s", "2s"], "ended": ["1s", "2s"]},
            },
        }
        result = parse_finalized_event(
            event,
            sport="atp",
            local_home_name="Jan Choinski",
            local_away_name="Botic van de Zandschulp",
            collected_at=NOW,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.result_status, "final")
        self.assertEqual(result.home_score, 2)
        self.assertEqual(result.away_score, 0)
        self.assertEqual(result.winner_role, "home")
        self.assertTrue(result.raw_result["first_set_completed"])
        self.assertTrue(result.raw_result["at_least_one_point_completed"])

    def test_retirement_keeps_exact_book_settlement_evidence(self):
        event = {
            "eventID": "sgo-tennis-2",
            "teams": {
                "home": team("Player One", 1),
                "away": team("Player Two", 0),
            },
            "status": {
                "finalized": True,
                "displayLong": "Player Two retired",
                "periods": {"started": ["1s", "2s"], "ended": ["1s"]},
            },
        }
        result = parse_finalized_event(
            event,
            sport="wta",
            local_home_name="Player One",
            local_away_name="Player Two",
            collected_at=NOW,
        )
        self.assertEqual(result.result_status, "retirement")
        self.assertEqual(result.winner_role, "home")
        self.assertTrue(result.raw_result["first_set_completed"])

    def test_mma_no_contest_is_void_terminal_result(self):
        event = {
            "eventID": "sgo-mma-1",
            "teams": {
                "home": team("Fighter One"),
                "away": team("Fighter Two"),
            },
            "status": {
                "finalized": True,
                "displayLong": "No Contest",
                "periods": {"started": ["1"], "ended": ["1"]},
            },
        }
        result = parse_finalized_event(
            event,
            sport="mma",
            local_home_name="Fighter One",
            local_away_name="Fighter Two",
            collected_at=NOW,
        )
        self.assertEqual(result.result_status, "no_contest")
        self.assertEqual(result.winner_role, "void")

    def test_unfinalized_or_wrong_participants_are_rejected(self):
        base = {
            "eventID": "sgo-tennis-3",
            "teams": {"home": team("A", 2), "away": team("B", 0)},
            "status": {"finalized": False, "ended": True, "displayLong": "Ended"},
        }
        self.assertIsNone(
            parse_finalized_event(
                base,
                sport="atp",
                local_home_name="A",
                local_away_name="B",
                collected_at=NOW,
            )
        )
        base["status"]["finalized"] = True
        self.assertIsNone(
            parse_finalized_event(
                base,
                sport="atp",
                local_home_name="A",
                local_away_name="Different Player",
                collected_at=NOW,
            )
        )


if __name__ == "__main__":
    unittest.main()
