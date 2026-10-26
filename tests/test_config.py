from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.config import Settings


class ConfigTests(unittest.TestCase):
    def test_phase_one_rejects_non_test_mode(self):
        with patch.dict(os.environ, {"BOT_MODE": "official"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.load("missing.env")

    def test_safe_summary_never_contains_key_value(self):
        with patch.dict(
            os.environ,
            {
                "BOT_MODE": "test_only",
                "THE_ODDS_API_KEY": "secret-value",
                "THERUNDOWN_API_KEY": "second-secret-value",
                "ODDSPAPI_API_KEY": "oddspapi-secret-value",
                "ODDS_API_IO_KEY": "odds-io-secret-value",
                "SPORTSGAMEODDS_API_KEY": "sgo-secret-value",
                "API_SPORTS_KEY": "api-sports-secret-value",
                "CFBD_API_KEY": "cfbd-secret-value",
                "FOOTBALL_DATA_API_KEY": "football-data-secret-value",
            },
            clear=True,
        ):
            settings = Settings.load("missing.env")
            self.assertTrue(settings.safe_summary()["odds_api_key_configured"])
            self.assertTrue(settings.safe_summary()["therundown_api_key_configured"])
            self.assertNotIn("secret-value", repr(settings.safe_summary()))
            self.assertNotIn("second-secret-value", repr(settings.safe_summary()))
            for key in settings.provider_api_keys().values():
                self.assertNotIn(str(key), repr(settings.safe_summary()))

    def test_all_seven_provider_keys_can_be_required_together(self):
        with patch.dict(
            os.environ,
            {
                "THE_ODDS_API_KEY": "one",
                "ODDSPAPI_API_KEY": "two",
                "ODDS_API_IO_KEY": "three",
                "SPORTSGAMEODDS_API_KEY": "four",
                "API_SPORTS_KEY": "five",
                "CFBD_API_KEY": "six",
                "FOOTBALL_DATA_API_KEY": "seven",
            },
            clear=True,
        ):
            Settings.load("missing.env").require_provider_api_keys()

    def test_missing_provider_keys_fail_closed(self):
        with patch.dict(os.environ, {"THE_ODDS_API_KEY": "one"}, clear=True):
            with self.assertRaises(RuntimeError):
                Settings.load("missing.env").require_provider_api_keys()

    def test_safe_summary_never_contains_nws_contact(self):
        with patch.dict(
            os.environ,
            {"NWS_USER_AGENT": "SportsAnalyticsPipeline/0.2 (contact: private@example.com)"},
            clear=True,
        ):
            summary = Settings.load("missing.env").safe_summary()
            self.assertTrue(summary["nws_user_agent_configured"])
            self.assertNotIn("private@example.com", repr(summary))

    def test_therundown_key_is_required_only_by_its_collector(self):
        with patch.dict(os.environ, {"BOT_MODE": "test_only"}, clear=True):
            settings = Settings.load("missing.env")
            with self.assertRaises(RuntimeError):
                settings.require_therundown_api_key()

    def test_sportsbook_lists_start_unapproved(self):
        with patch.dict(os.environ, {"BOT_MODE": "test_only"}, clear=True):
            settings = Settings.load("missing.env")
            self.assertFalse(settings.sportsbook_lists_approved())

    def test_live_weather_requires_real_contact(self):
        with patch.dict(os.environ, {"BOT_MODE": "test_only"}, clear=True):
            settings = Settings.load("missing.env")
            with self.assertRaises(RuntimeError):
                settings.require_nws_user_agent()

    def test_historical_limits_are_bounded(self):
        with patch.dict(
            os.environ,
            {
                "MULTISPORT_HISTORY_LOOKBACK_DAYS": "32",
                "MULTISPORT_HISTORY_MAX_REQUESTS": "30",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                Settings.load("missing.env")

    def test_historical_bookmakers_are_not_secrets(self):
        with patch.dict(
            os.environ,
            {"ODDS_API_IO_HISTORY_BOOKMAKERS": "DraftKings, FanDuel"},
            clear=True,
        ):
            settings = Settings.load("missing.env")
            self.assertEqual(
                settings.odds_api_io_history_bookmakers,
                ("DraftKings", "FanDuel"),
            )

    def test_live_research_feed_settings_are_bounded_and_secret_safe(self):
        with patch.dict(
            os.environ,
            {
                "MULTISPORT_RESEARCH_FEEDS_JSON": (
                    '{"nba_official":"https://feeds.example/nba"}'
                ),
                "MULTISPORT_RESEARCH_FEED_TOKEN": "private-feed-token",
                "MULTISPORT_RESEARCH_MAX_REQUESTS": "30",
                "MULTISPORT_RESEARCH_HORIZON_MINUTES": "360",
            },
            clear=True,
        ):
            settings = Settings.load("missing.env")
            self.assertEqual(
                settings.multisport_research_feed_urls,
                {"nba_official": "https://feeds.example/nba"},
            )
            summary = settings.safe_summary()
            self.assertEqual(summary["multisport_research_feed_source_keys"], ("nba_official",))
            self.assertTrue(summary["multisport_research_feed_token_configured"])
            self.assertNotIn("private-feed-token", repr(summary))
            self.assertNotIn("https://feeds.example/nba", repr(summary))

        with patch.dict(
            os.environ,
            {"MULTISPORT_RESEARCH_MAX_REQUESTS": "31"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                Settings.load("missing.env")


if __name__ == "__main__":
    unittest.main()
