#!/usr/bin/env python3
"""Build clean, Telegram-ready status files for each multi-sport result."""

from __future__ import annotations

import argparse
from hashlib import sha256
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sports_analytics_pipeline.multisport_results import pending_result_notifications

SPORT_TITLES = {
    "nba": "🏀 NBA Test Result",
    "wnba": "🏀 WNBA Test Result",
    "epl": "⚽ EPL / Soccer Test Result",
    "mma": "🥊 MMA / UFC Test Result",
    "atp": "🎾 ATP Tennis Test Result",
    "wta": "🎾 WTA Tennis Test Result",
}

SPORT_SHORT_LABELS = {
    "nba": "🏀 NBA",
    "wnba": "🏀 WNBA",
    "epl": "⚽ EPL",
    "mma": "🥊 MMA / UFC",
    "atp": "🎾 ATP",
    "wta": "🎾 WTA",
}

MARKET_LABELS = {
    "spreads": "Full-Game Spread",
    "totals": "Full-Game Total",
    "match_moneyline": "Match Moneyline (ML)",
    "three_way_moneyline": "3-Way Moneyline (ML)",
    "h2h": "Moneyline (ML)",
}

MONEYLINE_MARKETS = {"moneylines", "match_moneyline", "three_way_moneyline", "h2h"}

BLOCKER_LABELS = {
    "official_status": "Official match status could not be verified.",
    "injury_availability": "Required injury or availability information could not be verified.",
    "lineup_or_participants": "Required lineup or participant information could not be verified.",
    "weather_environment": "Required weather or environment information could not be verified.",
    "rest_travel": "Required rest or travel information could not be verified.",
    "statistics_analytics": "Required statistics or analytics information could not be verified.",
    "transactions_or_changes": "Required roster or team-change information could not be verified.",
    "current_odds": "Fresh current odds could not be verified.",
    "weigh_in_status": "Required weigh-in information could not be verified.",
}

from sports_analytics_pipeline.revised_strategy import pick_line

UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")


def parse_sports(value: str) -> tuple[str, ...]:
    sports = tuple(dict.fromkeys(item.strip().casefold() for item in value.split(",") if item.strip()))
    invalid = [sport for sport in sports if sport not in SPORT_TITLES]
    if invalid:
        raise SystemExit(f"Unsupported Telegram sport(s): {', '.join(invalid)}")
    return sports


def _display_value(value: Any, fallback: str = "Unavailable") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def _percent(value: Any, *, suffix: str = "%") -> str:
    if value is None:
        return "Unavailable"
    try:
        return f"{float(value):.2f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_start(value: Any) -> str | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return parsed.astimezone(EASTERN).strftime("%a %b %-d, %-I:%M %p ET")


def _plain_blocker(value: str | None) -> str | None:
    if not value:
        return None
    key = value.split(":", 1)[0].strip()
    return BLOCKER_LABELS.get(key, value.replace("_", " ").replace(":missing", " could not be verified."))


