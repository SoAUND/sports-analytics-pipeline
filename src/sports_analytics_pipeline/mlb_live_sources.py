"""Live-source normalization guards for MLB paper operations.

These helpers solve two source-shape problems without weakening any paper-run
safety gate:

* Official MLB transactions can reference a minor-league affiliate on one side
  of a move.  We retain the transaction only when at least one supplied team
  endpoint resolves to a controlled MLB team.  The affiliate side is stored as
  unknown instead of being falsely mapped to its parent club.
* The Odds API can accept a requested bookmaker key but omit that bookmaker from
  a particular response.  We record requested/returned/missing bookmaker keys
  explicitly.  Missing books remain missing; no substitute price is created.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from .collectors.base import CollectionBatch, TeamResolver
from .collectors.http import HttpTransport
from .collectors.mlb_official import (
    MlbTransactionsBatch,
    STATS_BASE_URL,
    parse_mlb_transactions,
)


_UNMAPPED_TEAM_PREFIX = "__unmapped_mlb_transaction_team__:"


class _AffiliateTolerantResolver:
    """Mark unknown transaction endpoints without inventing an MLB identity."""

    def __init__(self, resolver: TeamResolver):
        self._resolver = resolver

    def resolve(self, source_name: str) -> str:
        try:
            return self._resolver.resolve(source_name)
        except ValueError:
            return _UNMAPPED_TEAM_PREFIX + quote(source_name, safe="")


def _clean_transaction_team(value: str | None) -> tuple[str | None, str | None]:
    if value is None or not value.startswith(_UNMAPPED_TEAM_PREFIX):
        return value, None
    encoded = value[len(_UNMAPPED_TEAM_PREFIX) :]
    # Keep the original source spelling in warnings. URL quoting is used only so
    # a provider name can never collide with a controlled internal MLB team id.
    from urllib.parse import unquote

    return None, unquote(encoded)


def parse_mlb_transactions_with_affiliates(
    payload: dict[str, Any],
    *,
    team_resolver: TeamResolver,
    collected_at: datetime,
    start_date: str,
    end_date: str,
) -> MlbTransactionsBatch:
    """Parse official transactions while safely tolerating one affiliate endpoint.

    A transaction with an unknown/minor-league endpoint is retained only when
    the opposite supplied endpoint is a confirmed MLB team.  If supplied team
    names exist but none resolve to MLB, parsing still fails closed.
    """
    parsed = parse_mlb_transactions(
        payload,
        team_resolver=_AffiliateTolerantResolver(team_resolver),
        collected_at=collected_at,
        start_date=start_date,
        end_date=end_date,
    )
    warnings = list(parsed.warnings)
    records = []
    for item in parsed.records:
        from_team_id, unknown_from = _clean_transaction_team(item.from_team_id)
        to_team_id, unknown_to = _clean_transaction_team(item.to_team_id)
        unknown_names = tuple(name for name in (unknown_from, unknown_to) if name)

        if unknown_names and from_team_id is None and to_team_id is None:
            names = ", ".join(repr(name) for name in unknown_names)
            raise ValueError(
                "MLB transaction has supplied team endpoint(s) but no controlled "
                f"MLB team could be confirmed: {names}"
            )

        if unknown_names:
            warnings.append(
                "MLB transaction for "
                f"{item.player_name!r} referenced unmapped affiliate/non-MLB team "
                f"endpoint(s) {', '.join(repr(name) for name in unknown_names)}; "
                "only the confirmed MLB endpoint is retained."
            )

        records.append(
            replace(item, from_team_id=from_team_id, to_team_id=to_team_id)
        )

    return replace(parsed, records=tuple(records), warnings=tuple(warnings))


class AffiliateAwareMlbTransactionsCollector:
    """Official MLB transaction collector with affiliate-safe normalization."""

    provider_key = "mlb_transactions"

    def __init__(self, *, transport: HttpTransport, team_resolver: TeamResolver):
        self._transport = transport
        self._teams = team_resolver

    def collect_window(
        self, end_date: date | str, *, lookback_days: int = 7
    ) -> MlbTransactionsBatch:
        if lookback_days < 1 or lookback_days > 30:
            raise ValueError("lookback_days must be between 1 and 30")
        if isinstance(end_date, date):
            ending = end_date
        else:
            try:
                ending = date.fromisoformat(str(end_date))
            except ValueError as exc:
                raise ValueError("end_date must use YYYY-MM-DD") from exc
        from datetime import timedelta

        starting = ending - timedelta(days=lookback_days)
        response = self._transport.get(
            f"{STATS_BASE_URL}/transactions",
            params={
                "sportId": "1",
                "startDate": starting.strftime("%m/%d/%Y"),
                "endDate": ending.strftime("%m/%d/%Y"),
                "hydrate": "person",
            },
            headers={"Accept": "application/json", "User-Agent": "OurSportsBot/0.8"},
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("MLB transactions response must be an object")
        return parse_mlb_transactions_with_affiliates(
            payload,
            team_resolver=self._teams,
            collected_at=response.received_at,
            start_date=starting.isoformat(),
            end_date=ending.isoformat(),
        )


def add_bookmaker_response_diagnostics(
    batch: CollectionBatch,
    *,
    requested_bookmakers: tuple[str, ...],
) -> CollectionBatch:
    """Record which explicitly requested bookmaker keys the provider returned.

    This is diagnostic only.  It never aliases, substitutes, copies, or creates
    a price for a missing sportsbook.
    """
    payload = batch.raw_payload
    if not isinstance(payload, list):
        raise ValueError("Current bookmaker diagnostics require an event list")

    returned = sorted(
        {
            str(bookmaker.get("key"))
            for event in payload
            if isinstance(event, dict)
            for bookmaker in (event.get("bookmakers") or [])
            if isinstance(bookmaker, dict) and bookmaker.get("key")
        }
    )
    requested = tuple(dict.fromkeys(str(book) for book in requested_bookmakers if str(book)))
    returned_set = set(returned)
    missing = tuple(book for book in requested if book not in returned_set)

    warnings = list(batch.warnings)
    if missing:
        warnings.append(
            "The Odds API did not return explicitly requested bookmaker key(s): "
            + ", ".join(missing)
        )

    metadata = dict(batch.metadata)
    metadata.update(
        {
            "requested_bookmakers": list(requested),
            "returned_bookmakers": returned,
            "missing_requested_bookmakers": list(missing),
        }
    )
    return replace(batch, warnings=tuple(warnings), metadata=metadata)
