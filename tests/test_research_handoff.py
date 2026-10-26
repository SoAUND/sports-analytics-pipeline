import copy
import sqlite3
import unittest

from test_experimental_calls import NOW, event
from sports_analytics_pipeline.experimental_calls import market_calls
from sports_analytics_pipeline.revised_strategy import evaluate_research, optional_settlement_review
from sports_analytics_pipeline.value_models import matching_quotes
from sports_analytics_pipeline.full_evaluation import evaluation_lines
from sports_analytics_pipeline.research_cadence import display_risk
from sports_analytics_pipeline.public_research import attach_public_research, public_research_sport, PublicResearchCollector
from test_public_research import Transport, spec


class ResearchHandoffTests(unittest.TestCase):
    def test_free_source_collection_continues_after_old_daily_limit(self):
        db=sqlite3.connect(':memory:');transport=Transport()
        collector=PublicResearchCollector(db,transport,sources=[spec()])
        db.executemany('INSERT INTO public_source_requests VALUES (?,?,?)',
                       [(NOW.date().isoformat(),'https://example.com/old',NOW.isoformat())]*300)
        db.commit()
        report=collector.collect(sports=['nhl'],as_of=NOW)
        self.assertEqual(len(transport.calls),1)
        self.assertEqual(report['source_attempts'][0]['status'],'retrieved')

    def two_books(self):
        item=event()
        other=copy.deepcopy(item['bookmakers'][0]);other['key']='fanduel'
        item['bookmakers'].append(other)
        return item

    def test_complete_prices_survive_discovery_and_missing_model(self):
        original=self.two_books();before=copy.deepcopy(original)
        row=market_calls([original],as_of=NOW)[0]
        self.assertEqual(row['price_scope'],'public_reference')
        result=evaluate_research(row,as_of=NOW)
        self.assertEqual(result['full_evaluation']['qualifications']['market_match']['status'],'passed')
        quotes,rejected=matching_quotes(result,as_of=NOW)
        self.assertEqual(len(quotes),2);self.assertEqual(rejected,[])
        self.assertTrue(result['settlement_assumed'])
        self.assertNotIn('model_probability',result)
        self.assertNotIn('ev_per_dollar',result)
        self.assertEqual(original,before)

    def test_execution_scope_requires_configured_book(self):
        row=market_calls([self.two_books()],as_of=NOW,accessible_books=('hardrockbet_fl',))[0]
        self.assertEqual(row['price_scope'],'verified_execution')
        self.assertEqual(row['book_of_record'],'hardrockbet_fl')

    def test_soccer_preserves_draw_and_selected_index(self):
        item=self.two_books();item['sport_key']='soccer_epl'
        for book in item['bookmakers']:
            book['markets'][0]['outcomes'].append({'name':'Draw','price':4})
        row=optional_settlement_review(market_calls([item],as_of=NOW)[0])
        self.assertEqual(row['outcome_count'],3)
        quotes,rejected=matching_quotes(row,as_of=NOW)
        self.assertEqual(len(quotes),2);self.assertEqual(rejected,[])
        self.assertEqual(quotes[0]['quote']['outcomes'][2]['name'],'Draw')

    def test_mismatched_quote_still_fails(self):
        row=market_calls([self.two_books()],as_of=NOW)[0]
        for q in row['quotes']:q['event_id']='wrong-event'
        result=evaluate_research(row,as_of=NOW)
        self.assertEqual(result['full_evaluation']['qualifications']['market_match']['status'],'failed')

    def test_unknown_soccer_retains_competition_and_context_attempts(self):
        row={'sport':'soccer_argentina_primera_division','home_name':'Talleres','away_name':'Belgrano'}
        report={'source_attempts':[{'source_key':'news','sports':['epl'],'status':'http_403'}],'records':[]}
        out=attach_public_research([row],report,as_of=NOW)[0]
        self.assertEqual(public_research_sport(row['sport']),'epl')
        self.assertEqual(out['sport'],row['sport'])
        self.assertEqual(out['public_source_status_counts'],{'http_403':1})
        self.assertEqual(len(out['public_source_attempts']),1)
        self.assertNotIn('evidence',out)

    def test_report_keeps_all_checks_without_repeating_research_reason(self):
        row=market_calls([self.two_books()],as_of=NOW)[0]
        result=evaluate_research(row,as_of=NOW)
        card='\n'.join(evaluation_lines(result))
        self.assertEqual(card.count('Missing/invalid research:'),1)
        self.assertIn('Qualifications checked: 12/12',card)
        self.assertIn('Checked means attempted',card)
        self.assertTrue(display_risk(result))


if __name__=='__main__':unittest.main()
