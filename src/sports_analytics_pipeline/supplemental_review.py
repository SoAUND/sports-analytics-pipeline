"""Join explicitly reviewed source facts and book rules to exact live markets."""
from urllib.parse import urlsplit
from .revised_strategy import timestamp


def apply_review(row,records,*,as_of,accessible_books):
    result={**row,'book_accessible':row.get('book_of_record') in accessible_books}
    event=[r for r in records.get('events',[]) if r.get('event_id')==row['event_id'] and r.get('reviewed') is True]
    if len(event)!=1:return result
    review=event[0]
    if (review.get('home_name'),review.get('away_name'),review.get('start_time')) != (row['home_name'],row['away_name'],row['start_time']):return result
    if not review.get('reviewer') or timestamp(review['reviewed_at'])>as_of:return result
    sources=records.get('approved_sources',{})
    evidence=[]
    for item in review.get('evidence',[]):
        config=sources.get(item.get('source_key'),{})
        host=urlsplit(item.get('source_url','')).hostname or ''
        if not config.get('host') or host!=config['host'] or item.get('official')!=config.get('official'):continue
        if not item.get('facts') or not item.get('source_updated_at') or not item.get('collected_at'):continue
        evidence.append(item)
    result['evidence']=evidence
    for field in ('material_information_complete','conflict_clear','exposure_clear'):
        result[field]=review.get(field) is True
    if review.get('model_information_accounted_for') is not True:result['material_information_complete']=False
    result['stability']=review.get('stability','unknown')
    rules={}
    for rule in records.get('settlement_rules',[]):
        if rule.get('sport')!=row['sport'] or rule.get('market')!=row['market'] or rule.get('period')!=row['period']:continue
        if rule.get('reviewed') is not True or not rule.get('reviewer') or not rule.get('source_url') or not rule.get('rule_key'):continue
        if timestamp(rule['reviewed_at'])>as_of:continue
        if rule.get('book')=='hardrockbet_fl' and rule.get('state')!='fl':continue
        rules[rule['book']]=rule['rule_key']
    own=rules.get(row['book_of_record'])
    if own:
        result['settlement_rule']=own
        result['quotes']=[{**q,'settlement_rule':own} for q in row['quotes'] if rules.get(q['book'])==own]
    return result
