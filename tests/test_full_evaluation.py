import copy
import json
import sqlite3
import unittest

from test_revised_strategy import NOW, evidence_candidate
from sports_analytics_pipeline.revised_strategy import evaluate_research, GATES
from sports_analytics_pipeline.full_evaluation import evaluation_lines
from sports_analytics_pipeline.deep_analysis import audit_candidate
from sports_analytics_pipeline.research_cadence import delivery_cards, prepare_manual_job, run_due, pending_messages


class FullEvaluationTests(unittest.TestCase):
    def test_missing_model_does_not_stop_later_independent_checks(self):
        row=evidence_candidate();row.pop('model')
        result=evaluate_research(row,as_of=NOW);audit=result['full_evaluation']
        self.assertEqual(len(audit['stages']),7)
        self.assertEqual(list(audit['qualifications']),list(GATES))
        self.assertTrue(all(v['attempted'] for v in audit['qualifications'].values()))
        self.assertEqual(audit['qualifications']['current_price']['status'],'passed')
        self.assertEqual(audit['qualifications']['market_match']['status'],'passed')
        self.assertEqual(audit['qualifications']['exposure']['status'],'passed')
        self.assertEqual(audit['qualifications']['conservative_probability']['status'],'unable_to_evaluate')
        self.assertEqual(audit['qualifications']['positive_ev']['status'],'unable_to_evaluate')
        self.assertNotIn('ev_per_dollar', result)
        self.assertIn(result['publication_decision'], {'official_pick', 'conditional_pick', 'watchlist'})

    def test_research_failure_still_calculates_value_and_checks_all_gates(self):
        row=evidence_candidate();row['advanced_metrics']={}
        result=evaluate_research(row,as_of=NOW);audit=result['full_evaluation']
        self.assertNotEqual(audit['qualifications']['research']['status'],'passed')
        self.assertEqual(audit['qualifications']['positive_ev']['status'],'passed')
        self.assertEqual(audit['qualifications']['exposure']['status'],'passed')
        self.assertEqual(audit['stages'][6]['status'],'failed')
        self.assertIn(result['publication_decision'], {'official_pick', 'conditional_pick', 'watchlist'})

    def test_bad_price_does_not_block_exposure_conflict_or_model_checks(self):
        row=evidence_candidate();row['decimal_odds']='bad';row['exposure_clear']=False;row['conflict_clear']=False
        result=evaluate_research(row,as_of=NOW);q=result['full_evaluation']['qualifications']
        self.assertEqual(q['current_price']['status'],'unable_to_evaluate')
        self.assertEqual(q['exposure']['status'],'failed')
        self.assertEqual(q['conflict_integrity']['status'],'failed')
        self.assertEqual(q['model_eligibility']['status'],'failed')

    def test_soccer_probability_gaps_do_not_stop_independent_checks(self):
        row=evidence_candidate();row['sport']='epl';row['market']='three_way_moneyline'
        result=evaluate_research(row,as_of=NOW);q=result['full_evaluation']['qualifications']
        self.assertEqual(q['conservative_probability']['status'],'unable_to_evaluate')
        self.assertEqual(q['exposure']['status'],'passed')
        self.assertEqual(q['manual_approval']['status'],'unable_to_evaluate')

    def test_all_failures_and_unavailable_checks_are_shown(self):
        row=evidence_candidate();row.pop('model');row['exposure_clear']=False;row['conflict_clear']=False
        result=evaluate_research(row,as_of=NOW);result['publication_decision']='watchlist'
        card=delivery_cards([result],title='CHECK',as_of=NOW,include_details=False)[0]
        self.assertIn('Stages checked: 7/7 | Qualifications checked: 12/12',card)
        self.assertIn('Exposure limits: exposure_clear explicitly false',card)
        self.assertIn('Conflict/sensitivity integrity: conflict_clear explicitly false',card)
        self.assertIn('Positive expected value: Depends on usable conservative probability',card)
        self.assertIn('Stage 5',card)
        self.assertNotIn('Stage 3 watchlist',card)

    def test_legacy_stage_audit_retains_all_gaps_after_first_failure(self):
        row={'sport':'nhl','market':'moneyline'}
        audit=audit_candidate(row)
        self.assertEqual(audit.level,1)
        self.assertIn('expected_goals',audit.missing_metrics)
        self.assertIn('official_status',audit.missing_evidence)
        self.assertIn('conservative_probability',audit.missing_output)

    def test_provisional_calls_are_fully_evaluated_and_persisted(self):
        row=evidence_candidate();row.pop('model');row['publication_decision']='watchlist';original=copy.deepcopy(row)
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        prepare_manual_job(db,NOW)
        run_due(db,NOW,{'candidates':[],'experimental_calls':[row]})
        audit=json.loads(db.execute('SELECT decision_json FROM research_audit').fetchone()[0])
        self.assertEqual(audit['full_evaluation']['qualifications_attempted'],12)
        self.assertEqual(audit['publication_decision'],'watchlist')
        self.assertIn('Stages checked: 7/7',pending_messages(db)[0]['message'])
        self.assertEqual(row,original)

    def test_rejected_model_never_gets_promoted_by_diagnostics(self):
        row=evidence_candidate();row['model']['rejected']=True
        result=evaluate_research(row,as_of=NOW)
        self.assertEqual(result['publication_decision'],'pass')
        self.assertEqual(result['full_evaluation']['qualifications']['model_eligibility']['status'],'failed')
        self.assertEqual(result['full_evaluation']['qualifications']['exposure']['status'],'passed')

    def test_missing_identity_does_not_hide_remaining_failures(self):
        row={'exposure_clear':False,'manual_wager_approved':False}
        result=evaluate_research(row,as_of=NOW)
        self.assertEqual(result['full_evaluation']['qualifications_attempted'],12)
        self.assertIn('exposure',result['full_evaluation']['failed_qualifications'])
        self.assertIn('manual_approval',result['full_evaluation']['failed_qualifications'])

    def test_integer_line_cannot_assume_zero_push_probability(self):
        row=evidence_candidate();row['line']=-2
        result=evaluate_research(row,as_of=NOW)
        self.assertEqual(result['full_evaluation']['qualifications']['positive_ev']['status'],'unable_to_evaluate')
        self.assertEqual(result['full_evaluation']['qualifications']['exposure']['status'],'passed')


if __name__=='__main__': unittest.main()
