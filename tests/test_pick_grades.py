import unittest

from sports_analytics_pipeline.pick_grades import format_pick_strength, pick_strength_grade


class PickGradeTests(unittest.TestCase):
    def test_every_grade_boundary(self):
        cases = {
            100: "A+", 95: "A+", 94: "A", 90: "A", 89: "A-", 85: "A-",
            84: "B+", 82: "B+", 81: "B", 79: "B", 78: "B-", 75: "B-",
            74: "C+", 70: "C+", 69: "C", 65: "C", 64: "C-", 60: "C-",
            59: "D+", 55: "D+", 54: "D", 50: "D", 49: "D-", 45: "D-",
            44: "F+", 35: "F+", 34: "F", 20: "F", 19: "F-", 0: "F-",
        }
        for score, grade in cases.items():
            with self.subTest(score=score):
                self.assertEqual(pick_strength_grade(score), grade)

    def test_display_keeps_grade_separate_from_probability(self):
        self.assertEqual(format_pick_strength(78), "78/100 (Grade B-)")
        self.assertIsNone(format_pick_strength(None))

    def test_invalid_scores_are_rejected(self):
        for score in (-1, 101):
            with self.assertRaises(ValueError):
                pick_strength_grade(score)
        with self.assertRaises(TypeError):
            pick_strength_grade(75.0)


if __name__ == "__main__":
    unittest.main()
