import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.collectors.http import HttpResponse
from sports_analytics_pipeline.collectors.provider_services import (
    CLIENTS,
    configuration_report,
    live_probe_report,
    routing_report,
    safe_provider_report,
)
from sports_analytics_pipeline.config import Settings


NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)
SECRET_VALUES = {
    "THE_ODDS_API_KEY": "secret-the-odds",
    "ODDSPAPI_API_KEY": "secret-oddspapi",
    "ODDS_API_IO_KEY": "secret-odds-io",
    "SPORTSGAMEODDS_API_KEY": "secret-sgo",
    "API_SPORTS_KEY": "secret-api-sports",
    "CFBD_API_KEY": "secret-cfbd",
    "FOOTBALL_DATA_API_KEY": "secret-football-data",
}


class ProbeTransport:
    def __init__(self):
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        payload = [] if "the-odds-api" in url or "oddspapi" in url or "collegefootball" in url else {}
        return HttpResponse(
            status=200,
            body=json.dumps(payload).encode("utf-8"),
            headers={},
            received_at=NOW,
        )


def configured_settings():
    with patch.dict(os.environ, {"BOT_MODE": "test_only", **SECRET_VALUES}, clear=True):
        return Settings.load("missing.env")


class ProviderServiceTests(unittest.TestCase):
    def test_live_probes_use_each_official_auth_contract(self):
        transport = ProbeTransport()
        report = live_probe_report(configured_settings(), transport=transport)
        self.assertEqual(set(report), set(CLIENTS))
        self.assertTrue(all(probe.reachable for probe in report.values()))

        calls = {url: (params, headers) for url, params, headers in transport.calls}
        self.assertEqual(calls[CLIENTS["the_odds_api"].endpoint][0]["apiKey"], "secret-the-odds")
        self.assertEqual(calls[CLIENTS["oddspapi"].endpoint][0]["apiKey"], "secret-oddspapi")
        self.assertEqual(calls[CLIENTS["odds_api_io"].endpoint][0]["apiKey"], "secret-odds-io")
        self.assertEqual(calls[CLIENTS["sports_game_odds"].endpoint][1]["x-api-key"], "secret-sgo")
        self.assertEqual(calls[CLIENTS["api_sports"].endpoint][1]["x-apisports-key"], "secret-api-sports")
        self.assertEqual(
            calls[CLIENTS["cfbd"].endpoint][1]["Authorization"],
            "Bearer secret-cfbd",
        )
        self.assertEqual(
            calls[CLIENTS["football_data"].endpoint][1]["X-Auth-Token"],
            "secret-football-data",
        )

    def test_reports_never_contain_secret_values(self):
        transport = ProbeTransport()
        probes = live_probe_report(configured_settings(), transport=transport)
        displayed = repr(safe_provider_report(probes))
        for secret in SECRET_VALUES.values():
            self.assertNotIn(secret, displayed)

    def test_probe_errors_redact_the_key(self):
        class FailingTransport:
            def get(self, url, *, params=None, headers=None):
                supplied = next(iter((params or {}).values()), "secret-the-odds")
                raise RuntimeError(f"request failed for {supplied}")

        probes = live_probe_report(
            configured_settings(),
            transport=FailingTransport(),
            provider_keys=("the_odds_api",),
        )
        self.assertFalse(probes["the_odds_api"].reachable)
        self.assertNotIn("secret-the-odds", repr(probes))
        self.assertIn("[REDACTED]", probes["the_odds_api"].error)

    def test_configuration_check_makes_no_network_calls(self):
        probes = configuration_report(configured_settings())
        self.assertTrue(all(probe.configured for probe in probes.values()))
        self.assertTrue(all(probe.reachable is None for probe in probes.values()))

    def test_backup_routes_are_never_automatic(self):
        routes = routing_report(configured_settings())
        market = next(row for row in routes if row["data_kind"] == "market_odds")
        self.assertEqual(market["primary"], "the_odds_api")
        self.assertFalse(market["automatic_fallback"])
        self.assertEqual(
            tuple(row["provider"] for row in market["fallbacks"]),
            ("oddspapi", "odds_api_io", "sports_game_odds"),
        )


if __name__ == "__main__":
    unittest.main()
