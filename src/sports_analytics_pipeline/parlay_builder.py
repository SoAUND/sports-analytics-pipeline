"""Automatic 2/4-leg discovery with explicit quote and dependency gates.

Never multiply sportsbook leg prices and call that an actual ticket quote.
Candidate combinations without ticket evidence are reviews, not picks.
"""
from itertools import combinations
from decimal import Decimal, InvalidOperation
from .deep_analysis import audit_candidate
from .revised_strategy import timestamp, market_identity, evaluate_ticket, american, pick_line


def leg_key(row):
    return market_identity(row) + (row.get('selection_name'), row.get('book_of_record'))


def ticket_key(legs):
    return frozenset(leg_key(row) for row in legs)


def eligible_leg(row, as_of):
    """Tier 1-3 and test-only legs can enter an estimated ticket. Edge is not required."""
    try:
        return (row.get('publication_decision') in {'official_pick', 'conditional_pick', 'experimental'}
                and row.get('market_status') == 'open'
                and timestamp(row['start_time']) > as_of
                and as_of.timestamp() - 3600 <= timestamp(row['price_time']).timestamp() <= as_of.timestamp()
                and Decimal(str(row['decimal_odds'])) > 1)
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return False


def build_parlays(rows, *, as_of, ticket_quotes=()):
    """Search all eligible 2/4-leg combinations; retain a small ranked report.

One event per automatic ticket avoids contradictory outcomes and unmodeled
same-game dependency. Explicit supplied same-game tickets retain their separate
review path. Mixed books cannot create a executable ticket.
"""
    unique = {}
    for row in rows:
        if eligible_leg(row, as_of):
            key = leg_key(row)
            if key not in unique or rank([row]) > rank([unique[key]]):
                unique[key] = row
    legs = list(unique.values())
    quotes = {}
    for quote in ticket_quotes:
        try:
            quotes.setdefault(ticket_key(quote['legs']), []).append(quote)
        except (KeyError, TypeError):
            continue
    pools = {'team': [r for r in legs if r.get('market') != 'player_prop'],
             'props': [r for r in legs if r.get('market') == 'player_prop']}
    result = {'verified': [], 'reviews': [], 'combinations_checked': 0,
              'eligible_legs': len(legs), 'blocked_quotes': 0}
    for kind, pool in pools.items():
        for size in (2, 4):
            verified, reviews = [], []
            for combo in combinations(pool, size):
                if len({r['event_id'] for r in combo}) != size:
                    continue
                if len({r['book_of_record'] for r in combo}) != 1:
                    continue
                result['combinations_checked'] += 1
                ticket = {'legs': list(combo), 'kind': kind, 'leg_count': size,
                          'decision': 'conditional_review',
                          'reason': 'Actual combined quote and dependency evidence required'}
                accepted = []
                for quote in quotes.get(ticket_key(combo), []):
                    try:
                        if (quote.get('book_of_record') != combo[0]['book_of_record']
                            or not quote.get('source_url')
                            or quote.get('quote_status') != 'confirmed'
                            or not as_of.timestamp()-3600 <= timestamp(quote['price_time']).timestamp() <= as_of.timestamp()
                            or not quote.get('correlation_source_url')
                            or not quote.get('correlation_evidence')
                            or not quote.get('story')
                            or not as_of.timestamp()-3600 <= timestamp(quote['correlation_time']).timestamp() <= as_of.timestamp()):
                            raise ValueError('Ticket evidence missing or stale')
                        graded = evaluate_ticket(list(combo), decimal_odds=quote.get('decimal_odds'),
                            joint_probability=quote.get('joint_probability'),
                            correlation_method=quote.get('correlation_method'), story=quote.get('story'))
                        if graded['decision'] not in {'official_pick', 'experimental'}:
                            raise ValueError(graded.get('reason', 'Ticket value failed'))
                        # A joint win probability cannot exceed any constituent win probability.
                        probabilities = [Decimal(str(r['conservative_probability'])) * (1-Decimal(str(r.get('push_probability', 0)))) for r in combo]
                        if any(Decimal(str(r.get('push_probability', 0))) != 0 for r in combo):
                            raise ValueError('Automatic push-ticket settlement model unavailable')
                        joint = Decimal(str(graded['joint_probability']))
                        if not max(Decimal(0), sum(probabilities)-size+1) <= joint <= min(probabilities):
                            raise ValueError('Joint probability violates marginal bounds')
                        accepted.append({**ticket, **quote, **graded, 'legs': list(combo), 'kind': kind})
                    except (KeyError, TypeError, ValueError, InvalidOperation):
                        result['blocked_quotes'] += 1
                if accepted:
                    verified.append(max(accepted, key=lambda t: Decimal(t['ev_per_dollar'])))
                else:
                    graded = evaluate_ticket(list(combo), story='distinct events')
                    if graded.get('estimated'):
                        reviews.append({**ticket, **graded, 'legs': list(combo), 'kind': kind,
                                        'reason': graded['honesty_label']})
                    else:
                        if quotes.get(ticket_key(combo)):
                            ticket['reason'] = 'Supplied ticket failed price, dependency, settlement or value verification'
                        reviews.append(ticket)
            # Ranking happens only after every combination is searched. No discovery cap.
            result['verified'].extend(sorted(verified, key=lambda t: (t['decision']=='official_pick', Decimal(t['ev_per_dollar']), rank(t['legs'])), reverse=True)[:3])
            result['reviews'].extend(sorted(reviews, key=lambda t: rank(t['legs']), reverse=True)[:1])
    return result


