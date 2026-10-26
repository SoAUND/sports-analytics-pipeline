"""Export existing multi-sport evidence into Our revised publisher contract."""
from datetime import timedelta
import json
from .multisport_evaluation import current_market_views, _matching_views_for_candidate
from .multisport_research import live_readiness_report
from .multisport_runtime import approval_status, validation_count
from .sports_registry import get_sport_market
from .revised_strategy import timestamp
from .research_bridge import projection_evidence
from .deep_analysis import stored_three_way_probabilities


def execution_price_scope(book, accessible_books):
    """Only the exact configured primary-book jurisdiction is an execution quote."""
    primary_book_keys = {'hardrock', 'hardrockbet', 'hardrockbet_fl', 'hardrockbet_az', 'hardrockbet_oh'}
    return 'execution' if book in primary_book_keys and book in accessible_books else 'public_reference'


def export_candidates(db, settings, *, as_of):
    """No generated probability or invented source success; incomplete inputs pass."""
    events=db.execute('''SELECT e.*,h.full_name home_name,a.full_name away_name
        FROM multisport_events e JOIN multisport_participants h ON h.participant_id=e.home_participant_id
        JOIN multisport_participants a ON a.participant_id=e.away_participant_id
        WHERE e.status='scheduled' AND e.commence_time>? AND e.commence_time<=?''',
        (as_of.isoformat(),(as_of+timedelta(hours=24)).isoformat())).fetchall()
    out=[]
    for event in events:
        prediction=db.execute('SELECT * FROM multisport_predictions WHERE event_id=? AND prediction_time<=? ORDER BY prediction_time DESC LIMIT 1',
            (event['event_id'],as_of.isoformat())).fetchone()
        if prediction is None:continue
        probs=json.loads(prediction['probabilities_json']);sport=event['sport'];spec=get_sport_market(sport)
        ready=live_readiness_report(db,event_id=event['event_id'],sport=sport,as_of=as_of)
        evidence=[];seen=set();checks=[]
        for item in db.execute('SELECT * FROM multisport_research_checks WHERE event_id=? AND collected_at<=? ORDER BY collected_at DESC,research_check_id DESC',
                (event['event_id'],as_of.isoformat())):
            if item['category'] in seen:continue
            seen.add(item['category']);data=json.loads(item['evidence_json'] or '{}')
            checks.append({'status':item['verification_status'],'source_url':item['source_url'],
                'source_updated_at':item['source_updated_at'],'collected_at':item['collected_at'],'data':data})
            evidence.append({'category':item['category'],'status':item['verification_status'],'source_key':item['source_key'],
                'source_url':item['source_url'],'official':bool(item['official']),'source_updated_at':item['source_updated_at'],
                'collected_at':item['collected_at'],'material_change':data.get('material_change',False)})
        views=current_market_views(db,event_id=event['event_id'],sport=sport,as_of=as_of,stale_after_minutes=60)
        for view in views:
            if view.sportsbook_key not in set(settings.accessible_bookmakers)|set(settings.market_consensus_bookmakers):continue
            for selected in view.rows:
                role=selected['outcome_role']
                name='Draw' if role=='draw' else event['home_name'] if role=='home' else event['away_name']
                settlement=db.execute('SELECT * FROM multisport_settlement_rules WHERE sport=? AND sportsbook_key=? AND market_key=? AND period=? AND rule_status=\'verified\'',
                    (sport,view.sportsbook_key,spec.internal_market_key,spec.period)).fetchone()
                # Missing book rules are disclosed as assumptions by the publisher.
                rule=settlement['rule_key'] if settlement else ''
                item={'candidate_id':event['event_id']+':'+role+':'+view.sportsbook_key,'event_id':event['event_id'],
                      'sport':sport,'home_name':event['home_name'],'away_name':event['away_name'],
                      'start_time':event['commence_time'],'market':spec.internal_market_key,'period':spec.period,
                      'line':selected['points'],'participant':role,'selection_name':name,'settlement_rule':rule,
                      'decimal_odds':selected['decimal_odds'],'price_time':selected['source_updated_at'],
                      'market_status':selected['market_status'],'book_of_record':view.sportsbook_key,
                      'book_accessible':view.sportsbook_key in settings.accessible_bookmakers,
                      'accessible_books':list(settings.accessible_bookmakers),
                      'price_scope':execution_price_scope(view.sportsbook_key, settings.accessible_bookmakers),
                      'outcome_count':spec.outcome_count,'material_information_complete':ready.ready,'conflict_clear':not any('conflict' in b for b in ready.blockers),
                      'exposure_clear':False,'stability':'unknown','evidence':evidence,
                      'model':{'probability':probs.get(role),'push_probability':probs.get('push',0),'uncertainty':prediction['uncertainty_margin'],
                               'version':prediction['model_version'],'prediction_time':prediction['prediction_time'],
                               'uncertainty_method':'stored calibrated model uncertainty',
                               'approved':approval_status(db,sport,prediction['model_version'])=='approved',
                               'rejected':prediction['status']=='rejected','oos_count':validation_count(db,sport),'calibration':'unknown'}}
                # Copy a stored three-way model only. Never fill a missing home, draw, or away probability.
                if spec.allows_draw:
                    three_way = stored_three_way_probabilities(probs)
                    if three_way is not None:
                        item['model']['outcome_probabilities'] = three_way
                # Usage must be declared by the stored prediction, never inferred from a source page.
                components=probs.get('factor_components', {})
                if isinstance(components,dict) and isinstance(components.get(role),list):
                    item['model']['factor_components']=components[role]
                item.update(projection_evidence(checks,item,item['model'],as_of=as_of))
                # Carry exposure and sensitivity only when previously evaluated; do not assume them clear.
                decision=db.execute('SELECT * FROM multisport_paper_decisions WHERE event_id=? AND outcome_role=? AND sportsbook_key=?',
                                    (event['event_id'],role,view.sportsbook_key)).fetchone()
                if decision and decision['rule_version']=='sap-revised-2026-10-01':
                    gates=json.loads(decision['gate_results_json']);item['exposure_clear']=gates.get('exposure') is True
                    item['stability']='robust' if decision['probability_edge'] is not None and decision['probability_edge']>=.06 else 'unknown'
                if item['model']['approved']:item['model']['calibration']='acceptable'
                matches=_matching_views_for_candidate(views,sport=sport,role=role,points=selected['points'],consensus_books=set(settings.market_consensus_bookmakers)|{view.sportsbook_key})
                quotes=[]
                for matching in matches:
                    matching_rule=db.execute("SELECT rule_key FROM multisport_settlement_rules WHERE sport=? AND sportsbook_key=? AND market_key=? AND period=? AND rule_status='verified'",
                        (sport,matching.sportsbook_key,spec.internal_market_key,spec.period)).fetchone()
                    if rule and matching_rule is not None and matching_rule['rule_key'] != rule:
                        continue
                    assumed = not rule or matching_rule is None
                    rows=list(matching.rows);index=next((i for i,r in enumerate(rows) if r['outcome_role']==role),None)
                    if index is None:continue
                    quotes.append({**{k:item[k] for k in ('event_id','market','period','line','participant','settlement_rule')},
                        'book':matching.sportsbook_key,'settlement_assumed':assumed,'status':'open','source_updated_at':max(r['source_updated_at'] for r in rows),
                        'collected_at':max(r['collected_at'] for r in rows),'selected_index':index,
                        'outcomes':[{'name':'Draw' if r['outcome_role']=='draw' else event['home_name'] if r['outcome_role']=='home' else event['away_name'],
                                     'decimal_odds':r['decimal_odds']} for r in rows]})
                item['quotes']=quotes
                item['source_attempts']=[{'book':b,'result':'obtained' if any(q['book']==b for q in quotes) else 'unavailable or mismatched'}
                                         for b in settings.market_consensus_bookmakers]
                # All explicitly configured books requested together by the collector; unknown books are not invented.
                item['all_approved_sources_attempted']=bool(settings.market_consensus_bookmakers)
                out.append(item)
    return out
