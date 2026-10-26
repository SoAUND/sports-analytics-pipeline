import unittest
from datetime import datetime, timezone

from sports_analytics_pipeline.collectors.http import HttpResponse
from sports_analytics_pipeline.collectors.multisport_mma_results import (
    ApiSportsMmaResultsCollector,
    parse_api_sports_fight,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 2, 23, 0, tzinfo=UTC)


def fight(status, winner=None):
    results = {"won_by": "Decision"}
    if winner is not None:
        results["winner"] = {"id": 2, "name": winner}
    return {
        "id": 77,
        "status": status,
        "fighters": {
            "first": {"id": 1, "name": "Silvestre Sanchez"},
            "second": {"id": 2, "name": "Liam McCracken"},
        },
        "results": results,
    }


class FakeTransport:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.params = None

    def get(self, _url, *, headers=None, params=None):
        self.calls += 1
        self.params = params
        return HttpResponse(
            status=200,
            body=__import__("json").dumps(self.payload).encode(),
            headers={},
            received_at=NOW,
        )


class ApiSportsMmaResultTests(unittest.TestCase):
    def test_finished_fight_exactly_maps_winner(self):
        result = parse_api_sports_fight(
            fight({"long": "Finished", "short": "FT"}, "Liam McCracken"),
            local_home_name="Silvestre Sanchez",
            local_away_name="Liam McCracken",
            collected_at=NOW,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.result_status, "final")
        self.assertEqual(result.winner_role, "away")

    def test_no_contest_is_terminal_void(self):
        row = fight({"long": "No Contest", "short": "NC"})
        row["results"]["won_by"] = "No Contest"
        result = parse_api_sports_fight(
            row,
            local_home_name="Silvestre Sanchez",
            local_away_name="Liam McCracken",
            collected_at=NOW,
        )
        self.assertEqual(result.result_status, "no_contest")
        self.assertEqual(result.winner_role, "void")

    def test_unfinished_or_wrong_fighters_fail_closed(self):
        self.assertIsNone(
            parse_api_sports_fight(
                fight({"long": "Scheduled", "short": "NS"}, "Liam McCracken"),
                local_home_name="Silvestre Sanchez",
                local_away_name="Liam McCracken",
                collected_at=NOW,
            )
        )
        self.assertIsNone(
            parse_api_sports_fight(
                fight({"long": "Finished", "short": "FT"}, "Liam McCracken"),
                local_home_name="Different Fighter",
                local_away_name="Liam McCracken",
                collected_at=NOW,
            )
        )

    def test_date_query_is_cached(self):
        transport = FakeTransport({"errors": [], "response": []})
        collector = ApiSportsMmaResultsCollector(api_key="key", transport=transport)
        for opponent in ("Fighter Two", "Fighter Three"):
            self.assertIsNone(
                collector.collect_event(
                    local_home_name="Fighter One",
                    local_away_name=opponent,
                    commence_time=NOW,
                )
            )
        self.assertEqual(transport.params, {"date": "2026-09-02"})
        self.assertEqual(transport.calls, 1)


if __name__ == "__main__":
    unittest.main()
