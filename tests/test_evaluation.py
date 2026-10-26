from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.evaluation import (
    build_best_three_leg_parlay,
    CandidateInput,
    EvaluationPolicy,
    evaluate_candidate,
    evaluate_upcoming,
    score_candidate,
    select_best_straight_pick,
)
from sports_analytics_pipeline.calculations import remove_vig


UTC = timezone.utc


def candidate(**changes):
    values = {
        "game_id": "game-1",
        "season_type": "regular",
        "team_id": "BUF",
        "opponent_team_id": "MIA",
        "spread_points": Decimal("-3"),
        "sportsbook_key": "hardrockbet_fl",
        "odds_snapshot_id": 1,
        "decimal_odds": Decimal("1.91"),
        "checked_at": datetime(2026, 9, 10, 16, 0, tzinfo=UTC),
        "no_vig_probability": Decimal("0.50"),
        "no_vig_probability_low": Decimal("0.49"),
        "no_vig_probability_high": Decimal("0.51"),
        "consensus_book_count": 3,
        "model_run_id": "model-1",
        "model_version": "model-test",
        "feature_version": "features-test",
        "data_version": "data-test",
        "model_rule_version": "rules-test",
        "training_cutoff": datetime(2026, 8, 1, tzinfo=UTC),
        "model_probability": Decimal("0.60"),
        "push_probability": Decimal("0.03"),
        "uncertainty_margin": Decimal("0.02"),
        "model_market_spread_points": Decimal("-3"),
        "model_status": "test_only",
        "oos_decision_count": 250,
        "calibration_status": "acceptable",
        "clv_status": "stable",
        "research_complete": True,
        "market_match_confirmed": True,
        "material_information_complete": True,
        "information_quality": "full_official",
        "conflict_status": "clear",
        "exposure_status": "clear",
        "stability_status": "robust",
        "manual_approval_status": "approved",
        "source_notes": ("Official inputs reviewed.",),
    }
    values.update(changes)
    return CandidateInput(**values)


