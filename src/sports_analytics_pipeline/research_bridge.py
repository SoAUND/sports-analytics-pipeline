"""Carry model-linked research through export without inventing analysis."""
from copy import deepcopy
from .revised_strategy import timestamp


def projection_evidence(checks, candidate, model, *, as_of):
    """Accept only verified bundles for this exact prediction and market.

    Source adapters store projection_evidence inside evidence_json. The model
    separately declares factor_components; research cannot declare model usage.
    """
    result = {}
    identity = ('event_id', 'market', 'period', 'line', 'participant')
    if candidate.get('market') == 'player_prop':
        identity += ('prop_stat', 'direction', 'settlement_rule')
    cutoff = timestamp(model['prediction_time'])
    for check in checks:
        if check.get('status') != 'verified' or not check.get('source_url'):
            continue
        try:
            updated = timestamp(check['source_updated_at'])
            collected = timestamp(check['collected_at'])
            if not updated <= collected <= cutoff <= as_of:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        bundles = check.get('data', {}).get('projection_evidence', [])
        if not isinstance(bundles, list):
            continue
        for bundle in bundles:
            if not isinstance(bundle, dict):
                continue
            if any(bundle.get(k) != candidate.get(k) for k in identity):
                continue
            if bundle.get('model_version') != model['version'] or bundle.get('prediction_time') != model['prediction_time']:
                continue
            for key in ('advanced_metrics', 'factor_analysis', 'market_comparison', 'trend', 'prop_distribution_input'):
                value = bundle.get(key)
                if isinstance(value, dict) and key not in result:
                    result[key] = deepcopy(value)
    return result
