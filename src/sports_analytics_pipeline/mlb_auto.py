"""Schedule-aware gate for automatic MLB paper runs.

The gate uses the official MLB schedule only to decide whether a full paper
operation should start. It does not create a pick, model run, paper decision,
or real-money action. Scheduled GitHub checks can therefore run frequently without
spending sportsbook-odds requests when no game is near first pitch.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
EASTERN = ZoneInfo("America/New_York")
AUTOMATIC_MIN_MINUTES = 25
AUTOMATIC_MAX_MINUTES = 65
AUTOMATIC_OPERATION_HOURS = 2
AUTOMATIC_COOLDOWN_MINUTES = 20


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must include a time zone")
    return value.astimezone(timezone.utc)


def _operation_date(value: str | None, *, now: datetime) -> str:
    if value:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("operation_date must use YYYY-MM-DD") from exc
        return value
    return _aware_utc(now).astimezone(EASTERN).date().isoformat()


def candidate_schedule_dates(*, now: datetime) -> tuple[str, ...]:
    """Return schedule dates worth checking for a scheduled run.

    After midnight Eastern we also inspect the previous calendar date because a
    late West Coast game can still be approaching first pitch even though the
    Eastern calendar has already rolled over.
    """
    local = _aware_utc(now).astimezone(EASTERN)
    dates = [local.date().isoformat()]
    if local.hour < 3:
        dates.append((local.date() - timedelta(days=1)).isoformat())
    return tuple(dates)


def automatic_cooldown_active(
    latest_success_at: datetime | None,
    *,
    now: datetime,
    cooldown_minutes: int = AUTOMATIC_COOLDOWN_MINUTES,
) -> bool:
    """Return whether a recent successful automatic run should throttle another.

    The cooldown is a resource-control guard only. Missing marker metadata does
    not block a paper run because database uniqueness and all research/model
    safety gates still apply downstream.
    """
    if cooldown_minutes < 1 or cooldown_minutes > 120:
        raise ValueError("cooldown_minutes must be between 1 and 120")
    current = _aware_utc(now)
    if latest_success_at is None:
        return False
    latest = _aware_utc(latest_success_at)
    age = current - latest
    if age.total_seconds() < 0:
        return False
    return age < timedelta(minutes=cooldown_minutes)


def _game_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("MLB gameDate must include a time zone")
    return parsed.astimezone(timezone.utc)


def find_games_in_pregame_window(
    payload: dict[str, Any],
    *,
    now: datetime,
    min_minutes: int = AUTOMATIC_MIN_MINUTES,
    max_minutes: int = AUTOMATIC_MAX_MINUTES,
) -> tuple[dict[str, Any], ...]:
    """Return regular-season preview games inside the automatic pregame window."""
    if min_minutes < 0 or max_minutes <= min_minutes:
        raise ValueError("pregame window must have 0 <= min_minutes < max_minutes")
    current = _aware_utc(now)
    dates = payload.get("dates") or []
    if not isinstance(dates, list):
        raise ValueError("MLB schedule response dates must be a list")

    due: list[dict[str, Any]] = []
    for date_block in dates:
        if not isinstance(date_block, dict):
            continue
        for game in date_block.get("games") or []:
            if not isinstance(game, dict):
                continue
            if str(game.get("gameType") or "R").upper() != "R":
                continue
            status = game.get("status") or {}
            abstract = str(status.get("abstractGameState") or "").casefold()
            detailed = str(status.get("detailedState") or "").casefold()
            if abstract and abstract != "preview":
                continue
            if any(word in detailed for word in ("postponed", "cancelled", "canceled", "suspended")):
                continue
            game_date = str(game.get("gameDate") or "").strip()
            game_pk = game.get("gamePk")
            if not game_date or game_pk is None:
                continue
            start = _game_time(game_date)
            minutes = (start - current).total_seconds() / 60.0
            if minutes < min_minutes or minutes > max_minutes:
                continue
            teams = game.get("teams") or {}
            home = ((teams.get("home") or {}).get("team") or {}).get("name")
            away = ((teams.get("away") or {}).get("team") or {}).get("name")
            due.append(
                {
                    "source_game_id": str(game_pk),
                    "game_time": start.isoformat(),
                    "minutes_to_start": round(minutes, 2),
                    "home_team": home,
                    "away_team": away,
                }
            )
    due.sort(key=lambda item: (item["game_time"], item["source_game_id"]))
    return tuple(due)


def fetch_official_mlb_schedule(
    operation_date: str,
    *,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    """Fetch one official MLB regular-season schedule date with no sportsbook call."""
    if timeout_seconds < 1 or timeout_seconds > 60:
        raise ValueError("timeout_seconds must be between 1 and 60")
    _operation_date(operation_date, now=datetime.now(timezone.utc))
    query = urlencode({"sportId": "1", "gameType": "R", "date": operation_date})
    request = Request(
        f"{MLB_SCHEDULE_URL}?{query}",
        headers={"Accept": "application/json", "User-Agent": "OurSportsBot/0.7"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("MLB schedule response must be an object")
    return payload


def build_automatic_gate(
    *,
    event_name: str,
    now: datetime,
    requested_date: str | None = None,
    requested_mode: str | None = None,
    requested_hours: str | int | None = None,
    requested_attempts: str | int | None = None,
    schedule_payload: dict[str, Any] | None = None,
    latest_auto_success_at: datetime | None = None,
    min_minutes: int = AUTOMATIC_MIN_MINUTES,
    max_minutes: int = AUTOMATIC_MAX_MINUTES,
    cooldown_minutes: int = AUTOMATIC_COOLDOWN_MINUTES,
) -> dict[str, Any]:
    """Resolve manual inputs or decide whether a scheduled paper run is due."""
    operation_date = _operation_date(requested_date or None, now=now)
    if event_name == "schedule":
        if schedule_payload is None:
            raise ValueError("schedule_payload is required for a scheduled gate")
        due = find_games_in_pregame_window(
            schedule_payload,
            now=now,
            min_minutes=min_minutes,
            max_minutes=max_minutes,
        )
        cooldown_active = automatic_cooldown_active(
            latest_auto_success_at,
            now=now,
            cooldown_minutes=cooldown_minutes,
        )
        should_run = bool(due) and not cooldown_active
        if cooldown_active and due:
            notice = (
                "A recent successful automatic MLB paper run is still inside the cooldown, "
                "so this schedule check skips duplicate collection work."
            )
        elif due:
            notice = (
                "An official regular-season MLB game is near first pitch, so the scheduled "
                "test-only paper workflow may proceed. No real-money action is created."
            )
        else:
            notice = (
                "No official regular-season MLB game is inside the automatic pregame window, "
                "so the full paper workflow is skipped."
            )
        return {
            "should_run": should_run,
            "automated": True,
            "run_mode": "paper_run",
            "operation_date": operation_date,
            "hours": AUTOMATIC_OPERATION_HOURS,
            "attempts": 3,
            "pregame_min_minutes": min_minutes,
            "pregame_max_minutes": max_minutes,
            "cooldown_minutes": cooldown_minutes,
            "cooldown_active": cooldown_active,
            "latest_auto_success_at": (
                None if latest_auto_success_at is None else _aware_utc(latest_auto_success_at).isoformat()
            ),
            "due_games": list(due),
            "notice": notice,
        }

    if event_name != "workflow_dispatch":
        raise ValueError("event_name must be schedule or workflow_dispatch")
    mode = str(requested_mode or "preflight")
    if mode not in {"preflight", "bootstrap_model", "paper_run"}:
        raise ValueError("unsupported manual run mode")
    hours = int(requested_hours or 24)
    attempts = int(requested_attempts or 3)
    if hours < 1 or hours > 168:
        raise ValueError("hours must be between 1 and 168")
    if attempts < 1 or attempts > 5:
        raise ValueError("attempts must be between 1 and 5")
    return {
        "should_run": True,
        "automated": False,
        "run_mode": mode,
        "operation_date": operation_date,
        "hours": hours,
        "attempts": attempts,
        "pregame_min_minutes": None,
        "pregame_max_minutes": None,
        "cooldown_minutes": None,
        "cooldown_active": False,
        "latest_auto_success_at": None,
        "due_games": [],
        "notice": "Manual MLB workflow request accepted; existing test-only controls remain active.",
    }