def database_event_lookup(database_path: str | Path | None) -> Callable[[str], dict[str, Any] | None]:
    path = Path(database_path) if database_path else None

    def lookup(event_id: str) -> dict[str, Any] | None:
        if path is None or not path.exists():
            return None
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT e.event_id, e.commence_time, e.status AS event_status,
                       home.full_name AS home_name,
                       away.full_name AS away_name
                FROM multisport_events e
                JOIN multisport_participants home ON home.participant_id = e.home_participant_id
                JOIN multisport_participants away ON away.participant_id = e.away_participant_id
                WHERE e.event_id=?
                """,
                (event_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            connection.close()
        return dict(row) if row else None

    return lookup


def _status_for(payload: dict[str, Any], selection: dict[str, Any] | None) -> tuple[str, str | None]:
    if selection and selection.get("decision_type") != "pass":
        if selection.get("confidence_label") == "Unqualified Test Observation":
            return "TEST CAPTURED", str(selection.get("pass_reason") or "")
        return "READY", None

    readiness = payload.get("next_event_live_readiness")
    if isinstance(readiness, dict):
        blockers = readiness.get("blockers")
        ready = readiness.get("ready")
        if ready is True or str(readiness.get("status", "")).casefold() == "ready":
            return "READY", None
        if isinstance(blockers, (list, tuple)) and blockers:
            return "PASS", _plain_blocker(str(blockers[0]))
        if blockers:
            return "PASS", _plain_blocker(str(blockers))

    if payload.get("model_status") == "not_trained":
        return "COLLECTING", "Sport-specific model is not trained yet."
    return "PASS", "No qualifying selection was available in this completed run."


def _selection_for(status: dict[str, Any], sport: str) -> dict[str, Any] | None:
    sport_statuses = status.get("sports")
    if isinstance(sport_statuses, dict):
        payload = sport_statuses.get(sport)
        if isinstance(payload, dict):
            sport_display = payload.get("best_current_paper_selection_display")
            if isinstance(sport_display, dict):
                return sport_display
    display = status.get("best_current_paper_selection_display")
    if not isinstance(display, dict):
        return None
    if str(display.get("sport", "")).casefold() != sport:
        return None
    return display


def _selection_is_current(
    selection: dict[str, Any],
    *,
    event_lookup: Callable[[str], dict[str, Any] | None],
    horizon_minutes: int,
    odds_stale_after_minutes: int,
    as_of: datetime,
) -> bool:
    """Fail closed unless the lean is upcoming, unsettled, and backed by fresh odds."""
    event_id = str(selection.get("event_id") or "")
    details = event_lookup(event_id) if event_id else None
    details = details if isinstance(details, dict) else {}

    event_time = _parse_time(
        selection.get("event_commence_time") or details.get("commence_time")
    )
    event_status = str(
        selection.get("event_status") or details.get("event_status") or ""
    ).casefold()
    decision_time = _parse_time(selection.get("check_time"))
    odds_time = _parse_time(selection.get("odds_collected_at"))
    odds_status = str(selection.get("odds_market_status") or "").casefold()
    settled = selection.get("selection_settled") in (True, 1, "1", "true", "yes")

    fresh_after = as_of - timedelta(minutes=odds_stale_after_minutes)
    horizon_end = as_of + timedelta(minutes=horizon_minutes)
    return bool(
        not settled
        and event_status == "scheduled"
        and event_time is not None
        and as_of < event_time <= horizon_end
        and decision_time is not None
        and fresh_after <= decision_time <= as_of
        and odds_status == "open"
        and odds_time is not None
        and fresh_after <= odds_time <= as_of
    )


def _current_selection_for(
    status: dict[str, Any],
    sport: str,
    *,
    event_lookup: Callable[[str], dict[str, Any] | None],
    horizon_minutes: int,
    odds_stale_after_minutes: int,
    as_of: datetime,
) -> tuple[dict[str, Any] | None, bool]:
    candidate = _selection_for(status, sport)
    if candidate is None:
        return None, False
    if _selection_is_current(
        candidate,
        event_lookup=event_lookup,
        horizon_minutes=horizon_minutes,
        odds_stale_after_minutes=odds_stale_after_minutes,
        as_of=as_of,
    ):
        return candidate, False
    return None, True


def _market_label(selection: dict[str, Any]) -> str:
    market_key = str(selection.get("market_key") or "").casefold()
    if market_key == "moneylines":
        return "Fight Moneyline (ML)" if str(selection.get("sport")).casefold() == "mma" else "Moneyline (ML)"
    if market_key in MARKET_LABELS:
        return MARKET_LABELS[market_key]
    if not market_key:
        return "Unavailable"
    return market_key.replace("_", " ").title()


def _format_line(value: Any, *, signed: bool) -> str:
    if value is None or value == "":
        return "Unavailable"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == 0:
        return "PK" if signed else "0"
    rendered = f"{number:g}"
    return f"+{rendered}" if signed and number > 0 else rendered


def _selection_name(selection: dict[str, Any], details: dict[str, Any] | None) -> str:
    stored_name = selection.get("selection_name")
    if stored_name:
        return str(stored_name)
    role = str(selection.get("outcome_role") or "").casefold()
    if details and role in {"home", "away"}:
        event_name = details.get(f"{role}_name")
        if event_name:
            return str(event_name)
    if role == "draw":
        return "Draw"
    if role in {"over", "under"}:
        return role.title()
    return _display_value(selection.get("outcome_role"))


def _subject_label(selection: dict[str, Any]) -> str:
    role = str(selection.get("outcome_role") or "").casefold()
    if role in {"draw", "over", "under"}:
        return "Selection"
    sport = str(selection.get("sport") or "").casefold()
    if sport == "mma":
        return "Fighter"
    if sport in {"atp", "wta"}:
        return "Player"
    if sport in {"nba", "wnba", "epl"}:
        return "Team"
    return "Selection"


def _lean_text(selection: dict[str, Any], details: dict[str, Any] | None = None) -> str:
    name = _selection_name(selection, details)
    market_key = str(selection.get("market_key") or "").casefold()
    if market_key in MONEYLINE_MARKETS:
        return f"{name} ML"
    if market_key == "spreads":
        return f"{name} {_format_line(selection.get('points'), signed=True)}"
    if market_key == "totals":
        return f"{name} {_format_line(selection.get('points'), signed=False)}"
    if selection.get("points") is not None:
        return f"{name} {_format_line(selection.get('points'), signed=False)}"
    return name


def _event_lines(details: dict[str, Any] | None) -> list[str]:
    if not details:
        return []
    home = details.get("home_name")
    away = details.get("away_name")
    lines: list[str] = []
    if home and away:
        lines.append(f"Match: {home} vs {away}")
    start = _format_start(details.get("commence_time"))
    if start:
        lines.append(f"Start: {start}")
    return lines


def _format_selection(
    lines: list[str],
    selection: dict[str, Any],
    event_lookup: Callable[[str], dict[str, Any] | None],
) -> None:
    event_id = str(selection.get("event_id") or "")
    details = event_lookup(event_id) if event_id else None
    selection_name = _selection_name(selection, details)
    row = {**selection, **(details or {}), "selection_name": selection_name}
    lines.extend(["", pick_line(row)])
    market_key = str(selection.get("market_key") or "").casefold()
    lines.extend(
        [
            "",
            f"🚨 BOT LEAN: {_lean_text(selection, details).upper()}",
            f"Market: {_market_label(selection)}",
            f"{_subject_label(selection)}: {selection_name}",
        ]
    )
    if market_key == "spreads":
        lines.append(f"Line: {_format_line(selection.get('points'), signed=True)}")
    elif market_key == "totals":
        lines.append(f"Line: {_format_line(selection.get('points'), signed=False)}")
    role = str(selection.get("outcome_role") or "").casefold()
    if role in {"home", "away"}:
        lines.append(f"Matchup Position: {role.upper()}")
    event_lines = _event_lines(details)
    lines.extend(event_lines or [f"Event: {_display_value(selection.get('event_id'))}"])
    if selection.get("confidence_label") == "Unqualified Test Observation":
        lines.extend(
            [
                "Qualified Selection: NO",
                "Test Record: CAPTURED ANYWAY",
                "Counts Toward Training/OOS: NO",
            ]
        )
    lines.extend(
        [
            f"Book: {_display_value(selection.get('sportsbook'))}",
            f"Odds: {_display_value(selection.get('american_odds'))}",
            f"Model Probability: {_percent(selection.get('model_probability_percent'))}",
            f"Conservative Probability: {_percent(selection.get('conservative_probability_percent'))}",
            f"Conservative Edge: {_percent(selection.get('edge_percentage_points'), suffix=' pp')}",
            f"Expected Value: {_percent(selection.get('expected_value_percent'))}",
            f"Pick Strength: {_display_value(selection.get('pick_strength_score'))}/100",
            f"Grade: {_display_value(selection.get('letter_grade'))}",
            f"Result: {_display_value(selection.get('decision_type')).replace('_', ' ').upper()}",
        ]
    )
    if selection.get("pass_reason"):
        lines.append(f"Why No Pick: {_plain_blocker(str(selection['pass_reason']))}")


def _format_progress(lines: list[str], payload: dict[str, Any]) -> None:
    examples = payload.get("historical_training_examples")
    minimum = payload.get("training_minimum")
    completed = payload.get("completed_oos")
    target = payload.get("validation_target")
    model_status = payload.get("model_status")

    lines.append("")
    if examples is not None and minimum is not None:
        lines.append(f"Training: {examples}/{minimum} verified examples")
    if model_status is not None:
        lines.append(f"Model: {str(model_status).replace('_', ' ').upper()}")
    if completed is not None and target is not None:
        lines.append(f"OOS Validation: {completed}/{target}")


def build_messages(
    status: dict,
    sports: tuple[str, ...],
    *,
    event_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    horizon_minutes: int = 90,
    odds_stale_after_minutes: int = 60,
    as_of: datetime | None = None,
) -> dict[str, str]:
    sport_statuses = status.get("sports") if isinstance(status.get("sports"), dict) else {}
    mode = str(status.get("mode") or "test_only")
    messages: dict[str, str] = {}
    event_lookup = event_lookup or (lambda _event_id: None)
    as_of = (as_of or datetime.now(UTC)).astimezone(UTC)

    for sport in sports:
        raw_payload = sport_statuses.get(sport)
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        selection, expired_selection = _current_selection_for(
            status,
            sport,
            event_lookup=event_lookup,
            horizon_minutes=horizon_minutes,
            odds_stale_after_minutes=odds_stale_after_minutes,
            as_of=as_of,
        )
        result_status, blocker = _status_for(payload, selection)
        has_previous_selection = int(payload.get("paper_decisions") or 0) > 0
        if expired_selection or (selection is None and has_previous_selection):
            result_status = "NO NEW SELECTION"
            blocker = (
                "Previous selections are settled, expired, or outside this run; "
                "this run produced no new lean."
            )
        readiness = payload.get("next_event_live_readiness")
        readiness = readiness if isinstance(readiness, dict) else {}
        event_id = str(readiness.get("event_id") or "")
        details = event_lookup(event_id) if event_id else None
        event_time = _parse_time(details.get("commence_time")) if details else None
        in_horizon = bool(event_time and as_of <= event_time <= as_of + timedelta(minutes=horizon_minutes))

        if not selection and event_time and not in_horizon:
            result_status = "NO ELIGIBLE MATCHES"
            blocker = f"No {sport.upper()} event is inside the {horizon_minutes}-minute evaluation window."

        lines = [f"Mode: {mode}", "", f"Status: {result_status}"]

        if selection:
            _format_selection(lines, selection, event_lookup)
        else:
            if details:
                lines.extend(_event_lines(details))
            lines.append("Qualified Selections: 0")
            if blocker:
                lines.extend(["", f"Why No Pick: {blocker}"])

        _format_progress(lines, payload)
        lines.extend(["", "No real-money action is taken."])
        messages[sport] = "\n".join(lines)

    return messages


def build_multisport_summary(
    status: dict,
    *,
    workflow_status: str = "success",
    event_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    horizon_minutes: int = 90,
    odds_stale_after_minutes: int = 60,
    as_of: datetime | None = None,
) -> str:
    sport_statuses = status.get("sports") if isinstance(status.get("sports"), dict) else {}
    event_lookup = event_lookup or (lambda _event_id: None)
    as_of = (as_of or datetime.now(UTC)).astimezone(UTC)
    best = status.get("best_current_paper_selection_display")
    best = best if isinstance(best, dict) and _selection_is_current(
        best,
        event_lookup=event_lookup,
        horizon_minutes=horizon_minutes,
        odds_stale_after_minutes=odds_stale_after_minutes,
        as_of=as_of,
    ) else None
    mode = str(status.get("mode") or "test_only")

    lines = [f"Mode: {mode}", "", "Sports Status:"]

    for sport in SPORT_TITLES:
        raw_payload = sport_statuses.get(sport)
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        selection, expired_selection = _current_selection_for(
            status,
            sport,
            event_lookup=event_lookup,
            horizon_minutes=horizon_minutes,
            odds_stale_after_minutes=odds_stale_after_minutes,
            as_of=as_of,
        )
        result_status, _ = _status_for(payload, selection)
        has_previous_selection = int(payload.get("paper_decisions") or 0) > 0
        if expired_selection or (selection is None and has_previous_selection):
            result_status = "NO NEW SELECTION"
        lines.append(f"{SPORT_SHORT_LABELS[sport]} — {result_status}")

    lines.extend(["", "Best Current Selection:"])
    if best and best.get("decision_type") != "pass":
        lines.extend(
            [
                f"Sport: {_display_value(best.get('sport')).upper()}",
                f"🚨 Bot Lean: {_lean_text(best).upper()}",
                f"Market: {_market_label(best)}",
                f"{_subject_label(best)}: {_selection_name(best, None)}",
                f"Event: {_display_value(best.get('event_id'))}",
                f"Book: {_display_value(best.get('sportsbook'))}",
                f"Odds: {_display_value(best.get('american_odds'))}",
                f"Edge: {_percent(best.get('edge_percentage_points'), suffix=' pp')}",
                f"EV: {_percent(best.get('expected_value_percent'))}",
                f"Pick Strength: {_display_value(best.get('pick_strength_score'))}/100",
                f"Grade: {_display_value(best.get('letter_grade'))}",
            ]
        )
    else:
        lines.append("None")

    lines.extend(["", "Training / Validation:"])
    for sport in SPORT_TITLES:
        raw_payload = sport_statuses.get(sport)
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        examples = _display_value(payload.get("historical_training_examples"), "0")
        minimum = _display_value(payload.get("training_minimum"), "50")
        completed = _display_value(payload.get("completed_oos"), "0")
        target = _display_value(payload.get("validation_target"), "100")
        lines.append(f"{sport.upper()}: {examples}/{minimum} training | {completed}/{target} OOS")

    automatic_wagering = bool(status.get("automatic_wagering", False))
    lines.extend(
        [
            "",
            f"Automatic wagering: {'ON' if automatic_wagering else 'OFF'}",
            "Test-only. No real-money action is taken.",
        ]
    )
    return "\n".join(lines)



def _american_odds(decimal_odds: Any) -> str:
    try:
        decimal = float(decimal_odds)
    except (TypeError, ValueError):
        return "Unavailable"
    if decimal <= 1:
        return "Unavailable"
    american = round((decimal - 1) * 100) if decimal >= 2 else round(-100 / (decimal - 1))
    return f"+{american}" if american > 0 else str(american)


def build_settled_result_message(result: dict[str, Any]) -> str:
    """Build one unmistakable postgame result for a recorded test selection."""
    grade = str(result["grading_result"]).upper()
    icon = {"WIN": "✅", "LOSS": "❌", "PUSH": "➖", "VOID": "↩️"}[grade]
    sport = str(result["sport"]).casefold()
    selection = {
        "sport": sport,
        "market_key": result.get("market_key"),
        "outcome_role": result.get("outcome_role"),
        "selection_name": result.get("selection_name"),
        "points": result.get("points"),
    }
    home_name = _display_value(result.get("home_name"))
    away_name = _display_value(result.get("away_name"))
    score = "Not applicable"
    if result.get("home_score") is not None and result.get("away_score") is not None:
        score = (
            f"{home_name} {_format_line(result['home_score'], signed=False)} — "
            f"{away_name} {_format_line(result['away_score'], signed=False)}"
        )
    elif result.get("winner_role") in {"home", "away"}:
        winner_key = f"{result['winner_role']}_name"
        score = f"Official winner: {_display_value(result.get(winner_key))}"
    record = result.get("record") if isinstance(result.get("record"), dict) else {}
    units = float(result.get("paper_profit_loss") or 0.0)
    dollars = float(result.get("paper_profit_loss_dollars") or 0.0)
    unit_text = f"{units:+.2f} units"
    dollar_text = "$" + f"{abs(dollars):.2f}"
    if dollars > 0:
        dollar_text = f"+{dollar_text}"
    elif dollars < 0:
        dollar_text = f"-{dollar_text}"
    else:
        dollar_text = "$0.00"
    settled = _format_start(result.get("settled_at")) or _display_value(result.get("settled_at"))
    lines = [
        f"{icon} {grade} {1 if grade == 'WIN' else 0}/1",
        f"Sport: {SPORT_SHORT_LABELS.get(sport, sport.upper())}",
        "",
        f"BOT PICK: {_lean_text(selection).upper()}",
        f"Market: {_market_label(selection)}",
        f"{_subject_label(selection)}: {_selection_name(selection, None)}",
    ]
    if str(result.get("market_key")).casefold() == "spreads":
        lines.append(f"Line: {_format_line(result.get('points'), signed=True)}")
    lines.extend(
        [
            pick_line({**result, "selection_name": result.get("selection_name")}, grade=grade),
            f"Final: {score}",
            f"Official Status: {_display_value(result.get('result_status')).replace('_', ' ').upper()}",
            f"Book: {_display_value(result.get('sportsbook_key'))}",
            f"Odds: {_american_odds(result.get('decimal_odds'))}",
            "",
            f"Paper P/L: {unit_text} ({dollar_text})",
            (
                "Running Test Record: "
                f"{int(record.get('win', 0))}-{int(record.get('loss', 0))}-"
                f"{int(record.get('push', 0))}"
                f" | Voids: {int(record.get('void', 0))}"
            ),
            f"Counts Toward Training/OOS: {'YES' if result.get('countable_oos') else 'NO'}",
            f"Result Source: {_display_value(result.get('source_key'))}",
            f"Settled: {settled}",
            "",
            "TEST ONLY — No real-money action was taken.",
        ]
    )
    return "\n".join(lines)


def write_settled_messages(database_path: str | Path | None, out_dir: str | Path) -> list[str]:
    """Write each unsent result plus an ID sidecar used after successful delivery."""
    path = Path(database_path) if database_path else None
    if path is None or not path.exists():
        return []
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        pending = pending_result_notifications(connection, channel="telegram")
    finally:
        connection.close()
    files: list[str] = []
    for result in pending:
        decision_id = str(result["paper_decision_id"])
        slug = sha256(decision_id.encode("utf-8")).hexdigest()[:16]
        message_path = destination / f"{slug}.txt"
        message_path.write_text(build_settled_result_message(result) + "\n", encoding="utf-8")
        (destination / f"{slug}.id").write_text(decision_id + "\n", encoding="utf-8")
        files.append(message_path.name)
    return files

def main() -> None:
    parser = argparse.ArgumentParser(description="Build clean Telegram messages for our multi-sport results")
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--sports", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--summary-file")
    parser.add_argument("--workflow-status", default="success")
    parser.add_argument("--database")
    parser.add_argument("--horizon-minutes", type=int, default=90)
    parser.add_argument("--odds-stale-after-minutes", type=int, default=60)
    parser.add_argument("--settled-out-dir")
    args = parser.parse_args()

    status_path = Path(args.status_file)
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    if not isinstance(status, dict):
        raise SystemExit("Multi-sport status must be a JSON object")

    sports = parse_sports(args.sports)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lookup = database_event_lookup(args.database)
    for sport, message in build_messages(
        status,
        sports,
        event_lookup=lookup,
        horizon_minutes=args.horizon_minutes,
        odds_stale_after_minutes=args.odds_stale_after_minutes,
    ).items():
        (out_dir / f"{sport}.txt").write_text(message + "\n", encoding="utf-8")

    settled_files = write_settled_messages(args.database, args.settled_out_dir) if args.settled_out_dir else []

    if args.summary_file:
        Path(args.summary_file).write_text(
            build_multisport_summary(
                status,
                workflow_status=args.workflow_status,
                event_lookup=lookup,
                horizon_minutes=args.horizon_minutes,
                odds_stale_after_minutes=args.odds_stale_after_minutes,
            ) + "\n",
            encoding="utf-8",
        )

    print(json.dumps({"sports": list(sports), "files": [f"{sport}.txt" for sport in sports], "settled_files": settled_files}, sort_keys=True))


if __name__ == "__main__":
    main()

