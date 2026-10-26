from decimal import Decimal
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.calculations import (
    CalculationError,
    american_to_decimal,
    break_even_probability,
    conservative_push_probabilities,
    decimal_to_american,
    minimum_acceptable_decimal_odds,
    push_aware_ev,
    qualifies_price,
    remove_vig,
    standard_ev,
)


class CalculationTests(unittest.TestCase):
    def assertDecimalAlmostEqual(self, actual, expected, places=8):
        self.assertAlmostEqual(float(actual), float(expected), places=places)

    def test_negative_american_to_decimal(self):
        self.assertDecimalAlmostEqual(american_to_decimal(-110), Decimal("1.909090909"))

    def test_positive_american_to_decimal(self):
        self.assertEqual(american_to_decimal(150), Decimal("2.5"))

    def test_decimal_to_american(self):
        self.assertDecimalAlmostEqual(decimal_to_american(american_to_decimal(-110)), -110)
        self.assertEqual(decimal_to_american(Decimal("2.5")), Decimal("150.0"))

    def test_break_even_probability(self):
        self.assertDecimalAlmostEqual(
            break_even_probability(american_to_decimal(-110)),
            Decimal("0.5238095238"),
        )

    def test_no_vig_probabilities_sum_to_one(self):
        probabilities = remove_vig([american_to_decimal(-110), american_to_decimal(-110)])
        self.assertEqual(probabilities, (Decimal("0.5"), Decimal("0.5")))
        self.assertEqual(sum(probabilities), Decimal("1"))

    def test_standard_ev_matches_approved_example(self):
        ev = standard_ev(Decimal("0.58"), american_to_decimal(-110))
        self.assertDecimalAlmostEqual(ev, Decimal("0.1072727273"))

    def test_push_aware_ev(self):
        ev = push_aware_ev("0.52", "0.43", "0.05", "1.91")
        self.assertEqual(ev, Decimal("0.0432"))

    def test_conservative_push_probabilities(self):
        p_win, p_loss, p_push, conditional = conservative_push_probabilities(
            "0.58", "0.03", "0.02"
        )
        self.assertEqual(conditional, Decimal("0.56"))
        self.assertEqual(p_win, Decimal("0.5432"))
        self.assertEqual(p_loss, Decimal("0.4268"))
        self.assertEqual(p_win + p_loss + p_push, Decimal("1.0000"))

    def test_minimum_price_preserves_five_point_edge(self):
        minimum = minimum_acceptable_decimal_odds("0.58")
        self.assertDecimalAlmostEqual(minimum, Decimal("1.8867924528"))
        self.assertTrue(qualifies_price("1.91", "0.58"))
        self.assertFalse(qualifies_price("1.88", "0.58"))

    def test_invalid_probability_sum_fails(self):
        with self.assertRaises(CalculationError):
            push_aware_ev("0.50", "0.40", "0.05", "1.91")

    def test_invalid_odds_fail(self):
        with self.assertRaises(CalculationError):
            american_to_decimal(0)
        with self.assertRaises(CalculationError):
            break_even_probability(1)


if __name__ == "__main__":
    unittest.main()