def rank(legs):
    return (all(r.get('publication_decision') == 'official_pick' for r in legs),
            min(Decimal(str(r.get('score', 0))) for r in legs),
            sum(Decimal(str(r.get('probability_edge', 0))) for r in legs))


def parlay_report(result):
    lines = ['🎟️ PARLAYS', '']
    for kind, label in [('team', 'TEAM MARKETS'), ('props', 'PLAYER PROPS')]:
        for size in (2, 4):
            lines.append(f'{size}-LEG {label}')
            tickets = [t for t in result['verified'] if t['kind']==kind and len(t['legs'])==size]
            if not tickets:
                estimated = [t for t in result['reviews'] if t.get('estimated') and t['kind']==kind and len(t['legs'])==size]
                if not estimated:
                    lines.append('No qualifying verified ticket.')
                for ticket in estimated[:3]:
                    lines.append(ticket['honesty_label'] + ' | ' + american(ticket['decimal_odds']))
                    lines.extend(pick_line(row) for row in ticket['legs'])
            for ticket in tickets:
                icon = '🔥' if ticket['decision']=='official_pick' and ticket['edge_tier']=='Strongest' else '✅' if ticket['decision']=='official_pick' else '🧪'
                lines.append(f"{icon} {ticket['decision'].replace('_', ' ')} | {ticket['edge_tier']} | Combined {american(ticket['decimal_odds'])}")
                lines.extend(pick_line(row) for row in ticket['legs'])
                lines.append(f"Joint conservative: {ticket['joint_probability']} | Edge: {Decimal(ticket['probability_edge'])*100:.2f} pp | EV/$: {ticket['ev_per_dollar']}")
                lines.append('Price source: '+str(ticket['book_of_record'])+' | Checked: '+str(ticket['price_time']))
                lines.append('Story: '+str(ticket['story']))
            lines.append('')
    lines.extend(['🎟️ SEPARATE OVERALL-PLUS-MONEY PARLAYS', ''])
    from .research_first import ESTIMATE_LABEL
    plus = [t for t in result['verified'] if Decimal(t['decimal_odds']) >= 2]
    estimated_plus = [t for t in result['reviews'] if t.get('estimated') and Decimal(str(t.get('decimal_odds') or 0)) >= 2]
    if not plus and not estimated_plus:
        lines.append('No qualifying verified ticket.')
    for ticket in plus:
        lines.append(f"{ticket['leg_count']}-leg {ticket['kind']} | {ticket['decision']} | Combined {american(ticket['decimal_odds'])}")
        lines.extend(pick_line(row) for row in ticket['legs'])
    for ticket in estimated_plus[:3]:
        lines.append(ESTIMATE_LABEL + ' | ' + american(ticket['decimal_odds']))
        lines.extend(pick_line(row) for row in ticket['legs'])
    if result['reviews']:
        lines.extend(['', '⚠️ PARLAY REVIEWS — NOT PICKS'])
        for ticket in result['reviews']:
            lines.append(f"{ticket['leg_count']}-leg {ticket['kind']} | {ticket['reason']}")
        lines.append('Review combinations and full evidence are saved in the run report; no unverified ticket is promoted.')
    lines.extend(['', f"Eligible legs: {result['eligible_legs']} | Combinations checked: {result['combinations_checked']}"])
    return '\n'.join(lines)
