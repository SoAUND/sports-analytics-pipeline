"""Strategy-stage research contract for every automatic slate scan.

This module deliberately contains no sportsbook or statistics scraping.  Adapters
must supply timestamped observations with provenance; this layer audits coverage
and prevents discovery-stage rows from being promoted into picks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


ANALYSIS_VERSION = "sap-strategy-stages-2026-10-04"

STRATEGY_STAGES = (
    "Discover the complete slate", "Collect current information",
    "Run sport-specific advanced analysis", "Project useful markets",
    "Compare market expressions", "Apply value and qualification gates",
    "Grade selections and construct verified tickets",
)

FACTOR_NAMES = ("discipline", "player_matchup", "team_consistency", "player_consistency")
PERFORMANCE_FACTOR_NAMES = ("discipline", "player_matchup", "performance_consistency", "player_consistency")
SOCCER_SPORTS = {"epl", "soccer"}
SOCCER_OUTCOMES = ("home", "draw", "away")


def required_factor_names(sport):
    """Tennis and MMA replace team consistency with individual performance consistency."""
    if str(sport).casefold() in {"atp", "wta", "mma"}:
        return PERFORMANCE_FACTOR_NAMES
    return FACTOR_NAMES


def stored_three_way_probabilities(probabilities):
    """Return stored home, draw, and away values only when every one was supplied."""
    if not isinstance(probabilities, dict):
        return None
    stored = {}
    for name in SOCCER_OUTCOMES:
        value = probabilities.get(name) if name in probabilities else None
        if value is None or isinstance(value, bool):
            return None
        stored[name] = value
    return stored


def soccer_outcome_gaps(row):
    """Fail closed unless a soccer model estimated home, draw, and away separately."""
    sport = str(row.get("sport", "")).casefold()
    market = str(row.get("market", ""))
    if market not in {"moneyline", "three_way_moneyline"}:
        return ()
    if sport not in SOCCER_SPORTS and market != "three_way_moneyline":
        return ()
    outcomes = stored_three_way_probabilities((row.get("model") or {}).get("outcome_probabilities"))
    if outcomes is None:
        return SOCCER_OUTCOMES
    try:
        values = {name: float(outcomes[name]) for name in SOCCER_OUTCOMES}
    except (TypeError, ValueError):
        return SOCCER_OUTCOMES
    invalid = tuple(name for name, value in values.items() if not 0 <= value <= 1)
    if invalid:
        return invalid
    if abs(sum(values.values()) - 1.0) > 1e-3:
        return ("outcome_probabilities_do_not_sum_to_one",)
    side = str(row.get("participant") or "").casefold()
    if side not in SOCCER_OUTCOMES:
        return ("selected_side",)
    try:
        supplied = float((row.get("model") or {}).get("probability"))
    except (TypeError, ValueError):
        return ("selected_side",)
    if abs(supplied - values[side]) > 1e-6:
        return ("selected_side_disagrees",)
    return ()


def factor_gaps(row):
    factors = row.get("factor_analysis") or {}
    gaps = []
    for name in required_factor_names(row.get("sport")):
        item = factors.get(name) or {}
        if (item.get("status") != "verified" or not item.get("source_url")
            or not item.get("evidence") or not item.get("model_component")
            or not item.get("source_updated_at")
            or not isinstance(item.get("projection_effect"), (int, float))
            or not isinstance(item.get("uncertainty_effect"), (int, float))
            or item.get("uncertainty_effect", -1) < 0):
            gaps.append(name)
    components = [v.get("model_component") for v in factors.values() if isinstance(v, dict)]
    if len(components) != len(set(components)):
        gaps.append("duplicate_factor_component")
    return gaps

MARKET_FAMILIES = (
    "moneyline", "spreads", "totals", "team_totals", "alternate_lines",
    "first_half", "first_quarter", "period_markets", "player_props",
)

COMMON_EVIDENCE = (
    "official_status", "injury_availability", "lineup_or_participants",
    "rest_travel", "statistics_analytics", "transactions_or_changes",
    "multi_book_prices", "line_movement",
)

SPORT_METRICS = {
    "nfl": (
        "epa_per_play", "offensive_success_rate", "defensive_success_rate",
        "neutral_pace", "proe", "explosive_play_rate", "pressure_rate",
        "sack_rate", "offensive_line_continuity", "red_zone_efficiency",
        "third_down_efficiency", "turnover_regression", "weather",
        "home_road_rest_travel", "quarterback_availability",
        "skill_position_availability",
    ),
    "cfb": (
        "epa_per_play", "success_rate", "pace", "explosive_play_rate",
        "pressure_rate", "offensive_line_continuity", "red_zone_efficiency",
        "turnover_regression", "weather", "availability",
    ),
    "nba": (
        "offensive_rating", "defensive_rating", "net_rating", "pace",
        "effective_field_goal_percentage", "turnover_rate", "rebound_rate",
        "usage", "minutes", "on_off_splits", "potential_assists",
        "rebound_chances", "touches", "shot_attempts",
        "opponent_defense_by_position", "opponent_defense_by_play_type",
    ),
    "wnba": (
        "offensive_rating", "defensive_rating", "net_rating", "pace",
        "effective_field_goal_percentage", "turnover_rate", "rebound_rate",
        "usage", "minutes", "on_off_splits", "potential_assists",
        "rebound_chances", "touches", "shot_attempts",
        "opponent_defense_by_position", "opponent_defense_by_play_type",
    ),
    "mlb": (
        "xera", "fip", "xfip", "strikeout_rate", "walk_rate",
        "pitch_repertoire_matchup", "handedness_splits", "wrc_plus",
        "bullpen_workload", "batting_order", "park_factor", "weather",
        "pitcher_strikeout_projection", "pitcher_outs_projection",
    ),
    "nhl": (
        "expected_goals", "high_danger_chances", "shot_share",
        "special_teams", "confirmed_goalie", "goalie_quality",
    ),
    "epl": (
        "expected_goals", "non_penalty_expected_goals", "expected_goals_allowed",
        "shot_quality", "pressing", "set_pieces", "confirmed_lineups",
    ),
    "soccer": (
        "expected_goals", "non_penalty_expected_goals", "expected_goals_allowed",
        "shot_quality", "pressing", "set_pieces", "confirmed_lineups",
    ),
    "atp": (
        "surface_adjusted_elo", "hold_rate", "break_rate",
        "serve_points_won", "return_points_won", "fatigue", "court_conditions",
    ),
    "wta": (
        "surface_adjusted_elo", "hold_rate", "break_rate",
        "serve_points_won", "return_points_won", "fatigue", "court_conditions",
    ),
    "mma": (
        "striking_differential", "striking_defense", "takedowns", "control",
        "cardio", "reach", "stance", "weigh_in_status", "late_replacement",
    ),
}

NFL_PROP_METRICS = (
    "expected_snaps", "expected_routes", "targets", "carries", "pass_attempts",
    "target_share", "air_yard_share", "red_zone_opportunities",
    "man_zone_splits", "defensive_personnel", "coverage_tendencies",
    "opponent_adjusted_allowed_by_position", "pressure_rate_vs_quarterback",
    "run_defense_success_rate", "box_counts", "teammate_injury_role_change",
)

REQUIRED_OUTPUT = (
    "score", "market", "decimal_odds", "conservative_probability",
    "break_even_probability", "probability_edge", "ev_per_dollar",
    "minimum_decimal_odds", "matching_book_count", "largest_risk",
    "recheck_time",
)


def required_metrics(sport: str, market: str | None = None) -> tuple[str, ...]:
    sport_key = str(sport).casefold()
    base = SPORT_METRICS.get(sport_key, ())
    if sport_key == "nfl" and market in {"player_prop", "player_props"}:
        return tuple(dict.fromkeys((*base, *NFL_PROP_METRICS)))
    return base


def advanced_metric_gaps(row: Mapping) -> list[str]:
    metrics = row.get("advanced_metrics") or {}
    required = required_metrics(str(row.get("sport", "")), row.get("market"))
    return [name for name in required if metrics.get(name) is None] + factor_gaps(row)


def evidence_gaps(row: Mapping) -> list[str]:
    present = {item.get("category") for item in row.get("evidence", ())
               if item.get("status") == "verified"}
    return [name for name in COMMON_EVIDENCE if name not in present]


def output_gaps(row: Mapping) -> list[str]:
    return [name for name in REQUIRED_OUTPUT if row.get(name) is None]


@dataclass(frozen=True)
class AnalysisAudit:
    level: int
    first_blocker: str | None
    missing_metrics: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    missing_output: tuple[str, ...]
    strategy_complete: bool

    @property
    def level30_complete(self):
        """Compatibility alias for stored historical audits."""
        return self.strategy_complete


def audit_candidate(row: Mapping) -> AnalysisAudit:
    """Inspect every coverage component, then report the first incomplete stage.

    `level` is a compatibility progress indicator, not where execution stopped.
    The evaluator's full_evaluation contains all stage and qualification outcomes.
    """
    level = 1
    blocker = None
    metric_gaps = tuple(advanced_metric_gaps(row))
    evidence_missing = tuple(evidence_gaps(row))
    missing_output = tuple(output_gaps(row))
    gates = row.get("gates") or {}
    excluded = {"manual_approval"}
    if row.get("publication_decision") in {"experimental", "test_only_selection"}:
        excluded.add("model_eligibility")
    analytical_gates = tuple(k for k in gates if k not in excluded)
    gates_complete = bool(analytical_gates) and all(gates.get(k) is True for k in analytical_gates)
    if not row.get("event_id") or not row.get("market"):
        blocker = "level_1_market_identity"
    elif row.get("market_status") != "open" or not row.get("price_time"):
        level, blocker = 2, "level_2_market_freshness"
    elif metric_gaps or evidence_missing:
        level, blocker = 3, "stage_3_advanced_research"
    elif missing_output or not gates_complete:
        level = 6
        blocker = "stage_6_value_and_gates"
    else:
        level = len(STRATEGY_STAGES)
    return AnalysisAudit(level, blocker, metric_gaps, evidence_missing, missing_output, blocker is None)


def report_section(row: Mapping) -> str:
    """Map a row to the mandatory Telegram section, failing closed."""
    decision = row.get("publication_decision")
    audit = audit_candidate(row)
    if decision == "official_pick" and audit.level30_complete:
        return "Official Picks"
    if decision == "conditional_pick" and audit.level >= 6:
        return "Conditional Picks"
    if decision in {"experimental", "test_only_selection"} and audit.level >= 6:
        return "Test-Only Selections"
    if decision in {"watchlist", "experimental_call"}:
        return "Leans / Watchlist"
    if decision in {"missing_market", "unavailable"}:
        return "Missing or Unavailable Markets"
    return "Passes"


def discoverable_markets(provider_markets: Iterable[str]) -> dict[str, bool]:
    """Audit full market-family discovery without pretending unsupported equals checked."""
    offered = {str(value).casefold() for value in provider_markets}
    aliases = {"h2h": "moneyline", "spreads": "spreads", "totals": "totals",
               "team_totals": "team_totals", "alternate_spreads": "alternate_lines",
               "alternate_totals": "alternate_lines", "player_props": "player_props"}
    normalized = {aliases.get(value, value) for value in offered}
    return {market: market in normalized for market in MARKET_FAMILIES}