class EvaluationScoringTests(unittest.TestCase):
    def test_strong_candidate_is_ranked_but_remains_test_only(self):
        result = evaluate_candidate(candidate())
        self.assertEqual(result.decision_type, "test_only_selection")
        self.assertGreaterEqual(result.score_breakdown.total, 75)
        self.assertFalse(result.official_eligible)
        self.assertGreater(result.probability_edge, Decimal("0.05"))
        self.assertGreater(result.ev_per_dollar, Decimal("0"))
        payload = result.to_dict()
        self.assertEqual(payload["pick_strength_grade"], "A-")
        self.assertEqual(
            payload["pick_strength_display"],
            f"{result.score_breakdown.total}/100 (Grade A-)",
        )

    def test_missing_material_information_forces_pass(self):
        result = evaluate_candidate(
            candidate(
                material_information_complete=False,
                information_quality="missing_or_conflicting",
            )
        )
        self.assertEqual(result.decision_type, "pass")
        self.assertIn("material_information", result.pass_reason)

    def test_model_must_match_the_exact_spread(self):
        result = evaluate_candidate(
            candidate(model_market_spread_points=Decimal("-2.5"))
        )
        self.assertEqual(result.decision_type, "pass")
        self.assertFalse(result.gate_results["model_eligibility"])

    def test_five_point_edge_is_a_hard_minimum(self):
        price = Decimal("2")
        below = evaluate_candidate(
            candidate(
                decimal_odds=price,
                push_probability=Decimal("0"),
                model_probability=Decimal("0.5699"),
                uncertainty_margin=Decimal("0.02"),
            )
        )
        exact = evaluate_candidate(
            candidate(
                decimal_odds=price,
                push_probability=Decimal("0"),
                model_probability=Decimal("0.57"),
                uncertainty_margin=Decimal("0.02"),
            )
        )
        self.assertFalse(below.gate_results["required_edge"])
        self.assertTrue(exact.gate_results["required_edge"])
        self.assertEqual(exact.score_breakdown.edge, 12)

    def test_validation_score_follows_documented_sample_bands(self):
        edge = Decimal("0.08")
        ev = Decimal("0.12")
        self.assertEqual(score_candidate(candidate(oos_decision_count=0), edge, ev).model_validation, 0)
        self.assertEqual(score_candidate(candidate(oos_decision_count=99), edge, ev).model_validation, 10)
        self.assertEqual(score_candidate(candidate(oos_decision_count=100), edge, ev).model_validation, 15)
        self.assertEqual(score_candidate(candidate(oos_decision_count=250), edge, ev).model_validation, 20)
        self.assertEqual(score_candidate(candidate(oos_decision_count=500), edge, ev).model_validation, 25)

    def test_preseason_can_never_be_official(self):
        result = evaluate_candidate(candidate(season_type="preseason"))
        self.assertEqual(result.decision_type, "test_only_selection")
        self.assertFalse(result.official_eligible)
        self.assertTrue(any("Preseason" in reason for reason in result.loss_reasons))

    def test_best_straight_pick_uses_documented_ranking(self):
        lower = evaluate_candidate(candidate(game_id="game-1"))
        higher = evaluate_candidate(
            candidate(
                game_id="game-2",
                team_id="KC",
                opponent_team_id="DEN",
                model_run_id="model-2",
                odds_snapshot_id=2,
                model_probability=Decimal("0.64"),
            )
        )
        output = select_best_straight_pick((lower, higher))
        self.assertEqual(output["decision"], "test_only_selection")
        self.assertEqual(output["selection"]["game_id"], "game-2")

    def test_best_straight_pick_never_forces_a_selection(self):
        output = select_best_straight_pick(())
        self.assertEqual(output["decision"], "pass")
        self.assertIsNone(output["selection"])

    def test_three_leg_parlay_proposes_distinct_qualified_games_but_stays_pass(self):
        legs = (
            evaluate_candidate(candidate(game_id="game-1", team_id="BUF", opponent_team_id="MIA")),
            evaluate_candidate(candidate(game_id="game-2", team_id="KC", opponent_team_id="DEN", model_run_id="model-2", odds_snapshot_id=2)),
            evaluate_candidate(candidate(game_id="game-3", team_id="SF", opponent_team_id="SEA", model_run_id="model-3", odds_snapshot_id=3)),
        )
        output = build_best_three_leg_parlay(legs)
        self.assertEqual(output["decision"], "pass")
        self.assertEqual(len(output["proposed_legs"]), 3)
        self.assertTrue(all(leg["pick_strength_grade"] for leg in output["proposed_legs"]))
        self.assertEqual(
            len({leg["game_id"] for leg in output["proposed_legs"]}), 3
        )
        self.assertIsNotNone(output["minimum_acceptable_parlay_decimal_odds"])
        self.assertIsNone(output["actual_parlay_decimal_odds"])
        self.assertIn("actual sportsbook parlay price", output["pass_reason"])

    def test_three_leg_parlay_passes_when_three_independent_legs_are_unavailable(self):
        first = evaluate_candidate(candidate(game_id="game-1"))
        second = evaluate_candidate(
            candidate(
                game_id="game-2",
                team_id="KC",
                opponent_team_id="DEN",
                model_run_id="model-2",
                odds_snapshot_id=2,
            )
        )
        output = build_best_three_leg_parlay((first, second))
        self.assertEqual(output["decision"], "pass")
        self.assertEqual(output["proposed_legs"], [])
        self.assertIn("Fewer than three", output["pass_reason"])


class EvaluationEngineTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        initialize_schema(self.db)
        seed_reference_data(self.db)
        self.as_of = datetime(2026, 8, 14, 22, 10, tzinfo=UTC)
        self.prediction = self.as_of - timedelta(minutes=5)
        self.market_time = self.prediction - timedelta(minutes=5)
        kickoff = self.as_of + timedelta(hours=2)
        self.db.execute(
            """
            INSERT INTO games (
                game_id, season, season_type, week, home_team_id, away_team_id,
                kickoff_time, stadium_id, status, created_at, updated_at
            ) VALUES (?, 2026, 'preseason', 1, 'BUF', 'MIA', ?,
                      'highmark_stadium_2026', 'scheduled', ?, ?)
            """,
            ("game-1", kickoff.isoformat(), self.market_time.isoformat(), self.market_time.isoformat()),
        )
        for index, book in enumerate(("draftkings", "betmgm", "fanduel")):
            for team, opponent, spread, price in (
                ("BUF", "MIA", -3, 1.91 + (index * 0.01)),
                ("MIA", "BUF", 3, 1.91),
            ):
                self.db.execute(
                    """
                    INSERT INTO odds_snapshots (
                        game_id, sportsbook_key, market_key, period, settlement_rule,
                        outcome_team_id, opponent_team_id, spread_points, decimal_odds,
                        source_updated_at, collected_at, market_status,
                        is_accessible, is_consensus_eligible
                    ) VALUES ('game-1', ?, 'spreads', 'full_game',
                              'includes_overtime_push_returns_stake', ?, ?, ?, ?, ?, ?,
                              'open', 1, 1)
                    """,
                    (book, team, opponent, spread, price, self.market_time.isoformat(), self.market_time.isoformat()),
                )
        self.db.execute(
            """
            INSERT INTO model_runs (
                model_run_id, game_id, model_version, feature_version, data_version,
                rule_version, training_cutoff, prediction_time,
                home_cover_probability, away_cover_probability, push_probability,
                uncertainty_margin, market_spread_points, oos_decision_count,
                calibration_status, clv_status, status, created_at
            ) VALUES ('model-1', 'game-1', 'model-test', 'features-test', 'data-test',
                      'rules-test', ?, ?, 0.62, 0.35, 0.03, 0.02, -3, 250,
                      'acceptable', 'stable', 'test_only', ?)
            """,
            (
                (self.prediction - timedelta(days=1)).isoformat(),
                self.prediction.isoformat(),
                self.prediction.isoformat(),
            ),
        )

    def tearDown(self):
        self.db.close()

    def _context(self):
        self.db.execute(
            """
            INSERT INTO evaluation_contexts (
                evaluation_context_id, game_id, as_of, research_complete,
                market_match_confirmed, material_information_complete,
                information_quality, conflict_status, exposure_status,
                stability_status, manual_approval_status, source_notes_json, created_at
            ) VALUES ('context-1', 'game-1', ?, 1, 1, 1, 'full_official',
                      'clear', 'clear', 'robust', 'pending', ?, ?)
            """,
            (
                self.prediction.isoformat(),
                json.dumps(["Official injuries and playing-time roles reviewed."]),
                self.prediction.isoformat(),
            ),
        )

    def test_missing_reviewed_context_forces_pass(self):
        output = evaluate_upcoming(self.db, as_of=self.as_of, persist=False)
        self.assertEqual(output["test_only_selections"], [])
        self.assertEqual(output["passes"][0]["decision"], "pass")
        self.assertIn("research", output["passes"][0]["pass_reason"])

    def test_complete_context_produces_ranked_test_selection_and_audit_row(self):
        self._context()
        output = evaluate_upcoming(self.db, as_of=self.as_of, persist=True)
        self.assertEqual(len(output["test_only_selections"]), 1)
        selection = output["test_only_selections"][0]
        self.assertEqual(selection["team_id"], "BUF")
        self.assertEqual(selection["sportsbook"], "fanduel")
        self.assertEqual(selection["decision"], "test_only_selection")
        expected_median = remove_vig((Decimal("1.92"), Decimal("1.91")))[0]
        self.assertAlmostEqual(
            float(selection["no_vig_probability"]), float(expected_median), places=8
        )
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1)

    def test_consensus_only_books_can_supply_the_selected_price(self):
        self._context()
        self.db.execute(
            "UPDATE odds_snapshots SET is_accessible=0, is_consensus_eligible=1"
        )
        self.db.commit()
        output = evaluate_upcoming(self.db, as_of=self.as_of, persist=False)
        self.assertEqual(output["qualification_status"], "QUALIFIED")
        self.assertEqual(output["test_only_selections"][0]["sportsbook"], "fanduel")
        self.assertEqual(output["test_only_selections"][0]["qualification_status"], "QUALIFIED")

    def test_missing_model_returns_an_explicit_pass_without_fabrication(self):
        self.db.execute("DELETE FROM model_runs")
        output = evaluate_upcoming(self.db, as_of=self.as_of, persist=True)
        self.assertIn("No stored model run", output["passes"][0]["pass_reason"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

    def test_existing_database_receives_phase3_columns(self):
        columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(model_runs)")
        }
        self.assertTrue(
            {"market_spread_points", "oos_decision_count", "calibration_status", "clv_status"}.issubset(columns)
        )


if __name__ == "__main__":
    unittest.main()

