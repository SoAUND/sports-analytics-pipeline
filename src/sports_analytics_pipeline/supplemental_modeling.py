"""Experimental, chronological score model for NHL/CFB.

Fits only earlier finalized regular-season games. Historical holdout count is never
reported as forward validation or approval. Prices never become model features.
"""
from collections import defaultdict
from datetime import timedelta
from hashlib import sha256
import json
import math
from statistics import mean
from .revised_strategy import timestamp


def fit_line(xs,ys):
    xbar,ybar=mean(xs),mean(ys)
    variance=sum((x-xbar)**2 for x in xs)
    if variance<=1e-9:raise ValueError('Model feature has no variation')
    slope=sum((x-xbar)*(y-ybar) for x,y in zip(xs,ys))/variance
    return ybar-slope*xbar,slope


def predict_line(coefficients,x):return coefficients[0]+coefficients[1]*x


def train_score_model(games,*,sport,as_of):
    if sport not in {'nhl','cfb'}:raise ValueError('Unsupported score model sport')
    seen={};history=defaultdict(list);examples=[]
    for g in games:
        try:
            if g['sport']!=sport or g.get('regular_season') is not True or g.get('final') is not True:continue
            start,end=timestamp(g['start_time']),timestamp(g['final_available_at'])
            if end>as_of or end<=start:continue
            home,away=g['home_name'],g['away_name']
            hs,aws=float(g['home_score']),float(g['away_score'])
            if not all(math.isfinite(x) and x>=0 and x.is_integer() for x in (hs,aws)):continue
            if home==away or not home or not away:continue
            row={**g,'home_score':hs,'away_score':aws,'start':start,'end':end}
            if g['game_id'] in seen and seen[g['game_id']]!=row:raise ValueError('Conflicting finalized game identity')
            seen[g['game_id']]=row
        except (KeyError,TypeError):continue
    ordered=sorted(seen.values(),key=lambda x:(x['start'],x['game_id']))
    # Final-time eligibility prevents a concurrent game's score entering a feature.
    prior=[]
    for game in ordered:
        relevant=[g for g in prior if g['end']<=game['start']]
        h=[g for g in relevant if game['home_name'] in (g['home_name'],g['away_name'])][-20:]
        a=[g for g in relevant if game['away_name'] in (g['home_name'],g['away_name'])][-20:]
        if len(h)>=8 and len(a)>=8:
            hm=mean(g['home_score']-g['away_score'] if g['home_name']==game['home_name'] else g['away_score']-g['home_score'] for g in h)
            am=mean(g['home_score']-g['away_score'] if g['home_name']==game['away_name'] else g['away_score']-g['home_score'] for g in a)
            total=mean([g['home_score']+g['away_score'] for g in h+a])
            examples.append({'margin_feature':hm-am,'total_feature':total,'margin':game['home_score']-game['away_score'],
                'total':game['home_score']+game['away_score'],'start':game['start'].isoformat()})
        prior.append(game)
    split=int(len(examples)*.8)
    training,validation=examples[:split],examples[split:]
    if len(training)<200 or len(validation)<100:raise ValueError('Need 200 chronological training and 100 holdout games after team warm-up')
    margin=fit_line([x['margin_feature'] for x in training],[x['margin'] for x in training])
    total=fit_line([x['total_feature'] for x in training],[x['total'] for x in training])
    residuals=[x['margin']-predict_line(margin,x['margin_feature']) for x in training]
    total_residuals=[x['total']-predict_line(total,x['total_feature']) for x in training]
    bins=defaultdict(list)
    margin_lines=(0,-1.5,1.5) if sport=='nhl' else (0,-7.5,-3.5,3.5,7.5)
    total_lines=(5.5,6.5) if sport=='nhl' else (45.5,55.5,65.5)
    for v in validation:
        for family,lines,coefficients,feature,errors,target in (
                ('margin',margin_lines,margin,'margin_feature',residuals,'margin'),
                ('total',total_lines,total,'total_feature',total_residuals,'total')):
            expected=predict_line(coefficients,v[feature])
            for line in lines:
                p,push=distribution_probability(expected,errors,line)
                if line==0 and family=='margin':p+=push*.5
                outcome=float(v[target]>line)+(.5*float(v[target]==line) if line==0 else 0)
                bins[(family,line,min(9,int(p*10)))].append((p,outcome))
    error=max(abs(mean(p for p,y in b)-mean(y for p,y in b))+1.96*.5/math.sqrt(len(b)) for b in bins.values())
    uncertainty=min(1,max(.01,error,1.96*.5/math.sqrt(len(validation))))
    digest=sha256(json.dumps([{k:v for k,v in g.items() if k not in {'start','end'}} for g in ordered],sort_keys=True).encode()).hexdigest()[:16]
    return {'version':sport+'-experimental-score-v1-'+digest,'sport':sport,'as_of':as_of.isoformat(),
        'margin_coefficients':margin,'total_coefficients':total,'margin_residuals':residuals,'total_residuals':total_residuals,
        'uncertainty':uncertainty,'historical_training_count':len(training),'historical_holdout_count':len(validation),
        'approved':False,'oos_count':0,'calibration':'unreviewed','games':ordered,'validation_examples':validation}


