"""Game-specific missing inputs, with source attempts rather than generic labels."""
from importlib.resources import files
import json


DETAILS={
 'nfl':('named starting quarterback and official game-day inactive list','offensive/defensive EPA per play, success rate, pressure rate and opponent-adjusted home/away splits'),
 'cfb':('named starting quarterback, depth-chart changes and conference availability report','offensive/defensive EPA or success rate, opponent strength and home/away splits at the same competition level'),
 'nba':('named starting five, player minutes restrictions and bench-role changes','opponent-adjusted net rating, pace, shooting profile and lineup-impact estimates'),
 'wnba':('named starting five, player minutes restrictions and bench-role changes','opponent-adjusted net rating, pace, shooting profile and lineup-impact estimates'),
 'mlb':('named starting pitcher, batting order, handedness and bullpen availability','starter FIP/xFIP or equivalent, platoon splits and bullpen workload over the prior three days'),
 'nhl':('named confirmed starting goalie, forward/defense lines and scratches','goalie form, expected-goals rates, power play and penalty kill performance'),
 'epl':('official starting XI, goalkeeper and formation','expected goals for/against, chance quality, home/away splits and opponent style'),
 'mma':('official fighter pairing, weigh-in result and opponent/replacement changes','recent comparable-fight records, stance, reach, striking and takedown/defense matchups'),
 'atp':('official active singles pairing and withdrawal/retirement status','same-surface serve/return, hold/break rates, opponent matchups and recent comparable form'),
 'wta':('official active singles pairing and withdrawal/retirement status','same-surface serve/return, hold/break rates, opponent matchups and recent comparable form'),
}


def evidence_gaps(row):
    """Describe what this market-only call has not verified; never infer player names."""
    sport=row.get('sport');home=row.get('home_name','Home');away=row.get('away_name','Away')
    lineup,stats=DETAILS.get(sport,('confirmed participants and starting roles','sport-specific matchup statistics'))
    definitions=[
      ('model',f"{away} vs {home}",['independent win-probability estimate','uncertainty margin','prediction timestamp','model version','held-out calibration result','out-of-sample validation sample']),
      ('injury_availability',home,['named injured/unavailable players','current status of questionable players','expected impact on this selection']),
      ('injury_availability',away,['named injured/unavailable players','current status of questionable players','expected impact on this selection']),
      ('lineup_or_participants',f"{away} vs {home}",[lineup]),
      ('statistics_analytics',f"{away} vs {home}",[stats,'trend sample size/dates: at least eight comparable games in the last two to three seasons; opposing trends identified']),
      ('rest_travel',f"{away} vs {home}",['previous game/match time for each side','rest-day calculation','venue and travel/time-zone change']),
      ('transactions_or_changes',f"{away} vs {home}",['recent roster/participant changes','confirmed impact on starters or expected roles']),
    ]
    if sport in {'nfl','cfb','mlb','epl','atp','wta'}:
        definitions.append(('weather_environment',f"{away} vs {home}",['confirmed venue/roof or indoor status','game-time forecast: wind, temperature and precipitation']))
    if sport=='mma':definitions.append(('weigh_in_status',f"{away} vs {home}",['official weigh-in weights for both fighters','missed-weight/catchweight terms']))
    sources=json.loads(files('sports_analytics_pipeline').joinpath('data/public_sources.json').read_text())['sources']
    categories={s['source_key']:s['category'] for s in sources}
    gaps=[]
    for category,scope,fields in definitions:
        attempted=[]
        for a in row.get('public_source_attempts',[]):
            source_category=a.get('category') or categories.get(a.get('source_key','').split(':link:')[0])
            if source_category==category:attempted.append({k:a.get(k) for k in ('source_key','url','status','checked_at')})
        gaps.append({'category':category,'scope':scope,'status':'not_verified_for_this_call',
                     'missing_fields':fields,'source_attempts':attempted})
    return gaps


def gap_lines(row):
    lines=['Evidence missing or unverified for this call:']
    for gap in evidence_gaps(row):
        lines.append(gap['category']+' | '+gap['scope'])
        lines.extend('  - '+field for field in gap['missing_fields'])
        if not gap['source_attempts']:
            lines.append('  Source check: no corresponding source attempt recorded for this call.')
        else:
            for attempt in gap['source_attempts']:
                state='page retrieved; required game-specific fields remain unverified' if attempt['status']=='retrieved' else str(attempt['status'])
                lines.append('  Source check: '+str(attempt['source_key'])+' | '+state+' | checked '+str(attempt.get('checked_at') or 'time unavailable'))
    return lines
