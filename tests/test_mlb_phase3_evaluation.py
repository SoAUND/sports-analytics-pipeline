import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sports_analytics_pipeline.collectors.mlb_official import OfficialMlbPageCheck
from sports_analytics_pipeline.collectors.storage import CollectorRepository
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.mlb_evaluation import (
    MlbCandidateInput,
    MlbEvaluationEngine,
    MlbEvaluationPolicy,
    build_best_mlb_three_leg_parlay,
    evaluate_mlb_candidate,
    select_best_mlb_straight_pick,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 16, 16, 0, tzinfo=UTC)


class MLBPhase3EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)
        seed_reference_data(self.db, include_mlb=True)

    def tearDown(self):
        self.db.close()

    def candidate(self, **changes):
        data = dict(
            game_id="mlb_1",
            season_type="regular",
            team_id="mlb_nyy",
            opponent_team_id="mlb_bos",
            sportsbook_key="hardrockbet",
            odds_snapshot_id=1,
            decimal_odds=Decimal("2.10"),
            checked_at=NOW - timedelta(minutes=5),
            no_vig_probability=Decimal("0.48"),
            no_vig_probability_low=Decimal("0.47"),
            no_vig_probability_high=Decimal("0.49"),
            consensus_book_count=3,
            model_run_id="model-1",
            model_version="mlb-model-v1",
            feature_version="mlb-features-v1",
            data_version="mlb-data-v1",
            model_rule_version="mlb-model-rule-v1",
            training_cutoff=NOW - timedelta(days=1),
            model_probability=Decimal("0.62"),
            uncertainty_margin=Decimal("0.02"),
            model_status="test_only",
            oos_decision_count=25,
            calibration_status="untested",
            clv_status="unknown",
            research_complete=True,
            market_match_confirmed=True,
            material_information_complete=True,
            information_quality="full_official",
            conflict_status="clear",
            exposure_status="clear",
            stability_status="robust",
            manual_approval_status="approved",
            starting_pitchers_confirmed=True,
            lineups_confirmed=True,
            source_notes=("official MLB and approved analytics reviewed",),
        )
        data.update(changes)
        return MlbCandidateInput(**data)

    def test_qualifying_moneyline_is_test_only_never_official(self):
        result = evaluate_mlb_candidate(self.candidate())
        self.assertEqual(result.decision_type, "test_only_selection")
        self.assertFalse(result.official_eligible)
        self.assertGreaterEqual(result.probability_edge, Decimal("0.05"))
        self.assertGreater(result.ev_per_dollar, 0)
        payload = result.to_dict()
        self.assertEqual(payload["pick_strength_grade"], "A-")
        self.assertEqual(
            payload["pick_strength_display"],
            f"{result.score_breakdown.total}/100 (Grade A-)",
        )

    def test_two_to_five_point_edge_is_rejected_during_development(self):
        result = evaluate_mlb_candidate(self.candidate(model_probability=Decimal("0.54")))
        self.assertEqual(result.decision_type, "pass")
        self.assertFalse(result.gate_results["required_edge"])

    def test_five_point_edge_returns_after_100_oos(self):
        result = evaluate_mlb_candidate(self.candidate(
            model_probability=Decimal("0.54"),
            oos_decision_count=100,
            calibration_status="acceptable",
        ))
        self.assertEqual(result.decision_type, "pass")
        self.assertFalse(result.gate_results["required_edge"])

    def test_development_preserves_full_uncertainty(self):
        result = evaluate_mlb_candidate(self.candidate(
            model_probability=Decimal("0.70"),
            uncertainty_margin=Decimal("0.15"),
        ))
        self.assertEqual(result.decision_type, "test_only_selection")
        self.assertEqual(result.uncertainty_margin, Decimal("0.15"))
        self.assertEqual(result.conservative_probability, Decimal("0.55"))

    def test_minus_450_and_more_expensive_favorites_are_excluded(self):
        minus_450_decimal = Decimal("1") + (Decimal("100") / Decimal("450"))
        result = evaluate_mlb_candidate(self.candidate(
            decimal_odds=minus_450_decimal,
            model_probability=Decimal("0.95"),
        ))
        self.assertEqual(result.decision_type, "pass")
        self.assertFalse(result.gate_results["odds_cutoff"])

        minus_449_decimal = Decimal("1") + (Decimal("100") / Decimal("300"))
        allowed = evaluate_mlb_candidate(self.candidate(
            decimal_odds=minus_449_decimal,
            model_probability=Decimal("0.95"),
        ))
        self.assertEqual(allowed.decision_type, "test_only_selection")
        self.assertTrue(allowed.gate_results["odds_cutoff"])

    def test_unconfirmed_lineup_forces_pass(self):
        result = evaluate_mlb_candidate(self.candidate(lineups_confirmed=False))
        self.assertEqual(result.decision_type, "pass")
        self.assertFalse(result.gate_results["lineups"])

    def test_unconfirmed_starters_force_pass(self):
        result = evaluate_mlb_candidate(self.candidate(starting_pitchers_confirmed=False))
        self.assertEqual(result.decision_type, "pass")
        self.assertFalse(result.gate_results["starting_pitchers"])

    def test_best_straight_pick_does_not_force_selection(self):
        self.assertEqual(select_best_mlb_straight_pick([])["decision"], "pass")
        weak = evaluate_mlb_candidate(self.candidate(lineups_confirmed=False))
        self.assertEqual(select_best_mlb_straight_pick([weak])["decision"], "pass")

    def test_three_leg_parlay_is_only_a_screen_without_actual_quote(self):
        legs = []
        matchups = [
            ("g1", "mlb_nyy", "mlb_bos"),
            ("g2", "mlb_lad", "mlb_sf"),
            ("g3", "mlb_chc", "mlb_stl"),
        ]
        for i, (game, team, opp) in enumerate(matchups, 1):
            legs.append(evaluate_mlb_candidate(self.candidate(
                game_id=game, team_id=team, opponent_team_id=opp,
                odds_snapshot_id=i, model_run_id=f"m{i}"
            )))
        parlay = build_best_mlb_three_leg_parlay(legs)
        self.assertEqual(parlay["decision"], "pass")
        self.assertEqual(len(parlay["proposed_legs"]), 3)
        self.assertTrue(all(leg["pick_strength_grade"] for leg in parlay["proposed_legs"]))
        self.assertIsNone(parlay["actual_parlay_decimal_odds"])

    def test_engine_ignores_nfl_games(self):
        result = MlbEvaluationEngine(self.db, MlbEvaluationPolicy()).run(
            as_of=NOW, persist=False
        )
        self.assertEqual(result["games_found"], 0)

    def _insert_game_stack(self, *, confirmed=True, with_checks=True, with_context=True):
        now = NOW.isoformat()
        kickoff = (NOW + timedelta(hours=4)).isoformat()
        with self.db:
            self.db.execute(
                """INSERT INTO games (
                    game_id, source_game_id, sport, season, season_type, week,
                    home_team_id, away_team_id, kickoff_time, stadium_id, status,
                    created_at, updated_at
                ) VALUES ('mlb_777001','777001','mlb',2026,'regular',0,
                          'mlb_nyy','mlb_bos',?,'yankee_stadium','scheduled',?,?)""",
                (kickoff, now, now),
            )
            self.db.execute(
                """INSERT INTO players(player_id,sport,player_name,primary_team_id)
                   VALUES ('p1','mlb','Home Starter','mlb_nyy'),('p2','mlb','Away Starter','mlb_bos')"""
            )
            self.db.execute(
                """INSERT INTO mlb_game_context (
                    game_id, probable_home_pitcher_id, probable_away_pitcher_id,
                    confirmed_home_pitcher_id, confirmed_away_pitcher_id,
                    home_lineup_status, away_lineup_status, collected_at
                ) VALUES ('mlb_777001','p1','p2',?,?,?, ?, ?)""",
                (
                    "p1" if confirmed else None,
                    "p2" if confirmed else None,
                    "confirmed" if confirmed else "unconfirmed",
                    "confirmed" if confirmed else "unconfirmed",
                    now,
                ),
            )
            self.db.execute(
                """INSERT INTO model_runs (
                    model_run_id, game_id, sport, market_key, model_version,
                    feature_version, data_version, rule_version, training_cutoff,
                    prediction_time, home_cover_probability, away_cover_probability,
                    push_probability, uncertainty_margin, oos_decision_count,
                    calibration_status, clv_status, status, created_at
                ) VALUES ('mlb-model-run','mlb_777001','mlb','moneylines','m1','f1','d1','r1',
                          ?,?,0.62,0.38,0,0.02,25,'untested','unknown','test_only',?)""",
                ((NOW - timedelta(days=1)).isoformat(), now, now),
            )
            if with_context:
                self.db.execute(
                    """INSERT INTO evaluation_contexts (
                        evaluation_context_id, game_id, as_of, research_complete,
                        market_match_confirmed, material_information_complete,
                        information_quality, conflict_status, exposure_status,
                        stability_status, manual_approval_status, source_notes_json, created_at
                    ) VALUES ('ctx','mlb_777001',?,1,1,1,'full_official','clear','clear',
                              'robust','approved','[\"reviewed\"]',?)""",
                    (now, now),
                )
            # Three complete consensus/accessible markets, each exact two-sided same update.
            books = (("hardrockbet", 2.10, 1.80), ("book2", 2.05, 1.82), ("book3", 2.08, 1.81))
            for book, home_price, away_price in books:
                for team, opp, price in (
                    ("mlb_nyy", "mlb_bos", home_price),
                    ("mlb_bos", "mlb_nyy", away_price),
                ):
                    self.db.execute(
                        """INSERT INTO odds_snapshots (
                            game_id,sportsbook_key,market_key,period,settlement_rule,
                            outcome_team_id,opponent_team_id,spread_points,decimal_odds,
                            source_updated_at,collected_at,market_status,is_accessible,
                            is_consensus_eligible
                        ) VALUES ('mlb_777001',?,'moneylines','full_game','standard',?,?,NULL,?,?,?,
                                  'open',?,1)""",
                        (
                            book, team, opp, price,
                            (NOW - timedelta(minutes=5)).isoformat(),
                            (NOW - timedelta(minutes=4)).isoformat(),
                            1 if book == "hardrockbet" else 0,
                        ),
                    )
            if with_checks:
                for check in ("injuries", "lineups", "transactions", "probable_pitchers"):
                    source = f"mlb_{check}"
                    self.db.execute(
                        """INSERT INTO source_checks (
                            game_id,source_key,check_type,access_status,checked_at,source_url
                        ) VALUES ('mlb_777001',?,?, 'confirmed', ?, 'https://www.mlb.com/')""",
                        (source, check, (NOW - timedelta(minutes=10)).isoformat()),
                    )

    def test_engine_requires_model_and_reviewed_context(self):
        self._insert_game_stack(with_context=False)
        result = MlbEvaluationEngine(self.db, MlbEvaluationPolicy()).run(
            as_of=NOW, persist=False
        )
        self.assertEqual(result["best_straight_pick"]["decision"], "pass")
        self.assertTrue(result["passes"])

    def test_engine_requires_official_page_checks(self):
        self._insert_game_stack(with_checks=False)
        result = MlbEvaluationEngine(self.db, MlbEvaluationPolicy()).run(
            as_of=NOW, persist=False
        )
        self.assertEqual(result["best_straight_pick"]["decision"], "pass")

    def test_engine_can_rank_complete_test_only_candidate(self):
        self._insert_game_stack()
        result = MlbEvaluationEngine(self.db, MlbEvaluationPolicy()).run(
            as_of=NOW, persist=False
        )
        self.assertEqual(result["games_found"], 1)
        self.assertEqual(result["best_straight_pick"]["decision"], "test_only_selection")
        self.assertEqual(result["official_picks"], [])

    def test_consensus_only_books_can_supply_the_selected_moneyline(self):
        self._insert_game_stack()
        self.db.execute(
            "UPDATE odds_snapshots SET is_accessible=0, is_consensus_eligible=1 "
            "WHERE game_id='mlb_777001'"
        )
        self.db.commit()
        result = MlbEvaluationEngine(self.db, MlbEvaluationPolicy()).run(
            as_of=NOW, persist=False
        )
        self.assertEqual(result["qualification_status"], "QUALIFIED")
        self.assertEqual(result["best_straight_pick"]["decision"], "test_only_selection")
        self.assertEqual(result["test_only_selections"][0]["qualification_status"], "QUALIFIED")

    def test_mlb_official_check_persistence_uses_mlb_source_key(self):
        self._insert_game_stack(with_checks=False)
        check = OfficialMlbPageCheck(
            check_type="lineups",
            source_key="mlb_lineups",
            source_url="https://www.mlb.com/starting-lineups",
            checked_at=NOW,
            http_status=200,
            content_sha256="abc",
            access_status="confirmed",
        )
        stored = CollectorRepository(self.db).persist_mlb_official_check(
            check, game_ids=("mlb_777001",)
        )
        self.assertEqual(stored, 1)
        row = self.db.execute(
            "SELECT source_key,check_type FROM source_checks ORDER BY source_check_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual((row["source_key"], row["check_type"]), ("mlb_lineups", "lineups"))


if __name__ == "__main__":
    unittest.main()