def distribution_probability(expected,residuals,line):
    scores=[round(expected+r) for r in residuals]
    n=len(scores)
    return sum(x>line for x in scores)/n,sum(x==line for x in scores)/n


def score_prediction(model,row,*,as_of):
    if model['sport']!=row['sport'] or timestamp(model['as_of'])>as_of:raise ValueError('Model sport/time mismatch')
    if timestamp(row['start_time'])<=as_of:raise ValueError('Pregame window closed')
    games=[g for g in model['games'] if g['end']<=as_of]
    h=[g for g in games if row['home_name'] in (g['home_name'],g['away_name'])][-20:]
    a=[g for g in games if row['away_name'] in (g['home_name'],g['away_name'])][-20:]
    if len(h)<8 or len(a)<8:raise ValueError('Insufficient matched team history')
    hm=mean(g['home_score']-g['away_score'] if g['home_name']==row['home_name'] else g['away_score']-g['home_score'] for g in h)
    am=mean(g['home_score']-g['away_score'] if g['home_name']==row['away_name'] else g['away_score']-g['home_score'] for g in a)
    market=row['market'];participant=row['participant']
    expected=predict_line(model['margin_coefficients'],hm-am)
    if market=='totals':
        expected=predict_line(model['total_coefficients'],mean([g['home_score']+g['away_score'] for g in h+a]))
        win,push=distribution_probability(expected,model['total_residuals'],float(row['line']))
        if participant=='under':win=1-win-push
    elif market=='spreads':
        threshold=-float(row['line']) if participant=='home' else float(row['line'])
        win,push=distribution_probability(expected,model['margin_residuals'],threshold)
        if participant=='away':win=1-win-push
    elif market=='moneyline':
        win,tie=distribution_probability(expected,model['margin_residuals'],0)
        # Experimental score-distribution tie mass splits evenly; it is not a book push.
        win+=tie*.5;push=0
        if participant=='away':win=1-win
    else:raise ValueError('Unsupported model market')
    # Measure uncertainty at this exact line and probability region. A sparse
    # region remains fully uncertain, rather than capping or borrowing confidence.
    conditional=win/(1-push) if push<1 else 0
    local=[]
    for v in model['validation_examples']:
        if market=='totals':
            e=predict_line(model['total_coefficients'],v['total_feature'])
            pw,pr=distribution_probability(e,model['total_residuals'],float(row['line']))
            actual=v['total'];boundary=float(row['line'])
            y=float(actual>boundary)
            if participant=='under':pw=1-pw-pr;y=float(actual<boundary)
        else:
            e=predict_line(model['margin_coefficients'],v['margin_feature'])
            boundary=0 if market=='moneyline' else -float(row['line']) if participant=='home' else float(row['line'])
            pw,pr=distribution_probability(e,model['margin_residuals'],boundary)
            actual=v['margin'];y=float(actual>boundary)
            if market=='moneyline':pw+=pr*.5;pr=0
            if participant=='away':pw=1-pw-pr;y=float(actual<boundary)
        if actual==boundary or pr>=1:continue
        pc=pw/(1-pr)
        if abs(pc-conditional)<=.10:local.append((pc,y))
    uncertainty=1 if len(local)<20 else abs(mean(p for p,y in local)-mean(y for p,y in local))+1.96*.5/math.sqrt(len(local))
    return {'probability':win,'push_probability':push,'uncertainty':uncertainty,'version':model['version'],
        'prediction_time':as_of.isoformat(),'uncertainty_method':'exact-line local holdout calibration gap plus 95% sampling allowance; fewer than 20 matches blocks',
        'local_calibration_count':len(local),
        'approved':False,'oos_count':0,'calibration':'unreviewed',
        'historical_holdout_count':model['historical_holdout_count'],'historical_training_count':model['historical_training_count']}
