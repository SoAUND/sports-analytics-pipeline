"""Sport-specific feature adjustments that actually change experimental forecasts.

A downloaded statistic is not a model input until it passes identity, phase, time,
and unit checks and is recorded in factor_analysis. Missing inputs stay missing.
Nothing here approves a model or creates forward validation.
"""
from datetime import datetime, timedelta, timezone
from math import sqrt

from .deep_analysis import NFL_PROP_METRICS, required_metrics

# Score-history can support only these. The rest stay unavailable until a
# reviewed source supplies the named observation.
SCORE_DERIVED = {
    'home_away_scoring_split': 'Mean points for at home minus points for on the road, same phase.',
    'scoring_consistency': 'Population standard deviation of points for; higher widens uncertainty only.',
    'opponent_unadjusted_margin': 'Mean score margin. Not opponent-adjusted and not double-counted as EPA.',
}
EXPLICIT_ONLY = {
    'nfl': (
        'epa_per_play', 'offensive_success_rate', 'defensive_success_rate', 'neutral_pace',
        'pass_rate_over_expectation', 'explosive_play_rate', 'pressure_rate', 'sack_rate',
        'offensive_line_continuity', 'red_zone_efficiency', 'third_down_efficiency',
        'turnover_regression', 'weather', 'rest_travel', 'quarterback_availability',
        'skill_player_availability', 'confirmed_out_count',
    ),
    'nba': (
        'offensive_rating', 'defensive_rating', 'net_rating', 'pace', 'effective_fg',
        'turnover_rate', 'rebounding_rate', 'usage', 'minutes', 'on_off', 'confirmed_out_count',
    ),
    'wnba': (
        'offensive_rating', 'defensive_rating', 'net_rating', 'pace', 'effective_fg',
        'turnover_rate', 'rebounding_rate', 'usage', 'minutes', 'on_off', 'confirmed_out_count',
    ),
    'mlb': (
        'expected_era', 'fip', 'strikeout_rate', 'walk_rate', 'pitch_repertoire_vs_lineup',
        'handedness', 'wrc_plus', 'bullpen_workload', 'batting_order', 'park_weather',
    ),
    'nhl': (
        'expected_goals', 'high_danger_chances', 'shot_share', 'special_teams',
        'confirmed_goalie', 'goalie_quality',
    ),
    'soccer': (
        'expected_goals', 'non_penalty_expected_goals', 'expected_goals_allowed',
        'shot_quality', 'pressing', 'set_pieces', 'confirmed_lineups',
    ),
    'atp': (
        'surface_adjusted_elo', 'hold_rate', 'break_rate', 'serve_points_won',
        'return_points_won', 'fatigue', 'court_conditions', 'retirement_risk',
    ),
    'wta': (
        'surface_adjusted_elo', 'hold_rate', 'break_rate', 'serve_points_won',
        'return_points_won', 'fatigue', 'court_conditions', 'retirement_risk',
    ),
    'mma': (
        'striking_differential', 'striking_defense', 'takedowns', 'control', 'cardio',
        'reach', 'stance', 'weigh_in_status', 'late_replacement',
    ),
}
PROP_REQUIRED = {
    'opportunity_history': 'Completed same-phase player logs with the prop statistic and its opportunity unit.',
    'confirmed_role': 'Confirmed lineup, snap/minute/order role, or official weigh-in. Projected role is not enough.',
    'matching_quote': 'Exact open over/under quote with the same player, stat, line, and book identity.',
}


