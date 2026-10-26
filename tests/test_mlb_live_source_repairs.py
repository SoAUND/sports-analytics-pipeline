import json
import unittest
from datetime import datetime, timezone

from sports_analytics_pipeline.collectors.base import CollectionBatch, MappingTeamResolver
from sports_analytics_pipeline.collectors.http import HttpResponse
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import alias_mapping, seed_reference_data
from sports_analytics_pipeline.mlb_live_sources import (
    AffiliateAwareMlbTransactionsCollector,
    add_bookmaker_response_diagnostics,
    parse_mlb_transactions_with_affiliates,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 18, 21, 0, tzinfo=UTC)


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


class MlbLiveSourceRepairTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)
        seed_reference_data(self.db, include_mlb=True)
        self.teams = MappingTeamResolver(alias_mapping(self.db, "mlb"))

    def tearDown(self):
        self.db.close()

    def affiliate_payload(self):
        return {
            "transactions": [
                {
                    "person": {
                        "id": 7001,
                        "fullName": "Affiliate Player",
                        "primaryPosition": {"abbreviation": "P"},
                    },
                    "fromTeam": {"name": "Sacramento River Cats"},
                    "toTeam": {"name": "San Francisco Giants"},
                    "effectiveDate": "2026-08-18",
                    "typeDesc": "Status Change",
                    "description": (
                        "San Francisco Giants recalled Affiliate Player from "
                        "Sacramento River Cats."
                    ),
                }
            ]
        }

    def test_affiliate_endpoint_is_not_falsely_mapped_to_parent_mlb_team(self):
        batch = parse_mlb_transactions_with_affiliates(
            self.affiliate_payload(),
            team_resolver=self.teams,
            collected_at=NOW,
            start_date="2026-08-11",
            end_date="2026-08-18",
        )
        self.assertEqual(len(batch.records), 1)
        record = batch.records[0]
        self.assertIsNone(record.from_team_id)
        self.assertEqual(record.to_team_id, "mlb_sf")
        self.assertEqual(record.roster_status, "active")
        self.assertTrue(any("Sacramento River Cats" in warning for warning in batch.warnings))

    def test_unknown_only_transaction_still_fails_closed(self):
        payload = self.affiliate_payload()
        payload["transactions"][0].pop("toTeam")
        payload["transactions"][0]["fromTeam"]["name"] = "Imaginary MLB Club"
        with self.assertRaises(ValueError):
            parse_mlb_transactions_with_affiliates(
                payload,
                team_resolver=self.teams,
                collected_at=NOW,
                start_date="2026-08-11",
                end_date="2026-08-18",
            )

    def test_affiliate_aware_collector_keeps_official_endpoint_contract(self):
        transport = FakeTransport(self.affiliate_payload())
        batch = AffiliateAwareMlbTransactionsCollector(
            transport=transport, team_resolver=self.teams
        ).collect_window("2026-08-18", lookback_days=7)
        url, params, headers = transport.calls[0]
        self.assertEqual(url, "https://statsapi.mlb.com/api/v1/transactions")
        self.assertEqual(params["sportId"], "1")
        self.assertEqual(params["startDate"], "08/11/2026")
        self.assertEqual(params["endDate"], "08/18/2026")
        self.assertEqual(params["hydrate"], "person")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(batch.records[0].to_team_id, "mlb_sf")

    def test_odds_diagnostics_expose_missing_requested_primary_book_without_substitution(self):
        payload = [
            {
                "id": "event-1",
                "bookmakers": [
                    {"key": "draftkings"},
                    {"key": "fanduel"},
                    {"key": "betmgm"},
                ],
            }
        ]
        batch = CollectionBatch(
            provider_key="the_odds_api",
            collected_at=NOW,
            raw_payload=payload,
            quotes=(),
            warnings=(),
            metadata={"endpoint": "current_mlb_odds"},
        )
        diagnosed = add_bookmaker_response_diagnostics(
            batch,
            requested_bookmakers=(
                "hardrockbet_fl",
                "draftkings",
                "fanduel",
                "betmgm",
            ),
        )
        self.assertEqual(
            diagnosed.metadata["returned_bookmakers"],
            ["betmgm", "draftkings", "fanduel"],
        )
        self.assertEqual(
            diagnosed.metadata["missing_requested_bookmakers"],
            ["hardrockbet_fl"],
        )
        self.assertTrue(any("hardrockbet_fl" in warning for warning in diagnosed.warnings))
        self.assertEqual(diagnosed.quotes, ())


if __name__ == "__main__":
    unittest.main()
