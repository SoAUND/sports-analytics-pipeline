"""Test-only registry for the next sports supported by our shared analytics foundation.

This module intentionally contains no real-money execution and no model approval logic.
Each league/tour keeps its own validation record even when infrastructure is shared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SportMarketSpec:
    """One league/tour and the first market approved for paper-test development."""

    sport: str
    display_name: str
    provider_market_key: str
    internal_market_key: str
    period: str
    outcome_count: int
    allows_draw: bool = False
    exact_provider_sport_keys: tuple[str, ...] = ()
    provider_sport_key_prefixes: tuple[str, ...] = ()
    validation_target: int = 100
    test_only: bool = True

    def matches_provider_sport_key(self, provider_key: str) -> bool:
        if provider_key in self.exact_provider_sport_keys:
            return True
        return any(provider_key.startswith(prefix) for prefix in self.provider_sport_key_prefixes)


SPORT_MARKETS: dict[str, SportMarketSpec] = {
    "nba": SportMarketSpec(
        sport="nba",
        display_name="NBA",
        exact_provider_sport_keys=("basketball_nba",),
        provider_market_key="spreads",
        internal_market_key="spreads",
        period="full_game",
        outcome_count=2,
    ),
    "wnba": SportMarketSpec(
        sport="wnba",
        display_name="WNBA",
        exact_provider_sport_keys=("basketball_wnba",),
        provider_market_key="spreads",
        internal_market_key="spreads",
        period="full_game",
        outcome_count=2,
    ),
    "epl": SportMarketSpec(
        sport="epl",
        display_name="Premier League",
        exact_provider_sport_keys=("soccer_epl",),
        provider_market_key="h2h",
        internal_market_key="three_way_moneyline",
        period="full_game",
        outcome_count=3,
        allows_draw=True,
    ),
    "mma": SportMarketSpec(
        sport="mma",
        display_name="UFC/MMA",
        exact_provider_sport_keys=("mma_mixed_martial_arts",),
        provider_market_key="h2h",
        internal_market_key="moneylines",
        period="full_fight",
        outcome_count=2,
    ),
    "atp": SportMarketSpec(
        sport="atp",
        display_name="ATP Tennis",
        provider_sport_key_prefixes=("tennis_atp_",),
        provider_market_key="h2h",
        internal_market_key="match_moneyline",
        period="full_match",
        outcome_count=2,
    ),
    "wta": SportMarketSpec(
        sport="wta",
        display_name="WTA Tennis",
        provider_sport_key_prefixes=("tennis_wta_",),
        provider_market_key="h2h",
        internal_market_key="match_moneyline",
        period="full_match",
        outcome_count=2,
    ),
}


def get_sport_market(sport: str) -> SportMarketSpec:
    """Return a controlled sport definition; unknown sports are rejected."""
    try:
        return SPORT_MARKETS[sport.casefold().strip()]
    except KeyError as exc:
        raise ValueError(f"Unsupported multi-sport key: {sport!r}") from exc


def discover_active_provider_keys(
    sports_payload: list[dict[str, Any]],
    sport: str,
) -> tuple[str, ...]:
    """Find currently active provider keys for one controlled league/tour.

    The Odds API exposes tennis as tournament-specific keys rather than one generic
    ATP or WTA key, so discovery is required before collection. Exact-key sports
    use the same function to verify that the league is currently active.
    """
    spec = get_sport_market(sport)
    matches: list[str] = []
    for row in sports_payload:
        key = str(row.get("key", ""))
        if row.get("active") is not True:
            continue
        if spec.matches_provider_sport_key(key):
            matches.append(key)
    return tuple(sorted(dict.fromkeys(matches)))


def validation_bucket(sport: str) -> str:
    """Return the independent validation bucket for the first market of a sport."""
    spec = get_sport_market(sport)
    return f"{spec.sport}:{spec.internal_market_key}:{spec.period}"