def _time(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return parsed.astimezone(timezone.utc)


def sport_key(sport):
    text = str(sport or '')
    if text.startswith('soccer_') or text == 'epl':
        return 'soccer'
    if text == 'basketball_nba_preseason':
        return 'nba'
    return text


def coverage_matrix():
    """Source to parser to feature to model to evaluator. Honest, not a green board."""
    rows = []
    for sport, metrics in EXPLICIT_ONLY.items():
        for metric in metrics:
            rows.append({
                'sport': sport, 'feature': metric,
                'source': 'explicit advanced observation on the team or player record',
                'parser': 'advanced_features.usable_metric',
                'model_effect': 'capped logit shift only when both sides share phase, unit, and cutoff',
                'evaluator': 'factor_analysis plus advanced_metric_gaps; approval stays false',
                'status': 'wired_when_supplied',
            })
        for metric, note in SCORE_DERIVED.items():
            rows.append({
                'sport': sport, 'feature': metric, 'source': 'same-phase completed scores',
                'parser': 'advanced_features.score_features', 'model_effect': note,
                'evaluator': 'uncertainty or recorded unused split; not an approved model',
                'status': 'derived_from_scores',
            })
    for feature, note in PROP_REQUIRED.items():
        rows.append({
            'sport': 'player_prop', 'feature': feature, 'source': 'public player log plus exact quote',
            'parser': 'value_models.prop_distribution', 'model_effect': note,
            'evaluator': 'prepare_value_candidate only after a complete input',
            'status': 'refuses_without_confirmed_role',
        })
    return rows


def score_features(observations, *, phase, as_of):
    """Home/away split and consistency from completed same-phase scores only."""
    usable = []
    for record in observations or []:
        if str(record.get('competition_phase')) != str(phase):
            continue
        played, collected = _time(record['played_at']), _time(record['collected_at'])
        if collected > as_of or played > as_of - timedelta(hours=12):
            raise ValueError('Future or live score cannot enter a feature')
        usable.append(record)
    if len(usable) < 4:
        return {}
    points = [float(r['score_for']) for r in usable]
    mean = sum(points) / len(points)
    variance = sum((x - mean) ** 2 for x in points) / len(points)
    home = [float(r['score_for']) for r in usable if r.get('home_away') == 'home']
    away = [float(r['score_for']) for r in usable if r.get('home_away') == 'away']
    features = {
        'scoring_consistency': {
            'value': sqrt(variance), 'sample_size': len(usable), 'unit': 'points',
            'phase': str(phase), 'opponent_adjusted': False,
            'sources': sorted({r.get('source_url') for r in usable if r.get('source_url')}),
        }
    }
    if len(home) >= 2 and len(away) >= 2:
        features['home_away_scoring_split'] = {
            'value': sum(home) / len(home) - sum(away) / len(away),
            'sample_size': min(len(home), len(away)), 'unit': 'points',
            'phase': str(phase), 'opponent_adjusted': False,
            'sources': features['scoring_consistency']['sources'],
        }
    margins = [float(r['score_for']) - float(r['score_against']) for r in usable]
    features['opponent_unadjusted_margin'] = {
        'value': sum(margins) / len(margins), 'sample_size': len(usable), 'unit': 'points',
        'phase': str(phase), 'opponent_adjusted': False,
        'sources': features['scoring_consistency']['sources'],
    }
    return features


def usable_metric(record, *, phase, as_of):
    if not isinstance(record, dict) or record.get('value') is None:
        return None
    if str(record.get('competition_phase')) != str(phase):
        return None
    if _time(record['collected_at']) > as_of:
        raise ValueError('Future feature collection cannot enter the forecast')
    value = float(record['value'])
    if value != value:
        raise ValueError('Non-finite feature')
    sample = int(record.get('sample_size') or 0)
    if sample < 4:
        return None
    return {**record, 'value': value, 'sample_size': sample}


def apply_advanced_features(model, history, row, *, as_of):
    """Adjust an experimental forecast. Never sets approval or forward OOS count."""
    updated = dict(model)
    updated['approved'] = False
    updated['oos_count'] = 0
    updated['calibration'] = updated.get('calibration') or 'unreviewed'
    updated['experimental'] = True
    factors = {}
    phase = (history.get('snapshot') or {}).get('competition_phase')
    counts = updated.get('sample_size_per_side') or {}
    if counts and min(counts.values()) and max(counts.values()) / min(counts.values()) >= 2:
        extra = min(0.12, 0.03 * (max(counts.values()) / min(counts.values()) - 1))
        updated['uncertainty'] = min(1.0, float(updated['uncertainty']) + extra)
        factors['phase_sample_balance'] = {
            'status': 'applied', 'effect': 'uncertainty_only',
            'uncertainty_effect': extra, 'counts': counts,
            'probability_effect': 0,
            'reason': 'Same-phase samples differ by at least 2x. Phases were not mixed to fill the smaller side.',
            'provenance': 'completed same-phase observations already accepted by the baseline',
        }
    derived = {}
    for side in ('home', 'away'):
        team = (history.get('teams') or {}).get(side) or {}
        derived[side] = score_features(team.get('observations'), phase=phase, as_of=as_of)
        explicit = team.get('advanced_metrics') or {}
        for name, record in explicit.items():
            parsed = usable_metric(record, phase=phase, as_of=as_of)
            if parsed:
                derived[side][name] = parsed
    split = derived['home'].get('home_away_scoring_split')
    if split and row.get('participant') in {'home', 'away'}:
        factors['home_away_scoring_split'] = {
            'status': 'recorded_not_added_to_probability',
            'probability_effect': 0, 'uncertainty_effect': 0,
            'value': split['value'], 'provenance': split['sources'],
            'reason': 'Home/away scoring split is recorded. It is not added on top of score scenarios, which already use those games.',
        }
    consistency = [derived[s].get('scoring_consistency') for s in ('home', 'away')]
    if all(consistency):
        widen = min(0.05, sum(x['value'] for x in consistency) / 200)
        updated['uncertainty'] = min(1.0, float(updated['uncertainty']) + widen)
        factors['scoring_consistency'] = {
            'status': 'applied', 'effect': 'uncertainty_only', 'uncertainty_effect': widen,
            'probability_effect': 0,
            'values': {s: derived[s]['scoring_consistency']['value'] for s in ('home', 'away')},
            'provenance': sorted({u for s in consistency for u in s['sources']}),
            'reason': 'Higher same-phase scoring variance widens uncertainty. It does not move the point probability.',
        }
    key = sport_key(row.get('sport'))
    required = EXPLICIT_ONLY.get(key, ())
    missing = []
    shift = 0.0
    for metric in required:
        sides = [derived[s].get(metric) for s in ('home', 'away')]
        if not all(sides):
            missing.append(metric)
            continue
        if {s['unit'] for s in sides if s.get('unit')} not in (set(), {sides[0].get('unit')}):
            missing.append(metric + ':unit_conflict')
            continue
        effect = max(-0.03, min(0.03, (sides[0]['value'] - sides[1]['value']) * 0.01))
        if metric.endswith('_out_count'):
            effect = -effect
        if row.get('participant') == 'away':
            effect = -effect
        elif row.get('participant') not in {'home', 'away'}:
            effect = 0.0
        shift += effect
        factors[metric] = {
            'status': 'applied', 'probability_effect': effect, 'uncertainty_effect': 0.005,
            'home': sides[0]['value'], 'away': sides[1]['value'],
            'sample_size': min(s['sample_size'] for s in sides),
            'provenance': sorted({u for s in sides for u in (s.get('sources') or [s.get('source_url')]) if u}),
            'reason': 'Both sides supplied the same phase and unit. Shift is capped so an unvalidated feature cannot create an official edge.',
        }
        updated['uncertainty'] = min(1.0, float(updated['uncertainty']) + 0.005)
    shift = max(-0.06, min(0.06, shift))
    if shift:
        updated['probability'] = min(0.95, max(0.05, float(updated['probability']) + shift))
        updated['feature_probability_shift'] = shift
    injuries = ((history.get('snapshot') or {}).get('details') or {}).get('injuries')
    if injuries:
        factors['availability_snapshot'] = {
            'status': 'recorded_not_priced', 'probability_effect': 0, 'uncertainty_effect': 0.03,
            'reason': 'An injury payload was collected. It is not a confirmed role change until a starter status and role effect are both present.',
            'provenance': [(history.get('snapshot') or {}).get('source_url')],
        }
        updated['uncertainty'] = min(1.0, float(updated['uncertainty']) + 0.03)
    else:
        missing.append('confirmed_availability')
    updated['factor_analysis'] = factors
    updated['advanced_metrics'] = {name: factors[name]['probability_effect'] for name in factors if factors[name].get('status') == 'applied' and name in required}
    updated['advanced_metrics_missing'] = missing
    limitations = list(updated.get('limitations') or [])
    limitations.append('Advanced features change this forecast only when both sides supply the same phase and unit. Missing features were not filled.')
    if counts and min(counts.values()) < 8:
        limitations.append('Below the eight same-phase game floor; this forecast must not be promoted.')
    updated['limitations'] = limitations
    updated['required_metrics'] = list(required_metrics(key if key != 'soccer' else row.get('sport', 'soccer'), row.get('market')))
    if row.get('market') == 'player_prop':
        updated['required_metrics'] = list(dict.fromkeys([*updated['required_metrics'], *NFL_PROP_METRICS]))
    return updated


def prop_input_ready(row):
    """Refuse a new projected role. Legacy inputs still go through the mixture checks."""
    data = row.get('prop_distribution_input')
    if not isinstance(data, dict):
        return False, 'prop distribution input missing'
    role = data.get('role')
    if isinstance(role, dict) and (role.get('status') != 'confirmed' or not role.get('source_url')):
        return False, 'confirmed role/lineup/weigh-in missing'
    return True, ''
