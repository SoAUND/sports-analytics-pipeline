"""Non-short-circuit diagnostic evaluation. Missing dependencies are not passes.

Publication eligibility remains owned by revised_strategy. This audit executes
every independent check and records all seven stages, even for rejected rows.
"""
from datetime import timedelta
from decimal import Decimal

from .deep_analysis import STRATEGY_STAGES, advanced_metric_gaps, soccer_outcome_gaps


GATE_LABELS = {
    'research': 'Research completeness', 'current_price': 'Current price',
    'market_match': 'Exact market/book match', 'material_information': 'Material information',
    'model_eligibility': 'Model validation', 'conservative_probability': 'Conservative probability',
    'required_edge': 'Positive edge', 'positive_ev': 'Positive expected value',
    'acceptable_price': 'Minimum acceptable price', 'conflict_integrity': 'Conflict/sensitivity integrity',
    'exposure': 'Exposure limits', 'manual_approval': 'Manual execution approval',
}


def full_evaluation(row, *, as_of):
    # Imported lazily to keep the evaluator's public API and avoid import cycles.
    from .revised_strategy import GATES, SPORTS, MARKETS, timestamp, decimal
    from .value_models import matching_quotes

    checks = {}
    values = {}
    context = {}

    def record(key, status, reason):
        checks[key] = {'status': status, 'reason': reason, 'attempted': True}

    def test(key, fn):
        try:
            status, reason = fn()
            record(key, status, reason)
        except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError) as exc:
            record(key, 'unable_to_evaluate', 'Missing or unusable input: '+str(exc))

    def flag(key, field):
        value = row.get(field)
        if value is True:
            return 'passed', field+' verified'
        if value is False:
            return 'failed', field+' explicitly false'
        return 'unable_to_evaluate', field+' not supplied or not verified'

    def research():
        sport = str(row.get('sport', '')).lower()
        required = ['official_status', 'injury_availability', 'lineup_or_participants',
                    'rest_travel', 'statistics_analytics', 'transactions_or_changes',
                    'multi_book_prices', 'line_movement']
        if sport in {'nfl','cfb','mlb','epl','atp','wta'}:
            required.append('weather_environment')
        if sport == 'mma': required.append('weigh_in_status')
        limits = {'official_status':180, 'injury_availability':90 if sport in {'nba','wnba'} else 360 if sport=='mma' else 180,
                  'lineup_or_participants':90 if sport in {'nba','wnba','epl'} else 180,
                  'rest_travel':720, 'statistics_analytics':1440, 'transactions_or_changes':360 if sport=='mma' else 720,
                  'weather_environment':180, 'weigh_in_status':360, 'multi_book_prices':60, 'line_movement':60}
        verified = {}
        rejected = []
        for item in row.get('evidence', []) or []:
            try:
                cat = item.get('category')
                if cat not in limits: continue
                updated, collected = timestamp(item['source_updated_at']), timestamp(item['collected_at'])
                valid = (item.get('status')=='verified' and item.get('source_url') and item.get('source_key')
                         and as_of-timedelta(minutes=limits[cat]) <= updated <= collected <= as_of
                         and (cat not in {'official_status','lineup_or_participants','weigh_in_status'} or item.get('official') is True))
                if valid: verified[cat]=item
                else: rejected.append(cat)
            except (KeyError, ValueError, TypeError, AttributeError):
                rejected.append(str(item.get('category', 'malformed evidence')) if isinstance(item, dict) else 'malformed evidence')
        context['verified'] = verified
        missing = [cat for cat in required if cat not in verified]
        metrics = advanced_metric_gaps(row)
        factors = row.get('factor_analysis') or {}
        modeled = set((row.get('model') or {}).get('factor_components', []))
        for name, item in factors.items():
            if item.get('model_component') not in modeled: metrics.append(name+'_not_in_projection')
            try:
                if not as_of-timedelta(hours=24) <= timestamp(item['source_updated_at']) <= as_of: metrics.append(name+'_stale')
            except (KeyError, TypeError, ValueError): metrics.append(name+'_timestamp')
        context['missing_evidence'], context['missing_metrics'] = missing, metrics
        issues = missing+metrics
        if len({v['source_key'] for v in verified.values()})<3: issues.append('fewer than 3 verified source keys')
        if row.get('market')=='player_prop':
            trend=row.get('trend') or {}
            if not (int(trend.get('sample_size',0))>=8 and trend.get('same_competition') is True
                    and 0<int(trend.get('season_span',0))<=3 and trend.get('source_url')):
                issues.append('8 comparable observations within 3 seasons required')
        if str(row.get('sport', '')).lower() not in SPORTS: issues.append('unsupported sport')
        if issues:
            return ('failed' if rejected else 'unable_to_evaluate'), 'Missing/invalid research: '+', '.join(dict.fromkeys(issues))
        return 'passed', 'Required current evidence, metrics, factors and source coverage verified'

    def price():
        odds = decimal(row['decimal_odds']); quoted = timestamp(row['price_time']); start=timestamp(row['start_time'])
        values['break_even_probability'] = str(1/odds) if odds>1 else None
        issues=[]
        if not Decimal(1)+Decimal(100)/300 <= odds <= 11: issues.append('price outside configured bounds')
        if not as_of-timedelta(minutes=60)<=quoted<=as_of: issues.append('quote stale or future-dated')
        if row.get('market_status')!='open': issues.append('market not open')
        if as_of>=start: issues.append('pregame window closed')
        return ('failed', '; '.join(issues)) if issues else ('passed', 'Fresh open pregame quote within configured bounds')

    def market():
        identity=['event_id','home_name','away_name','period','selection_name','settlement_rule']
        missing=[k for k in identity if not row.get(k)]
        if row.get('market') not in MARKETS: missing.append('supported market')
        if row.get('market') in {'spreads','totals','player_prop'} and row.get('line') is None: missing.append('exact line')
        if row.get('market')=='player_prop' and (not row.get('prop_stat') or row.get('direction') not in {'over','under'}): missing.append('prop statistic/direction')
        if missing: return 'unable_to_evaluate', 'Missing market identity: '+', '.join(missing)
        quotes, rejected = matching_quotes(row, as_of=as_of)
        books={q['book'] for q in quotes}
        values['matching_book_count']=len(books)
        context['quotes']=quotes
        if not any(q['book']==row.get('book_of_record') and decimal(q['decimal_odds'])==decimal(row['decimal_odds']) for q in quotes):
            return 'failed' if row.get('quotes') else 'unable_to_evaluate', 'Exact book-of-record quote missing or mismatched'
        if len(books)<2: return 'unable_to_evaluate', 'Fewer than 2 fresh exact matching books'
        if row.get('book_accessible') is not True and row.get('price_scope')!='public_reference':
            return 'unable_to_evaluate', 'Book accessibility unverified'
        return 'passed', str(len(books))+' exact matching books'+(' (public reference; execution unverified)' if row.get('price_scope')=='public_reference' else '')

    def material():
        status,reason=flag('material_information','material_information_complete')
        if status!='passed': return status,reason
        if row.get('market')=='player_prop' and row.get('role_confirmed') is not True:
            return 'unable_to_evaluate', 'Upcoming player role unconfirmed'
        if row.get('market') in {'spreads','totals','player_prop'} and row.get('line') is not None:
            line=decimal(row['line'])
            if line==line.to_integral_value() and 'push_probability' not in (row.get('model') or {}):
                return 'unable_to_evaluate', 'Whole-number line requires explicit push probability'
        verified=context.get('verified',{})
        lineup=verified.get('lineup_or_participants')
        if lineup:
            for cat in ('injury_availability','transactions_or_changes','official_status'):
                news=verified.get(cat)
                if news and news.get('material_change') and timestamp(news['source_updated_at'])>timestamp(lineup['source_updated_at']):
                    return 'failed', 'Material news newer than lineup: '+cat+'; downstream recheck required'
        return 'passed', reason

    def eligibility():
        model=row.get('model')
        if not model: return 'unable_to_evaluate', 'No independent model supplied'
        issues=[]
        if model.get('rejected') is True: issues.append('model rejected')
        if model.get('approved') is not True: issues.append('model not approved')
        if int(model.get('oos_count',0))<100: issues.append('fewer than 100 held-out observations')
        if model.get('calibration')!='acceptable': issues.append('calibration not acceptable')
        return ('failed','; '.join(issues)) if issues else ('passed','Approved model with acceptable held-out calibration')

    def projection():
        model=row.get('model')
        distribution=row.get('prop_distribution') or {}
        if distribution.get('status')=='blocked': return 'unable_to_evaluate','Prop distribution blocked: '+str(distribution.get('reason'))
        if not model: return 'unable_to_evaluate', 'No usable independent probability model; market consensus is not a model'
        if model.get('rejected') is True: return 'failed', 'Rejected model cannot supply a usable projection'
        if row.get('market') in {'spreads','totals','player_prop'} and row.get('line') is not None:
            line=decimal(row['line'])
            if line==line.to_integral_value() and 'push_probability' not in model:
                return 'unable_to_evaluate', 'Whole-number line requires explicit push probability; zero is not assumed'
        gaps=soccer_outcome_gaps(row)
        if gaps: return 'unable_to_evaluate','Soccer home/draw/away model incomplete: '+', '.join(gaps)
        w,u,push=decimal(model['probability']),decimal(model['uncertainty']),decimal(model.get('push_probability',0))
        if not (0<=w<=1 and 0<=u<=1 and 0<=push<1 and w+push<=1): return 'failed','Invalid model probabilities'
        cutoff=timestamp(model['prediction_time']); quoted=timestamp(row['price_time'])
        if not model.get('version') or not model.get('uncertainty_method'): return 'unable_to_evaluate','Model version/uncertainty method missing'
        if not quoted<=cutoff<=as_of or any(timestamp(v['collected_at'])>cutoff for v in context.get('verified',{}).values()):
            return 'failed','Prediction timestamp does not incorporate latest quote/evidence'
        factors=row.get('factor_analysis') or {}
        effects=[decimal(v.get('uncertainty_effect',0)) for v in factors.values()]
        if any(x<0 for x in effects): return 'failed','Negative factor uncertainty'
        p=max(Decimal(0), w/(1-push)-max(u,sum(effects,Decimal(0))))
        context['p'],context['push']=p,push
        values.update(model_probability=str(w),conservative_probability=str(p),push_probability=str(push))
        return 'passed','Independent probability adjusted for pushes and uncertainty'

    def value(key):
        if 'p' not in context: return 'unable_to_evaluate', 'Depends on usable conservative probability: '+checks['conservative_probability']['reason']
        odds=decimal(row['decimal_odds'])
        if odds<=1: return 'failed','Invalid decimal odds'
        p=context['p'];edge=p-1/odds;ev=(1-context['push'])*(p*odds-1)
        minimum=1/p if p else Decimal('Infinity')
        values.update(probability_edge=str(edge),ev_per_dollar=str(ev),minimum_decimal_odds=str(minimum))
        ok={'required_edge':edge>0,'positive_ev':ev>0,'acceptable_price':odds>minimum}[key]
        reason={'required_edge':'Conservative edge: '+str(edge),'positive_ev':'Conservative EV per dollar: '+str(ev),
                'acceptable_price':'Offered decimal '+str(odds)+'; must exceed '+str(minimum)}[key]
        return ('passed' if ok else 'failed'),reason

    def conflict():
        if row.get('stability') in {'fragile','unstable'}: return 'failed','Fails reasonable sensitivity changes'
        return flag('conflict_integrity','conflict_clear')

    tests={'research':research,'current_price':price,'market_match':market,'material_information':material,
           'model_eligibility':eligibility,'conservative_probability':projection,
           'required_edge':lambda:value('required_edge'),'positive_ev':lambda:value('positive_ev'),
           'acceptable_price':lambda:value('acceptable_price'),'conflict_integrity':conflict,
           'exposure':lambda:flag('exposure','exposure_clear'),
           'manual_approval':lambda:flag('manual_approval','manual_wager_approved')}
    for key in GATES: test(key,tests[key])

    def stage(n,status,reason):
        return {'stage':n,'name':STRATEGY_STAGES[n-1],'attempted':True,'status':status,'reason':reason}
    def from_gates(n,keys):
        status='failed' if any(checks[k]['status']=='failed' for k in keys) else 'unable_to_evaluate' if any(checks[k]['status']=='unable_to_evaluate' for k in keys) else 'passed'
        reason='; '.join(GATE_LABELS[k]+': '+checks[k]['reason'] for k in keys if checks[k]['status']!='passed') or 'Required checks passed'
        return stage(n,status,reason)
    identity=bool(row.get('event_id') and row.get('market') and row.get('selection_name'))
    stages=[stage(1,'passed' if identity else 'unable_to_evaluate','Candidate event/market/selection identified; full-slate coverage is a separate run audit' if identity else 'Candidate identity incomplete'),
            from_gates(2,['current_price','material_information']),
            from_gates(3,['research']),from_gates(4,['conservative_probability'])]
    comparison=row.get('market_comparison')
    comparison_ok=isinstance(comparison,dict) and comparison.get('status')=='complete' and bool(comparison.get('source_url') or comparison.get('evidence'))
    stages.append(stage(5,'passed' if comparison_ok and checks['market_match']['status']=='passed' else 'unable_to_evaluate',
                        'Source-backed alternative market comparison and exact-book checks complete' if comparison_ok and checks['market_match']['status']=='passed' else 'Alternative-market comparison incomplete/unavailable; '+checks['market_match']['reason']))
    stages.append(from_gates(6,list(GATES[:-1])))
    decision=row.get('publication_decision','pass')
    stages.append(stage(7,'passed' if decision=='official_pick' else 'failed',
                        'Official grade recorded; each parlay still requires a verified ticket' if decision=='official_pick' else 'Classification completed: '+str(decision)+'; no automatic promotion or verified parlay eligibility'))
    failed=[key for key in GATES if checks[key]['status']=='failed']
    unable=[key for key in GATES if checks[key]['status']=='unable_to_evaluate']
    return {'version':'sap-full-evaluation-2026-10-05','stages':stages,'qualifications':checks,
            'stages_attempted':len(stages),'qualifications_attempted':len(checks),
            'evaluation_complete':True,'all_requirements_passed':all(v['status']=='passed' for v in checks.values()) and all(v['status']=='passed' for v in stages),
            'failed_qualifications':failed,'unable_qualifications':unable,
            'missing_evidence':context.get('missing_evidence',[]),'missing_metrics':context.get('missing_metrics',[]),
            'computed_values':values}


