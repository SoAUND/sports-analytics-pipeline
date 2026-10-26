from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "telegram-paper-results.yml"


class TelegramWorkflowTests(unittest.TestCase):
    def test_legacy_test_report_job_is_muted(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        notify_header = text.split("  notify:\n", 1)[1].split("    runs-on:", 1)[0]
        self.assertIn("if: ${{ false }}", notify_header)

    def test_software_tests_run_without_telegram_reports(self):
        text = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
        notification = text.split("- name: Send main-branch test result to Telegram", 1)[1]
        self.assertIn("if: ${{ false }}", notification)
        self.assertIn("python -m unittest discover -s tests -v", text)
        self.assertIn("node --test research_adapter/test/*.test.js", text)
        test_header = text.split("  test:\n", 1)[1].split("    steps:", 1)[0]
        self.assertNotIn("if: ${{ false }}", test_header)

    def test_active_research_delivery_is_not_muted(self):
        text = (ROOT / ".github/workflows/revised-research.yml").read_text(encoding="utf-8")
        self.assertNotIn("if: ${{ false }}", text)
        self.assertIn("scripts/run_revised_research.py", text)

    def test_gated_noop_runs_do_not_send_telegram_messages(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Inspect source workflow work", text)
        self.assertIn('TARGET_JOB="database"', text)
        self.assertIn('TARGET_JOB="paper"', text)
        self.assertIn("should_notify=$SHOULD_NOTIFY", text)
        self.assertIn(
            "if: steps.source.outputs.should_notify == 'true' && "
            "env.SOURCE_WORKFLOW == 'MLB Paper Database'",
            text,
        )
        self.assertIn(
            "if: steps.source.outputs.should_notify == 'true' && "
            "env.SOURCE_WORKFLOW == 'Multi-Sport Paper Database'",
            text,
        )
        self.assertNotIn(
            "Send MLB paper result to Telegram\n"
            "        if: env.SOURCE_WORKFLOW == 'MLB Paper Database'",
            text,
        )
        self.assertNotIn(
            "Send multi-sport paper result to Telegram\n"
            "        if: env.SOURCE_WORKFLOW == 'Multi-Sport Paper Database'",
            text,
        )

    def test_real_runs_remain_test_only(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("BOT_MODE: research", text)
        self.assertIn("TELEGRAM_BOT_TOKEN", text)
        self.assertIn("TELEGRAM_CHAT_ID", text)
        self.assertNotIn("place_wager", text)
        self.assertNotIn("auto_wager", text)


    def test_settled_results_are_sent_then_marked_and_persisted(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Send newly settled results to Telegram exactly once", text)
        self.assertIn("--settled-out-dir var/telegram-settled", text)
        self.assertIn("scripts/mark_multisport_result_notified.py", text)
        self.assertIn("Save canonical database with delivery state", text)
        self.assertIn("name: multisport-paper-database", text)
        self.assertLess(
            text.index("scripts/send_telegram_notification.py", text.index("Send newly settled results")),
            text.index("scripts/mark_multisport_result_notified.py", text.index("Send newly settled results")),
        )


if __name__ == "__main__":
    unittest.main()
