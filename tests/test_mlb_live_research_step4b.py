import json
import unittest
from datetime import datetime, timezone

from sports_analytics_pipeline.collectors.base import MappingTeamResolver
from sports_analytics_pipeline.collectors.http import HttpResponse
from sports_analytics_pipeline.collectors.mlb_official import (
    MlbTransactionsCollector,
    parse_mlb_transactions,
)
from sports_analytics_pipeline.collectors.storage import CollectorRepository
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import alias_mapping, seed_reference_data


UTC = timezone.utc
NOW = datetime(2026, 8, 16, 20, 0, tzinfo=UTC)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if not self.responses:
            raise AssertionError("Unexpected HTTP request")
        return self.responses.pop(0)


def response(payload, *, received_at=NOW):
    return HttpResponse(
        status=200,
        body=json.dumps(payload).encode(),
        headers={},
        received_at=received_at,
    )


class MLBStructuredResearchStep4BTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)
        seed_reference_data(self.db, include_mlb=True)
        self.teams = MappingTeamResolver(alias_mapping(self.db, "mlb"))

    def tearDown(self):
        self.db.close()

    def payload(self):
        return {
            "transactions": [
                {
                    "id": 9001,
                    "person": {
                        "id": 3001,
                        "fullName": "Injured Player",
                        "primaryPosition": {"abbreviation": "OF"},
                    },
                    "toTeam": {"name": "New York Yankees"},
                    "effectiveDate": "2026-08-15",
                    "typeCode": "SC",
                    "typeDesc": "Status Change",
                    "description": (
                        "New York Yankees placed Injured Player "
                        "on the 10-day injured list."
                    ),
                },
                {
                    "id": 9002,
                    "person": {
                        "id": 3001,
                        "fullName": "Injured Player",
                        "primaryPosition": {"abbreviation": "OF"},
                    },
                    "toTeam": {"name": "New York Yankees"},
                    "effectiveDate": "2026-08-16T14:00:00Z",
                    "typeCode": "SC",
                    "typeDesc": "Status Change",
                    "description": (
                        "New York Yankees reinstated Injured Player "
                        "from the 10-day injured list."
                    ),
                },
                {
                    "id": 9003,
                    "person": {
                        "id": 3002,
                        "fullName": "Optioned Player",
                        "primaryPosition": {"abbreviation": "P"},
                    },
                    "fromTeam": {"name": "Boston Red Sox"},
                    "effectiveDate": "2026-08-16",
                    "typeCode": "SC",
                    "typeDesc": "Status Change",
                    "description": "Boston Red Sox optioned Optioned Player to Triple-A.",
                },
            ]
        }

    def parse(self, payload=None):
        return parse_mlb_transactions(
            payload or self.payload(),
            team_resolver=self.teams,
            collected_at=NOW,
            start_date="2026-08-09",
            end_date="2026-08-16",
        )

    def test_parser_classifies_injured_reinstated_and_optioned_statuses(self):
        batch = self.parse()
        self.assertEqual(len(batch.records), 3)
        placed, reinstated, optioned = batch.records
        self.assertEqual(placed.player_id, "mlb_person_3001")
        self.assertEqual(placed.to_team_id, "mlb_nyy")
        self.assertEqual(placed.roster_status, "injured_list")
        self.assertTrue(placed.injured_list)
        self.assertEqual(reinstated.roster_status, "active")
        self.assertFalse(reinstated.injured_list)
        self.assertEqual(optioned.from_team_id, "mlb_bos")
        self.assertEqual(optioned.roster_status, "optioned")

    def test_date_only_transaction_uses_first_collection_time_not_fake_midnight(self):
        batch = self.parse()
        placed = batch.records[0]
        reinstated = batch.records[1]
        self.assertTrue(placed.source_time_is_collection_fallback)
        self.assertEqual(placed.source_updated_at, NOW)
        self.assertFalse(reinstated.source_time_is_collection_fallback)
        self.assertEqual(reinstated.source_updated_at, datetime(2026, 8, 16, 14, 0, tzinfo=UTC))
        self.assertTrue(any("date-level timing" in warning for warning in batch.warnings))

    def test_parser_fails_closed_on_unknown_team_name(self):
        payload = self.payload()
        payload["transactions"][0]["toTeam"]["name"] = "Imaginary MLB Club"
        with self.assertRaises(ValueError):
            self.parse(payload)

    def test_non_player_transaction_is_skipped_with_warning(self):
        payload = {"transactions": [{"effectiveDate": "2026-08-16", "description": "Admin move"}]}
        batch = self.parse(payload)
        self.assertEqual(batch.records, ())
        self.assertTrue(any("player identity was missing" in warning for warning in batch.warnings))

    def test_collector_uses_official_transactions_endpoint_and_seven_day_window(self):
        transport = FakeTransport([response(self.payload())])
        batch = MlbTransactionsCollector(
            transport=transport,
            team_resolver=self.teams,
        ).collect_window("2026-08-16", lookback_days=7)
        url, params, headers = transport.calls[0]
        self.assertEqual(url, "https://statsapi.mlb.com/api/v1/transactions")
        self.assertEqual(params["sportId"], "1")
        self.assertEqual(params["startDate"], "08/09/2026")
        self.assertEqual(params["endDate"], "08/16/2026")
        self.assertEqual(params["hydrate"], "person")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(batch.provider_key, "mlb_transactions")

    def test_storage_persists_official_transactions_roster_and_il_evidence(self):
        counts = CollectorRepository(self.db).persist_mlb_transactions(self.parse())
        self.assertEqual(counts, {"transactions": 3, "rosters": 3, "injuries": 1})

        transactions = self.db.execute(
            "SELECT * FROM transaction_snapshots ORDER BY transaction_snapshot_id"
        ).fetchall()
        self.assertEqual(len(transactions), 3)
        self.assertTrue(all(row["official"] == 1 for row in transactions))
        self.assertTrue(all(row["source_key"] == "mlb_transactions" for row in transactions))

        rosters = self.db.execute(
            "SELECT player_id, roster_status FROM roster_snapshots "
            "WHERE source_key='mlb_transactions' ORDER BY roster_snapshot_id"
        ).fetchall()
        self.assertEqual(
            [(row["player_id"], row["roster_status"]) for row in rosters],
            [
                ("mlb_person_3001", "injured_list"),
                ("mlb_person_3001", "active"),
                ("mlb_person_3002", "optioned"),
            ],
        )

        injury = self.db.execute(
            "SELECT * FROM injury_snapshots WHERE source_key='mlb_transactions'"
        ).fetchone()
        self.assertIsNotNone(injury)
        self.assertEqual(injury["player_id"], "mlb_person_3001")
        self.assertEqual(injury["game_id"], None)
        self.assertEqual(injury["game_status"], "out")
        self.assertEqual(injury["official"], 1)

    def test_storage_is_idempotent_and_preserves_first_known_time(self):
        repo = CollectorRepository(self.db)
        first = repo.persist_mlb_transactions(self.parse())
        second_batch = parse_mlb_transactions(
            self.payload(),
            team_resolver=self.teams,
            collected_at=datetime(2026, 8, 16, 21, 0, tzinfo=UTC),
            start_date="2026-08-09",
            end_date="2026-08-16",
        )
        second = repo.persist_mlb_transactions(second_batch)
        self.assertEqual(first["transactions"], 3)
        self.assertEqual(second, {"transactions": 0, "rosters": 0, "injuries": 0})
        first_source_time = self.db.execute(
            "SELECT source_updated_at FROM transaction_snapshots "
            "WHERE player_id='mlb_person_3001' AND transaction_date='2026-08-15'"
        ).fetchone()[0]
        self.assertEqual(first_source_time, NOW.isoformat())


if __name__ == "__main__":
    unittest.main()
