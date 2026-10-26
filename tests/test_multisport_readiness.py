import json
import os
from pathlib import Path
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sports_analytics_pipeline.collectors.http import HttpResponse
from sports_analytics_pipeline.collectors.multisport_odds import MultiSportOddsCollector
from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.multisport_operations import bootstrap, collect_results, collect_sport, run_cycle
from sports_analytics_pipeline.multisport_preflight import preflight_report
from sports_analytics_pipeline.multisport_research import live_readiness_report


UTC = timezone.utc
NOW = datetime(2026, 8, 17, 23, 30, tzinfo=UTC)


class FakeTransport:
    def __init__(self, payloads, remaining=500, received_at=NOW):
        self.payloads = payloads
        self.remaining = remaining
        self.received_at = received_at
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, dict(params or {})))
        path = url.split("/v4/", 1)[1].rstrip("/")
        if path not in self.payloads:
            raise AssertionError(f"Unexpected GET: {path}")
        return HttpResponse(
            status=200,
            body=json.dumps(self.payloads[path]).encode("utf-8"),
            headers={
                "x-requests-remaining": str(self.remaining),
                "x-requests-used": "1",
                "x-requests-last": "0" if path == "sports" or path.endswith("events") else "1",
            },
            received_at=self.received_at,
        )


def settings():
    with patch.dict(
        os.environ,
        {
            "BOT_MODE": "test_only",
            "THE_ODDS_API_KEY": "secret",
            "MARKET_CONSENSUS_BOOKMAKERS": "hardrockbet",
            "ACCESSIBLE_BOOKMAKERS": "hardrockbet",
        },
        clear=True,
    ):
        return Settings.load("missing.env")


class MultiSportReadinessTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)
        bootstrap(self.db, as_of=NOW)

    def tearDown(self):
        self.db.close()

    def test_new_quota_settings_have_safe_defaults(self):
        cfg = settings()
        self.assertEqual(cfg.odds_api_min_remaining_credits, 50)
        self.assertEqual(cfg.multisport_odds_horizon_minutes, 90)
        self.assertEqual(cfg.multisport_participant_refresh_hours, 24)

    def test_far_future_event_uses_free_discovery_only(self):
        payloads = {
            "sports": [{"key": "basketball_wnba", "active": True}],
            "sports/basketball_wnba/events": [{
                "id": "future-1",
                "sport_key": "basketball_wnba",
                "commence_time": "2026-08-18T04:00:00Z",
                "home_team": "Team A",
                "away_team": "Team B",
            }],
        }
        transport = FakeTransport(payloads)
        collector = MultiSportOddsCollector(
            api_key="secret", transport=transport, bookmakers=("hardrockbet",)
        )
        catalog = collector.collect_sports_catalog()
        result = collect_sport(
            self.db,
            settings=settings(),
            sport="wnba",
            as_of=NOW,
            collector=collector,
            catalog=catalog,
            transport=transport,
        )
        self.assertEqual(result["provider_keys_in_window"], 0)
        paths = [call[0] for call in transport.calls]
        self.assertTrue(any(path.rstrip("/").endswith("/events") for path in paths))
        self.assertFalse(any("participants" in path for path in paths))
        self.assertFalse(any(path.rstrip("/").endswith("/odds") for path in paths))

    def test_low_quota_reserve_blocks_cost_bearing_calls(self):
        payloads = {
            "sports": [{"key": "basketball_wnba", "active": True}],
            "sports/basketball_wnba/events": [{
                "id": "soon-1",
                "sport_key": "basketball_wnba",
                "commence_time": "2026-08-18T00:00:00Z",
                "home_team": "Team A",
                "away_team": "Team B",
            }],
        }
        transport = FakeTransport(payloads, remaining=50)
        collector = MultiSportOddsCollector(
            api_key="secret", transport=transport, bookmakers=("hardrockbet",)
        )
        catalog = collector.collect_sports_catalog()
        result = collect_sport(
            self.db,
            settings=settings(),
            sport="wnba",
            as_of=NOW,
            collector=collector,
            catalog=catalog,
            transport=transport,
        )
        self.assertEqual(result["provider_keys_in_window"], 1)
        self.assertTrue(result["notes"])
        paths = [call[0] for call in transport.calls]
        self.assertFalse(any("participants" in path for path in paths))
        self.assertFalse(any(path.rstrip("/").endswith("/odds") for path in paths))

    def test_score_api_is_not_polled_without_unsettled_paper_decision(self):
        result = collect_results(
            self.db,
            settings=settings(),
            sport="wnba",
            as_of=NOW,
        )
        self.assertEqual(result["stored"], 0)
        self.assertIn("not called", result["note"])

    def test_cycle_reports_odds_received_after_its_start_without_one_run_delay(self):
        received_at = NOW + timedelta(seconds=4)
        event_time = NOW + timedelta(minutes=30)
        provider_key = "tennis_wta_monterrey_open"
        stamp = (NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        payloads = {
            "sports": [{"key": provider_key, "active": True}],
            f"sports/{provider_key}/participants": [
                {"id": "p-home", "full_name": "Clara Tauson"},
                {"id": "p-away", "full_name": "Renata Zarazua"},
            ],
            f"sports/{provider_key}/events": [{
                "id": "event-1",
                "sport_key": provider_key,
                "commence_time": event_time.isoformat().replace("+00:00", "Z"),
                "home_team": "Clara Tauson",
                "away_team": "Renata Zarazua",
            }],
            f"sports/{provider_key}/odds": [{
                "id": "event-1",
                "sport_key": provider_key,
                "commence_time": event_time.isoformat().replace("+00:00", "Z"),
                "home_team": "Clara Tauson",
                "away_team": "Renata Zarazua",
                "bookmakers": [{
                    "key": "hardrockbet",
                    "title": "Primary Sportsbook",
                    "last_update": stamp,
                    "markets": [{
                        "key": "h2h",
                        "last_update": stamp,
                        "outcomes": [
                            {"name": "Clara Tauson", "price": 1.70},
                            {"name": "Renata Zarazua", "price": 2.15},
                        ],
                    }],
                }],
            }],
        }
        transport = FakeTransport(payloads, received_at=received_at)

        with patch("sports_analytics_pipeline.multisport_operations._transport", return_value=transport):
            result = run_cycle(
                self.db,
                settings=settings(),
                sports=("wta",),
                as_of=NOW,
            )

        self.assertEqual(result["as_of"], received_at.isoformat())
        self.assertEqual(result["sports"]["wta"]["collection"]["quotes"], 2)
        self.assertEqual(result["sports"]["wta"]["collection"]["odds_research"], 1)
        self.assertEqual(result["training"]["minimum_examples_per_sport"], 50)
        self.assertFalse(result["training"]["all_selected_models_trained"])
        self.assertEqual(
            result["training"]["sports"]["wta"],
            {
                "historical_training_examples": 0,
                "training_minimum": 50,
                "examples_needed": 50,
                "training_ready": False,
                "model_version": None,
                "model_status": "not_trained",
            },
        )
        event_id = f"wta:{provider_key}:event-1"
        report = live_readiness_report(
            self.db,
            event_id=event_id,
            sport="wta",
            as_of=datetime.fromisoformat(result["as_of"]),
        )
        self.assertEqual(report.category_statuses["current_odds"], "verified")
        self.assertNotIn("current_odds:missing", report.blockers)

    def test_preflight_truthfully_blocks_untrained_sports(self):
        report = preflight_report(self.db, settings=settings())
        self.assertTrue(report["all_collection_ready"])
        self.assertFalse(report["all_decision_ready"])
        self.assertFalse(report["sports"]["nba"]["training_ready"])
        self.assertIn("No trained test-only model", report["sports"]["nba"]["blockers"])
        self.assertEqual(report["sports"]["atp"]["settlement_status"], "verified")

    def test_workflow_enables_all_supported_sports_and_has_quota_safeguards(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "multisport-paper.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('if [ "$EVENT_NAME" = "schedule" ]', workflow)
        self.assertIn('SPORTS="nba,wnba,epl,mma,atp,wta"', workflow)
        self.assertIn(
            "Every supported sport is approved for automatic scheduled test-only evaluation.",
            workflow,
        )
        self.assertIn("ODDS_API_MIN_REMAINING_CREDITS", workflow)
        self.assertIn("MULTISPORT_ODDS_HORIZON_MINUTES", workflow)
        self.assertIn("train-all", workflow)
        self.assertIn("BOT_MODE: research", workflow)
        self.assertNotIn("place wager", workflow.casefold())


if __name__ == "__main__":
    unittest.main()

