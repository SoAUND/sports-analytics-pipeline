"""Controlled feature contracts for each first multi-sport model.

These contracts prevent a model from silently changing its inputs from event to event.
Values must come from timestamped research or market data that existed before the
prediction cutoff.  A missing required feature is a hard failure, not an invitation
to invent a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import sqlite3

from .multisport_runtime import record_feature


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    description: str
    source_category: str


FEATURE_VERSION = "multisport-features-1.0"
EPL_BASELINE_FEATURE_VERSION = "epl-baseline-features-1.0"


EPL_BASELINE_FEATURES = (
    "market_home_no_vig",
    "market_draw_no_vig",
    "market_away_no_vig",
    "recent5_points_diff",
    "home_away_strength_diff",
    "rest_days_diff",
    "fixture_congestion_diff",
)


FEATURE_CONTRACTS: dict[str, tuple[FeatureSpec, ...]] = {
    "nba": (
        FeatureSpec("market_spread_points", "Home-team full-game spread at the modeled price snapshot", "current_odds"),
        FeatureSpec("market_home_no_vig", "Home cover no-vig probability from matching current books", "current_odds"),
        FeatureSpec("net_rating_diff", "Home minus away adjusted net rating", "statistics_analytics"),
        FeatureSpec("recent5_margin_diff", "Home minus away average point margin over last five", "statistics_analytics"),
        FeatureSpec("recent10_margin_diff", "Home minus away average point margin over last ten", "statistics_analytics"),
        FeatureSpec("rest_days_diff", "Home minus away rest days", "official_status"),
        FeatureSpec("lineup_strength_diff", "Home minus away expected lineup strength", "lineup_or_participants"),
        FeatureSpec("availability_impact_diff", "Home minus away quantified injury/availability impact", "injury_availability"),
        FeatureSpec("travel_load_diff", "Home minus away documented travel load", "official_status"),
    ),
    "wnba": (
        FeatureSpec("market_spread_points", "Home-team full-game spread at the modeled price snapshot", "current_odds"),
        FeatureSpec("market_home_no_vig", "Home cover no-vig probability from matching current books", "current_odds"),
        FeatureSpec("net_rating_diff", "Home minus away adjusted net rating", "statistics_analytics"),
        FeatureSpec("recent5_margin_diff", "Home minus away average point margin over last five", "statistics_analytics"),
        FeatureSpec("recent10_margin_diff", "Home minus away average point margin over last ten", "statistics_analytics"),
        FeatureSpec("rest_days_diff", "Home minus away rest days", "official_status"),
        FeatureSpec("lineup_strength_diff", "Home minus away expected lineup strength", "lineup_or_participants"),
        FeatureSpec("availability_impact_diff", "Home minus away quantified injury/availability impact", "injury_availability"),
        FeatureSpec("travel_load_diff", "Home minus away documented travel load", "official_status"),
    ),
    "epl": (
        FeatureSpec("market_home_no_vig", "Home 1X2 no-vig probability", "current_odds"),
        FeatureSpec("market_draw_no_vig", "Draw 1X2 no-vig probability", "current_odds"),
        FeatureSpec("market_away_no_vig", "Away 1X2 no-vig probability", "current_odds"),
        FeatureSpec("xg_diff", "Home minus away expected-goals production", "statistics_analytics"),
        FeatureSpec("xga_diff", "Away minus home expected-goals allowed advantage", "statistics_analytics"),
        FeatureSpec("recent5_points_diff", "Home minus away league points over last five", "statistics_analytics"),
        FeatureSpec("home_away_strength_diff", "Home home-strength minus away away-strength", "statistics_analytics"),
        FeatureSpec("availability_impact_diff", "Home minus away injury/suspension impact", "injury_availability"),
        FeatureSpec("rest_days_diff", "Home minus away rest days", "official_status"),
        FeatureSpec("fixture_congestion_diff", "Home minus away recent schedule congestion", "official_status"),
    ),
    "mma": (
        FeatureSpec("market_home_no_vig", "First-listed fighter no-vig winner probability", "current_odds"),
        FeatureSpec("age_diff", "First-listed fighter age minus opponent age", "statistics_analytics"),
        FeatureSpec("reach_diff", "First-listed fighter reach minus opponent reach", "statistics_analytics"),
        FeatureSpec("strike_differential_diff", "Difference in significant-strike differential", "statistics_analytics"),
        FeatureSpec("takedown_rate_diff", "Difference in takedown rate", "statistics_analytics"),
        FeatureSpec("takedown_defense_diff", "Difference in takedown defense", "statistics_analytics"),
        FeatureSpec("submission_rate_diff", "Difference in submission attempt rate", "statistics_analytics"),
        FeatureSpec("layoff_days_diff", "Difference in days since last fight", "official_status"),
        FeatureSpec("short_notice_diff", "Difference in short-notice replacement indicator", "transactions_or_changes"),
        FeatureSpec("weight_miss_diff", "Difference in current weigh-in miss indicator", "official_status"),
    ),
    "atp": (
        FeatureSpec("market_home_no_vig", "First-listed player no-vig match-winner probability", "current_odds"),
        FeatureSpec("ranking_diff", "Opponent ranking minus first-listed player ranking", "statistics_analytics"),
        FeatureSpec("surface_elo_diff", "First-listed minus opponent surface Elo", "statistics_analytics"),
        FeatureSpec("serve_points_won_diff", "Difference in surface-adjusted serve points won", "statistics_analytics"),
        FeatureSpec("return_points_won_diff", "Difference in surface-adjusted return points won", "statistics_analytics"),
        FeatureSpec("recent_form_diff", "Difference in opponent-adjusted recent form", "statistics_analytics"),
        FeatureSpec("rest_days_diff", "First-listed minus opponent rest days", "official_status"),
        FeatureSpec("travel_load_diff", "Difference in documented recent travel load", "official_status"),
        FeatureSpec("retirement_risk_diff", "Difference in documented recent retirement/injury risk", "injury_availability"),
    ),
    "wta": (
        FeatureSpec("market_home_no_vig", "First-listed player no-vig match-winner probability", "current_odds"),
        FeatureSpec("ranking_diff", "Opponent ranking minus first-listed player ranking", "statistics_analytics"),
        FeatureSpec("surface_elo_diff", "First-listed minus opponent surface Elo", "statistics_analytics"),
        FeatureSpec("serve_points_won_diff", "Difference in surface-adjusted serve points won", "statistics_analytics"),
        FeatureSpec("return_points_won_diff", "Difference in surface-adjusted return points won", "statistics_analytics"),
        FeatureSpec("recent_form_diff", "Difference in opponent-adjusted recent form", "statistics_analytics"),
        FeatureSpec("rest_days_diff", "First-listed minus opponent rest days", "official_status"),
        FeatureSpec("travel_load_diff", "Difference in documented recent travel load", "official_status"),
        FeatureSpec("retirement_risk_diff", "Difference in documented recent retirement/injury risk", "injury_availability"),
    ),
}


def feature_contract(sport: str) -> tuple[FeatureSpec, ...]:
    key = sport.casefold().strip()
    try:
        return FEATURE_CONTRACTS[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported feature sport: {sport!r}") from exc


def training_feature_version(sport: str) -> str:
    """Return the approved historical-training contract for one sport."""
    return EPL_BASELINE_FEATURE_VERSION if sport.casefold().strip() == "epl" else FEATURE_VERSION


def required_feature_names(
    sport: str,
    *,
    feature_version: str = FEATURE_VERSION,
) -> tuple[str, ...]:
    key = sport.casefold().strip()
    if feature_version == EPL_BASELINE_FEATURE_VERSION:
        if key != "epl":
            raise ValueError("The EPL baseline feature version is only approved for EPL")
        return EPL_BASELINE_FEATURES
    if feature_version != FEATURE_VERSION:
        raise ValueError(f"Unsupported feature version: {feature_version}")
    return tuple(spec.name for spec in feature_contract(key))


def validate_feature_payload(
    sport: str,
    values: dict[str, float],
    *,
    feature_version: str = FEATURE_VERSION,
) -> dict[str, float]:
    required = set(required_feature_names(sport, feature_version=feature_version))
    supplied = set(values)
    missing = sorted(required - supplied)
    extra = sorted(supplied - required)
    if missing:
        raise ValueError(f"Missing required {sport} features: {', '.join(missing)}")
    if extra:
        raise ValueError(f"Unapproved {sport} features: {', '.join(extra)}")
    normalized = {name: float(values[name]) for name in sorted(required)}
    for name, value in normalized.items():
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError(f"Feature {name} must be finite")
    return normalized


def record_feature_payload(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    sport: str,
    values: dict[str, float],
    cutoff_time: datetime,
    source_by_feature: dict[str, str],
    source_updated_by_feature: dict[str, datetime | None] | None = None,
    collected_at: datetime | None = None,
    feature_version: str = FEATURE_VERSION,
) -> int:
    """Persist one complete sport feature vector with strict source attribution."""
    normalized = validate_feature_payload(
        sport,
        values,
        feature_version=feature_version,
    )
    missing_sources = sorted(set(normalized) - set(source_by_feature))
    if missing_sources:
        raise ValueError(f"Missing feature source attribution: {', '.join(missing_sources)}")
    source_updates = source_updated_by_feature or {}
    for name, value in normalized.items():
        record_feature(
            connection,
            event_id=event_id,
            sport=sport,
            feature_name=name,
            feature_value=value,
            cutoff_time=cutoff_time,
            feature_version=feature_version,
            collected_at=collected_at or cutoff_time,
            source_key=source_by_feature[name],
            source_updated_at=source_updates.get(name),
        )
    return len(normalized)
