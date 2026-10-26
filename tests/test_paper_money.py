from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from sports_analytics_pipeline.paper_money import paper_unit_dollars, units_to_dollars


class PaperMoneyTests(unittest.TestCase):
    def test_default_unit_is_five_hypothetical_dollars(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(paper_unit_dollars(), 5.0)
            self.assertEqual(units_to_dollars(0.91), 4.55)
            self.assertEqual(units_to_dollars(-1.0), -5.0)

    def test_configured_unit_changes_display_conversion(self):
        with patch.dict(os.environ, {"PAPER_UNIT_DOLLARS": "2"}):
            self.assertEqual(paper_unit_dollars(), 2.0)
            self.assertEqual(units_to_dollars(1.8), 3.6)

    def test_invalid_unit_is_rejected(self):
        with patch.dict(os.environ, {"PAPER_UNIT_DOLLARS": "0"}):
            with self.assertRaises(ValueError):
                paper_unit_dollars()


if __name__ == "__main__":
    unittest.main()
