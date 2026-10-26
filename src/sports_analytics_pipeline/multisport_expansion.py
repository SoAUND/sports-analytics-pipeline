"""Future-market and parlay expansion gates.

The project sequence requires each sport's first market to prove itself before totals,
props, alternate lines, live markets, or automated parlay ranking can be activated.
This module makes that sequencing enforceable instead of relying on memory.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from .multisport_runtime import approval_status, ensure_runtime_schema, validation_count
from .sports_registry import get_sport_market, validation_bucket


@dataclass(frozen=True)
class ExpansionReadiness:
    sport: str
    first_market: str
    completed_oos: int
    validation_target: int
    review_status: str
    calibration_status: str
    clv_status: str
    model_approval_status: str
    may_add_second_market: bool
    may_enable_parlay_ranking: bool
    blockers: tuple[str, ...]


def expansion_readiness(
    connection: sqlite3.Connection,
    *,
    sport: str,
    model_version: str | None = None,
) -> ExpansionReadiness:
    ensure_runtime_schema(connection)
    spec = get_sport_market(sport)
    count = validation_count(connection, sport)
    metrics = connection.execute(
        "SELECT * FROM multisport_validation_metrics WHERE validation_bucket=?",
        (validation_bucket(sport),),
    ).fetchone()
    review = str(metrics["review_status"]) if metrics else "collecting"
    calibration = str(metrics["calibration_status"]) if metrics else "untested"
    clv = str(metrics["clv_status"]) if metrics else "unknown"
    model_status = approval_status(connection, sport, model_version) if model_version else "test_only"
    blockers: list[str] = []
    if count < spec.validation_target:
        blockers.append(f"validation:{count}/{spec.validation_target}")
    if review != "reviewed_pass":
        blockers.append(f"review:{review}")
    if calibration != "acceptable":
        blockers.append(f"calibration:{calibration}")
    if clv not in {"stable", "favorable"}:
        blockers.append(f"clv:{clv}")
    if model_status != "approved":
        blockers.append(f"model:{model_status}")
    ready = not blockers
    return ExpansionReadiness(
        sport=sport,
        first_market=spec.internal_market_key,
        completed_oos=count,
        validation_target=spec.validation_target,
        review_status=review,
        calibration_status=calibration,
        clv_status=clv,
        model_approval_status=model_status,
        may_add_second_market=ready,
        may_enable_parlay_ranking=ready,
        blockers=tuple(blockers),
    )


def require_market_expansion_ready(
    connection: sqlite3.Connection,
    *,
    sport: str,
    model_version: str,
) -> None:
    readiness = expansion_readiness(connection, sport=sport, model_version=model_version)
    if not readiness.may_add_second_market:
        raise RuntimeError(
            f"{sport} cannot add another market yet: " + ", ".join(readiness.blockers)
        )


def require_parlay_ready(
    connection: sqlite3.Connection,
    *,
    legs: tuple[tuple[str, str], ...],
) -> None:
    """Require every leg's sport/model to have passed its own first-market review."""
    if len(legs) < 2:
        raise ValueError("A parlay requires at least two legs")
    failures: list[str] = []
    for sport, model_version in legs:
        readiness = expansion_readiness(connection, sport=sport, model_version=model_version)
        if not readiness.may_enable_parlay_ranking:
            failures.append(f"{sport}({';'.join(readiness.blockers)})")
    if failures:
        raise RuntimeError("Parlay ranking is still locked: " + ", ".join(failures))
