import copy
import sqlite3
import unittest
from datetime import timedelta
from test_revised_strategy import NOW, evidence_candidate
from sports_analytics_pipeline.revised_strategy import evaluate_research
from sports_analytics_pipeline.parlay_builder import build_parlays, parlay_report
from sports_analytics_pipeline.research_cadence import prepare_manual_job, run_due, pending_messages


def candidates(count=4, prop=False):
    rows=[]
    for i in range(count):
        row=evidence_candidate()
        row.update(candidate_id=f'c{i}', event_id=f'e{i}', home_name=f'Team {i}',selection_name=f'Team {i}')
        for item in row['evidence']:
            item['event_id'] = row['event_id']
        row['model'].update(probability=.8,approved=True,oos_count=500,calibration='acceptable',stable=True)
        if prop:
            from sports_analytics_pipeline.deep_analysis import required_metrics
            row.update(market='player_prop',prop_stat='points',direction='over',line=18.5,role_confirmed=True,
                       trend={'sample_size':8,'same_competition':True,'season_span':2,'source_url':'https://example.test/trend'})
            row['competition']='nba'
            row['role']='starter'
            row['game_logs']=[{'game_id':f'g{n}','player':row['selection_name'],'role':'starter','stat':'points','competition':'nba','date':NOW.isoformat(),'actual':22,'status':'completed','comparable':True,'source_url':'https://example.test/log'} for n in range(5)]
        for q in row['quotes']:
            q.update(event_id=row['event_id'],market=row['market'],line=row['line'])
            q['outcomes'][0]['name']=row['selection_name']
            if prop:
                q.update(prop_stat='points', direction='over')
                q['outcomes'][0]['direction']='over'
                q['outcomes'][1]['direction']='under'
        rows.append(row)
    return rows


def quote(legs,odds=4,probability=.6):
    return {'legs':legs,'decimal_odds':odds,'joint_probability':probability,
            'correlation_method':'independent_verified','correlation_time':NOW.isoformat(),
            'correlation_source_url':'https://example.test/dependency','correlation_evidence':'Independent event dependency review',
            'story':'Stable opportunities in unrelated games; each leg qualified',
            'book_of_record':'hardrockbet','source_url':'https://example.test/slip',
            'quote_status':'confirmed','price_time':NOW.isoformat()}


class ParlayBuilderTests(unittest.TestCase):
    def evaluated(self,count=4,prop=False):
        return [evaluate_research(r,as_of=NOW) for r in candidates(count,prop)]

    def test_searches_all_combinations_without_inventing_price(self):
        rows=self.evaluated()
        result=build_parlays(rows,as_of=NOW)
        self.assertEqual(result['eligible_legs'],4)
        self.assertEqual(result['combinations_checked'],7)
        self.assertEqual(result['verified'],[])
        self.assertEqual(len(result['reviews']),2)
        self.assertIn('2-LEG TEAM MARKETS',parlay_report(result))
        self.assertNotIn('Combined +',parlay_report(result))

    def test_verified_two_and_four_legs_and_separate_plus(self):
        rows=self.evaluated()
        result=build_parlays(rows,as_of=NOW,ticket_quotes=[quote(rows[:2]),quote(rows,16,.3)])
        self.assertEqual(sorted(t['leg_count'] for t in result['verified']),[2,4])
        self.assertTrue(all(t['decision']=='official_pick' for t in result['verified']))
        text=parlay_report(result)
        self.assertIn('Combined +300',text)
        self.assertIn('4-LEG TEAM MARKETS',text)
        self.assertIn('SEPARATE OVERALL-PLUS-MONEY',text)
        self.assertIn('ET',text)

    def test_props_have_separate_two_and_four_sections(self):
        rows=self.evaluated(prop=True)
        self.assertTrue(all(r['publication_decision']=='official_pick' for r in rows))
        result=build_parlays(rows,as_of=NOW,ticket_quotes=[quote(rows[:2]),quote(rows,16,.3)])
        self.assertEqual(len(result['verified']),2)
        self.assertTrue(all(t['kind']=='props' for t in result['verified']))

    def test_quote_missing_stale_mismatched_dependency_or_impossible_joint_blocks(self):
        rows=self.evaluated(2)
        for field,value in [('source_url',''),('book_of_record','draftkings'),('quote_status','estimated'),
                            ('price_time',(NOW-timedelta(hours=2)).isoformat()),('correlation_evidence',''),
                            ('correlation_time',(NOW-timedelta(hours=2)).isoformat()),('joint_probability',.99)]:
            q=quote(rows);q[field]=value
            with self.subTest(field=field):
                self.assertEqual(build_parlays(rows,as_of=NOW,ticket_quotes=[q])['verified'],[])

    def test_started_public_same_game_and_cross_book_do_not_create_ticket(self):
        rows=self.evaluated(2)
        for field,value in [('start_time',NOW.isoformat())]:
            changed=copy.deepcopy(rows);changed[0][field]=value
            self.assertEqual(build_parlays(changed,as_of=NOW)['combinations_checked'],0)
        public=copy.deepcopy(rows);public[0]['price_scope']='public_reference'
        self.assertGreaterEqual(build_parlays(public,as_of=NOW)['combinations_checked'],1)
        rows[1]['event_id']=rows[0]['event_id']
        self.assertEqual(build_parlays(rows,as_of=NOW)['combinations_checked'],0)
        rows=self.evaluated(2);rows[1]['book_of_record']='draftkings'
        self.assertEqual(build_parlays(rows,as_of=NOW)['combinations_checked'],0)

    def test_runtime_builds_ticket_from_fresh_evaluated_legs_not_old_decisions(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        prepare_manual_job(db,NOW)
        raw=candidates(2)
        run_due(db,NOW,{'candidates':raw,'parlay_tickets':[quote(raw)]})
        messages=pending_messages(db)
        self.assertEqual(len(messages),1)
        self.assertIn('Combined +300',messages[0]['message'])
        self.assertEqual(db.execute('SELECT count(*) FROM research_parlay_audit').fetchone()[0],1)

    def test_watchlist_never_spams_individual_moneylines(self):
        from sports_analytics_pipeline.research_cadence import delivery_cards
        rows=[{**r,'publication_decision':'watchlist','research_tier':'watchlist'} for r in self.evaluated(35)]
        cards=delivery_cards(rows,title='test',as_of=NOW,include_details=False)
        self.assertEqual(len(cards),1)
        self.assertIn('BEST OF THE DAY',cards[0])
        self.assertLessEqual(cards[0].count('Strength: '),3)
        self.assertIn('35 research-only entries',cards[0])