def evaluation_lines(row):
    audit=row.get('full_evaluation')
    if not audit:
        return ['WAIT — full evaluation not recorded; legacy row requires a fresh check.']
    lines=['🔎 Stages checked: '+str(audit['stages_attempted'])+'/7 | Qualifications checked: '+str(audit['qualifications_attempted'])+'/12']
    symbols={'passed':'PASS','failed':'FAIL','unable_to_evaluate':'WAIT'}
    lines.append('Stages: '+' | '.join(symbols[s['status']]+str(s['stage']) for s in audit['stages']))
    lines.append('Checked means attempted. WAIT means the evidence or a dependency is unavailable, not that the message is still loading.')
    def brief_reason(reason):
        if reason.startswith('Depends on usable conservative probability:'):
            return 'Depends on usable conservative probability: see that qualification'
        return reason
    for status,heading in [('passed','✅ Passed'),('failed','❌ Failed'),('unable_to_evaluate','WAIT — unable to evaluate')]:
        items=[(k,v) for k,v in audit['qualifications'].items() if v['status']==status]
        lines.append(heading+': '+(', '.join(GATE_LABELS[k] for k,v in items) if status=='passed' else '; '.join(GATE_LABELS[k]+': '+brief_reason(v['reason']) for k,v in items)) if items else heading+': none')
    for s in audit['stages']:
        if s['status']!='passed':
            reason=s['reason']
            # Keep distinct stage explanations; refer to qualification detail once.
            for key,check in sorted(audit['qualifications'].items(),key=lambda kv:len(kv[1]['reason']),reverse=True):
                if check['status']!='passed' and check['reason']:
                    reason=reason.replace(check['reason'],'see '+GATE_LABELS[key])
            lines.append(symbols[s['status']]+' Stage '+str(s['stage'])+' — '+reason)
    statuses=row.get('public_source_status_counts',{})
    if statuses:
        lines.append('🌐 Public source attempts: '+', '.join(str(count)+' '+status for status,count in sorted(statuses.items()))+'; matching reports: '+str(len(row.get('public_research',[]))))
    elif 'public_source_attempts' in row:
        lines.append('🌐 No matching public-source attempts recorded for this event; research routing/feed coverage incomplete.')
    connection=row.get('projection_connection',{})
    if connection:
        lines.append('🔌 Structured input connection: '+connection['status'].replace('_',' ')+'; '+str(connection.get('reason') or '; '.join(connection.get('missing',[]))))
    if row.get('model',{}).get('experimental'):
        lines.append('🧪 History-only experimental forecast: unvalidated; advanced matchup/availability adjustments incomplete. Not an approved parlay leg.')
    lines.append('🎟️ Official parlay legs still require eligibility, joint probability and a confirmed combined quote. Manual approval concerns wagering, not research publication.')
    return lines
