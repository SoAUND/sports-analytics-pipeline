from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "multisport-paper.yml"
HISTORY_WORKFLOW = ROOT / ".github" / "workflows" / "multisport-history.yml"


class MultiSportWorkflowTests(unittest.TestCase):
    def test_scheduler_is_test_only_gated_and_persistent(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('cron: "*/5 * * * *"', text)
        self.assertIn("Free discovery still checks every five minutes", text)
        self.assertIn('SPORTS="nba,wnba,epl,mma,atp,wta"', text)
        self.assertIn("BOT_MODE: research", text)
        self.assertIn("MULTISPORT_ODDS_HORIZON_MINUTES", text)
        self.assertIn("ODDS_API_MIN_REMAINING_CREDITS", text)
        self.assertIn("ODDS_API_DAILY_CREDIT_CAP", text)
        self.assertIn("MULTISPORT_ODDS_REFRESH_MINUTES", text)
        self.assertIn("MULTISPORT_SCORE_POLL_MINUTES", text)
        self.assertIn("MULTISPORT_MMA_RESULT_POLL_MINUTES", text)
        self.assertIn("multisport-paper-database", text)
        self.assertIn("our_multisport_bot.sqlite3", text)
        self.assertIn("MARKET_CONSENSUS_BOOKMAKERS", text)
        self.assertIn("ACCESSIBLE_BOOKMAKERS", text)
        self.assertNotIn("place_wager", text)
        self.assertNotIn("auto_wager", text)

    def test_scheduled_run_includes_all_supported_sports_after_explicit_project_approval(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('if [ "$EVENT_NAME" = "schedule" ]', text)
        self.assertIn('SPORTS="nba,wnba,epl,mma,atp,wta"', text)
        self.assertIn(
            "Every supported sport is approved for automatic scheduled test-only evaluation.",
            text,
        )
        self.assertIn("test-only/non-countable until their own readiness requirements are satisfied", text)

    def test_all_provider_secrets_are_wired_without_automatic_live_checks(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        for secret in (
            "THE_ODDS_API_KEY",
            "ODDSPAPI_API_KEY",
            "ODDS_API_IO_KEY",
            "SPORTSGAMEODDS_API_KEY",
            "API_SPORTS_KEY",
            "CFBD_API_KEY",
            "FOOTBALL_DATA_API_KEY",
        ):
            self.assertIn(f"secrets.{secret}", text)
        self.assertIn("check_provider_connections", text)
        self.assertIn("provider-health --require-all", text)
        self.assertIn("provider-health --live --require-all", text)
        self.assertIn("github.event_name == 'workflow_dispatch'", text)

    def test_live_research_feeds_are_bounded_and_secret_backed(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("MULTISPORT_RESEARCH_FEEDS_JSON", text)
        self.assertIn("MULTISPORT_RESEARCH_MAX_REQUESTS", text)
        self.assertIn("MULTISPORT_RESEARCH_HORIZON_MINUTES", text)
        self.assertIn("secrets.MULTISPORT_RESEARCH_FEED_TOKEN", text)
        self.assertIn("'30'", text)

    def test_historical_collector_is_daily_bounded_and_fail_closed(self):
        text = HISTORY_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('cron: "43 8 * * *"', text)
        self.assertIn("MULTISPORT_HISTORY_AUTOMATION_ENABLED", text)
        self.assertIn("MULTISPORT_HISTORY_MAX_REQUESTS", text)
        self.assertIn("MULTISPORT_HISTORY_MAX_ODDS_EVENTS", text)
        self.assertIn("ODDS_API_IO_HISTORY_BOOKMAKERS", text)
        self.assertIn("secrets.ODDS_API_IO_KEY", text)
        self.assertIn("secrets.FOOTBALL_DATA_API_KEY", text)
        self.assertIn("history-collect", text)
        self.assertIn("history-backfill-epl-features --max-requests 4", text)
        self.assertIn("history-build-examples --sports epl", text)
        self.assertIn("build_examples:", text)
        self.assertIn("backfill_epl_features:", text)
        self.assertIn("history_start:", text)
        self.assertIn("history_end:", text)
        self.assertIn('default: "2026-03-01T00:00:00+00:00"', text)
        self.assertIn('default: "2026-06-01T00:00:00+00:00"', text)
        self.assertIn("--verified-epl-odds-only", text)
        self.assertIn("--odds-only", text)
        self.assertIn(
            '[ "$INCLUDE_ODDS" = "true" ] && \\\n             [ "$SPORTS" = "epl" ]',
            text,
        )
        self.assertIn("Collect officially verified EPL closing odds", text)
        self.assertIn("needs.gate.outputs.run_history == 'true'", text)
        self.assertIn("needs.gate.outputs.backfill_epl_features == 'true'", text)
        self.assertIn("needs.gate.outputs.build_examples == 'true'", text)
        self.assertIn('default: "nfl,mlb,nba,wnba,epl,mma,atp,wta"', text)
        self.assertIn("for REQUIRED_SPORT in nfl mlb nba wnba epl mma atp wta", text)
        self.assertIn("MULTISPORT_HISTORY_MAX_REQUESTS || '30'", text)
        self.assertIn("BOT_MODE: research", text)
        self.assertNotIn("place_wager", text)
        self.assertLess(
            text.index("- name: Backfill safe EPL result-derived features"),
            text.index("- name: Collect officially verified EPL closing odds"),
        )
        self.assertLess(
            text.index("- name: Collect officially verified EPL closing odds"),
            text.index("- name: Build leakage-safe EPL training examples"),
        )


    def test_automatic_result_code_triggers_a_paper_cycle(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('src/sports_analytics_pipeline/collectors/multisport_structured_results.py', text)
        self.assertIn('src/sports_analytics_pipeline/multisport_results.py', text)
        self.assertIn('scripts/mark_multisport_result_notified.py', text)
        self.assertIn('secrets.SPORTSGAMEODDS_API_KEY', text)


if __name__ == "__main__":
    unittest.main()

