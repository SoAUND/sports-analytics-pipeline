"""Persistent pacing for paid sports-data requests.

The provider exposes month-to-date quota headers on free and paid responses.  We use
those headers plus a small per-workflow daily ceiling so a frequent scheduler cannot
empty the monthly account.  Unused credits roll forward automatically because each
new UTC day is sized from the credits that actually remain.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Mapping


UTC = timezone.utc


def _integer(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def ensure_quota_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_daily_credit_budgets (
            provider_key TEXT NOT NULL,
            quota_date TEXT NOT NULL,
            starting_remaining INTEGER NOT NULL CHECK (starting_remaining >= 0),
            daily_allowance INTEGER NOT NULL CHECK (daily_allowance >= 0),
            reserve_credits INTEGER NOT NULL CHECK (reserve_credits >= 0),
            created_at TEXT NOT NULL,
            PRIMARY KEY (provider_key, quota_date)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_poll_log (
            provider_key TEXT NOT NULL,
            sport TEXT NOT NULL,
            endpoint_name TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            last_polled_at TEXT NOT NULL,
            request_cost INTEGER,
            requests_remaining INTEGER,
            PRIMARY KEY (provider_key, sport, endpoint_name, scope_key)
        )
        """
    )


@dataclass
class DailyCreditBudget:
    """One provider's safe paid-request allowance for the current UTC day."""

    connection: sqlite3.Connection
    provider_key: str
    quota_date: str
    starting_remaining: int | None
    current_remaining: int | None
    daily_allowance: int
    reserve_credits: int
    disabled_reason: str | None = None

    @classmethod
    def from_metadata(
        cls,
        connection: sqlite3.Connection,
        *,
        provider_key: str,
        metadata: Mapping[str, object],
        as_of: datetime,
        reserve_credits: int,
        daily_credit_cap: int,
    ) -> "DailyCreditBudget":
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must include a time zone")
        if reserve_credits < 0 or daily_credit_cap < 1:
            raise ValueError("Quota reserve must be nonnegative and daily cap must be positive")
        ensure_quota_schema(connection)
        now = as_of.astimezone(UTC)
        quota_date = now.date().isoformat()
        remaining = _integer(metadata.get("requests_remaining"))
        if remaining is None:
            return cls(
                connection=connection,
                provider_key=provider_key,
                quota_date=quota_date,
                starting_remaining=None,
                current_remaining=None,
                daily_allowance=0,
                reserve_credits=reserve_credits,
                disabled_reason="remaining-credit metadata is unavailable",
            )

        days_in_month = monthrange(now.year, now.month)[1]
        days_remaining = days_in_month - now.day + 1
        sustainable_today = max(0, remaining - reserve_credits) // days_remaining
        allowance = min(daily_credit_cap, sustainable_today)
        connection.execute(
            """
            INSERT OR IGNORE INTO provider_daily_credit_budgets (
                provider_key, quota_date, starting_remaining, daily_allowance,
                reserve_credits, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                provider_key,
                quota_date,
                remaining,
                allowance,
                reserve_credits,
                now.isoformat(),
            ),
        )
        row = connection.execute(
            """
            SELECT starting_remaining, daily_allowance, reserve_credits
            FROM provider_daily_credit_budgets
            WHERE provider_key=? AND quota_date=?
            """,
            (provider_key, quota_date),
        ).fetchone()
        return cls(
            connection=connection,
            provider_key=provider_key,
            quota_date=quota_date,
            starting_remaining=int(row[0]),
            current_remaining=remaining,
            daily_allowance=int(row[1]),
            reserve_credits=int(row[2]),
        )

    @property
    def spent_today(self) -> int:
        if self.starting_remaining is None or self.current_remaining is None:
            return 0
        return max(0, self.starting_remaining - self.current_remaining)

    @property
    def available_today(self) -> int:
        if self.current_remaining is None:
            return 0
        return max(
            0,
            min(
                self.daily_allowance - self.spent_today,
                self.current_remaining - self.reserve_credits,
            ),
        )

    def reserve(self, cost: int) -> bool:
        """Reserve expected credits before a request; fail closed when uncertain."""
        if cost < 1:
            raise ValueError("Paid request cost must be positive")
        if self.disabled_reason or cost > self.available_today:
            return False
        assert self.current_remaining is not None
        self.current_remaining -= cost
        return True

    def reconcile(self, metadata: Mapping[str, object] | None) -> None:
        """Replace the estimate with the provider's post-request remaining header."""
        if not metadata:
            return
        remaining = _integer(metadata.get("requests_remaining"))
        if remaining is not None:
            self.current_remaining = min(
                remaining,
                self.current_remaining if self.current_remaining is not None else remaining,
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider_key,
            "quota_date": self.quota_date,
            "starting_remaining": self.starting_remaining,
            "estimated_remaining": self.current_remaining,
            "daily_allowance": self.daily_allowance,
            "spent_today": self.spent_today,
            "available_today": self.available_today,
            "reserve_credits": self.reserve_credits,
            "disabled_reason": self.disabled_reason,
        }


def poll_due(
    connection: sqlite3.Connection,
    *,
    provider_key: str,
    sport: str,
    endpoint_name: str,
    scope_key: str,
    as_of: datetime,
    interval_minutes: int,
) -> bool:
    if interval_minutes < 1:
        raise ValueError("Polling interval must be positive")
    ensure_quota_schema(connection)
    row = connection.execute(
        """
        SELECT last_polled_at FROM provider_poll_log
        WHERE provider_key=? AND sport=? AND endpoint_name=? AND scope_key=?
        """,
        (provider_key, sport, endpoint_name, scope_key),
    ).fetchone()
    if row is None:
        return True
    try:
        last = datetime.fromisoformat(str(row[0])).astimezone(UTC)
    except (TypeError, ValueError):
        return True
    return last <= as_of.astimezone(UTC) - timedelta(minutes=interval_minutes)


def record_poll(
    connection: sqlite3.Connection,
    *,
    provider_key: str,
    sport: str,
    endpoint_name: str,
    scope_key: str,
    polled_at: datetime,
    request_cost: object = None,
    requests_remaining: object = None,
) -> None:
    ensure_quota_schema(connection)
    connection.execute(
        """
        INSERT INTO provider_poll_log (
            provider_key, sport, endpoint_name, scope_key, last_polled_at,
            request_cost, requests_remaining
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider_key, sport, endpoint_name, scope_key) DO UPDATE SET
            last_polled_at=excluded.last_polled_at,
            request_cost=excluded.request_cost,
            requests_remaining=excluded.requests_remaining
        """,
        (
            provider_key,
            sport,
            endpoint_name,
            scope_key,
            polled_at.astimezone(UTC).isoformat(),
            _integer(request_cost),
            _integer(requests_remaining),
        ),
    )
