from pathlib import Path
import subprocess
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CollectorCliTests(unittest.TestCase):
    def test_rundown_command_rejects_unsupported_sport_before_collection(self):
        result = subprocess.run(
            [
                sys.executable,
                "scripts/run_collectors.py",
                "rundown-odds",
                "--date",
                "2026-09-13",
                "--sport-id",
                "1",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)

    def test_rundown_command_requires_explicit_date(self):
        result = subprocess.run(
            [
                sys.executable,
                "scripts/run_collectors.py",
                "rundown-odds",
                "--sport-id",
                "2",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--date", result.stderr)


if __name__ == "__main__":
    unittest.main()
