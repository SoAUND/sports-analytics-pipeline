"""Parsers that turn free public reports into forecast inputs.

A row counts only when the team, phase, and cutoff match. Questionable is not
confirmed. A missing report stays missing.
"""
import csv
import io
from datetime import datetime, timezone


def _time(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return parsed.astimezone(timezone.utc)


def metric(value, *, sample_size, unit, phase, source_url, collected_at):
    if sample_size < 1:
        return None
    return {
        'value': float(value), 'sample_size': int(sample_size), 'unit': unit,
        'competition_phase': str(phase), 'source_url': source_url,
        'sources': [source_url], 'collected_at': _time(collected_at).isoformat(),
        'opponent_adjusted': False,
    }


def nflverse_out_count(text, team_abbrev, *, week, game_type, phase, as_of, source_url, collected_at):
    """Count official Out designations for one team-week. Questionable is ignored."""
    rows = list(csv.DictReader(io.StringIO(text)))
    matched = []
    for row in rows:
        if str(row.get('team', '')).upper() != str(team_abbrev).upper():
            continue
        if str(row.get('week')) != str(week) or str(row.get('game_type')) != str(game_type):
            continue
        updated = row.get('date_modified') or collected_at
        if _time(updated) > as_of:
            continue
        if str(row.get('report_status', '')).lower() == 'out':
            matched.append(row.get('full_name') or row.get('gsis_id'))
    if not rows:
        return None, []
    return metric(len(matched), sample_size=max(1, len(matched) or 1), unit='players',
                  phase=phase, source_url=source_url, collected_at=collected_at), matched


def espn_injury_out_count(payload, team_name, *, phase, as_of, source_url, collected_at):
    """Count Out athletes on one ESPN site-API team injury list."""
    teams = payload.get('injuries') or payload.get('teams') or []
    found = None
    for team in teams:
        label = ((team.get('team') or {}).get('displayName')) or team.get('displayName')
        if label and str(label).lower() == str(team_name).lower():
            found = team
            break
    if found is None:
        return None, []
    outs = []
    for item in found.get('injuries') or []:
        athlete = item.get('athlete') or {}
        status = str(item.get('status') or (item.get('type') or {}).get('description') or '').lower()
        if 'out' == status or status.startswith('out'):
            outs.append(athlete.get('displayName') or athlete.get('id'))
    if _time(collected_at) > as_of:
        raise ValueError('Future injury report')
    return metric(len(outs), sample_size=1, unit='players', phase=phase,
                  source_url=source_url, collected_at=collected_at), outs


def mlb_lineup_confirmed(payload):
    """True only when both official batting orders are published."""
    teams = ((payload.get('liveData') or {}).get('boxscore') or {}).get('teams') or payload.get('teams') or {}
    orders = []
    for side in ('home', 'away'):
        players = (teams.get(side) or {}).get('battingOrder') or (teams.get(side) or {}).get('lineup')
        orders.append(bool(players))
    return all(orders)


def attach_metric(team, name, record):
    if not record:
        return
    team.setdefault('advanced_metrics', {})[name] = record


def team_epa_per_play(text, team_abbrev, *, week, game_type, phase, source_url, collected_at):
    """Prior same-phase weeks only. The current week is excluded."""
    rows = [row for row in csv.DictReader(io.StringIO(text))
            if str(row.get('team', '')).upper() == str(team_abbrev).upper()
            and str(row.get('season_type')) == str(game_type)
            and int(float(row['week'])) < int(week)]
    if not rows:
        return None
    total = plays = 0.0
    for row in rows:
        attempts = float(row.get('attempts') or 0)
        carries = float(row.get('carries') or 0)
        if attempts + carries <= 0:
            continue
        total += float(row.get('passing_epa') or 0) + float(row.get('rushing_epa') or 0)
        plays += attempts + carries
    if plays <= 0:
        return None
    return metric(total / plays, sample_size=len(rows), unit='epa_per_play',
                  phase=phase, source_url=source_url, collected_at=collected_at)


def espn_web_url(site_url):
    """Documented fallback host when the site API returns 403."""
    return site_url.replace('https://site.api.espn.com/', 'https://site.web.api.espn.com/', 1)
