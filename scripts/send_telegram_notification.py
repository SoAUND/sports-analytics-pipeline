from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TELEGRAM_LIMIT = 4096


def _load_credentials() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit("Telegram credentials are not configured")
    return token, chat_id


def _extract_json_from_markdown(text: str) -> dict[str, Any] | None:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _format_selection(selection: Any) -> str | None:
    if not isinstance(selection, dict):
        return None
    game = selection.get("game") or selection.get("event") or selection.get("game_id")
    pick = selection.get("pick") or selection.get("selection") or selection.get("recommended_pick")
    sportsbook = selection.get("sportsbook") or selection.get("bookmaker")
    odds = selection.get("odds") or selection.get("american_odds") or selection.get("price")
    score = selection.get("pick_strength_score") or selection.get("score")
    grade = selection.get("grade") or selection.get("letter_grade")

    parts: list[str] = []
    if game:
        parts.append(str(game))
    if pick:
        parts.append(str(pick))
    if sportsbook or odds:
        market = " ".join(str(value) for value in (sportsbook, odds) if value not in (None, ""))
        if market:
            parts.append(market)
    if score is not None or grade:
        strength = " / ".join(str(value) for value in (score, grade) if value not in (None, ""))
        if strength:
            parts.append(f"Grade: {strength}")
    return " — ".join(parts) if parts else None


def _format_mlb_status(path: Path, run_url: str) -> str:
    text = path.read_text(encoding="utf-8")
    payload = _extract_json_from_markdown(text)
    if payload is None:
        return _truncate(f"⚾ MLB Test Result\n\n{text}\n\nRun: {run_url}")

    live = payload.get("live_readiness") if isinstance(payload.get("live_readiness"), dict) else {}
    progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
    decisions = payload.get("run_decisions") if isinstance(payload.get("run_decisions"), dict) else {}
    operations = payload.get("operations") if isinstance(payload.get("operations"), dict) else {}

    status = payload.get("result_status") or payload.get("status") or live.get("status") or operations.get("status") or payload.get("workflow_status") or "unknown"
    operation_date = payload.get("operation_date") or "unknown"
    scheduled = live.get("scheduled_games", 0)
    ready = live.get("ready_games", 0)
    blocked = live.get("blocked_games", 0)
    selections = decisions.get("test_only_selections", live.get("test_only_selections_seen", 0))
    passes = decisions.get("passes", live.get("passes_seen", 0))
    recorded = progress.get("recorded_regular_season_decisions", 0)
    remaining = progress.get("remaining_to_review")
    paper_profit = progress.get("paper_profit_dollars")
    first_blocker = live.get("first_blocker")

    lines = [
        "⚾ MLB Test Result",
        f"Date: {operation_date}",
        f"Status: {str(status).upper()}",
        f"Games: {scheduled} scheduled | {ready} ready | {blocked} blocked",
        f"Selections: {selections} | Passes: {passes}",
        f"Validation: {recorded}/100 decisions",
    ]
    if remaining is not None:
        lines.append(f"Remaining: {remaining}")
    if paper_profit is not None:
        lines.append(f"Paper profit: ${float(paper_profit):.2f}")
    if first_blocker:
        lines.append(f"Main blocker: {first_blocker}")

    selection_rows = decisions.get("selections")
    if isinstance(selection_rows, list) and selection_rows:
        lines.append("")
        lines.append("Top selections:")
        for row in selection_rows[:3]:
            formatted = _format_selection(row)
            if formatted:
                lines.append(f"• {formatted}")

    lines.extend(["", "Test-only. No real-money action is taken.", f"Run: {run_url}"])
    return _truncate("\n".join(lines))


def _format_test_status(path: Path, outcome: str, run_url: str) -> str:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    count_match = re.findall(r"Ran\s+(\d+)\s+tests?", text)
    count = count_match[-1] if count_match else "unknown"
    failed = re.findall(r"FAILED\s*\((.*?)\)", text)
    detail = failed[-1] if failed else "0 reported failures" if outcome == "success" else "see workflow run"
    return _truncate(
        "\n".join(
            [
                "🧪 Software Test Result",
                f"Status: {outcome.upper()}",
                f"Tests run: {count}",
                f"Details: {detail}",
                f"Run: {run_url}",
            ]
        )
    )


def _format_file(title: str, path: Path, run_url: str) -> str:
    text = path.read_text(encoding="utf-8") if path.exists() else "No status output was produced."
    return _truncate(f"{title}\n\n{text}\n\nRun: {run_url}")


def _truncate(message: str) -> str:
    if len(message) <= TELEGRAM_LIMIT:
        return message
    suffix = "\n\n…truncated. Open the GitHub run for the full result."
    return message[: TELEGRAM_LIMIT - len(suffix)] + suffix


def _telegram_request(method: str, fields: dict[str, Any]) -> dict[str, Any]:
    token, _ = _load_credentials()
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8")
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 3:
                raise SystemExit(f'Telegram API rejected {method}: HTTP {exc.code}') from None
            try:
                retry_after = int(json.loads(exc.read().decode('utf-8')).get('parameters',{}).get('retry_after',30))
            except (ValueError,TypeError,UnicodeDecodeError):
                retry_after = 30
            time.sleep(min(max(retry_after,1),60)+1)
    result = json.loads(body)
    if not result.get("ok"):
        raise SystemExit(f"Telegram API rejected {method}")
    return result


def send_message(message: str) -> str:
    """Send a message and return Telegram's message id.

    Existing callers may ignore the return value, preserving backwards compatibility.
    """
    _, chat_id = _load_credentials()
    result = _telegram_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "true",
        },
    )
    payload = result.get("result") if isinstance(result.get("result"), dict) else {}
    message_id = payload.get("message_id")
    if message_id is None:
        raise SystemExit("Telegram response did not include a message id")
    return str(message_id)


def edit_message(message: str, message_id: str) -> str:
    """Edit an existing Telegram bot message and return its message id."""
    _, chat_id = _load_credentials()
    result = _telegram_request(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": message,
            "disable_web_page_preview": "true",
        },
    )
    payload = result.get("result") if isinstance(result.get("result"), dict) else {}
    returned_id = payload.get("message_id", message_id)
    return str(returned_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Send safe test-result notifications to Telegram")
    parser.add_argument("--mode", choices=("mlb", "tests", "file"), required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--outcome", default="unknown")
    parser.add_argument("--title", default="🏟️ Multi-Sport Test Result")
    parser.add_argument("--edit-message-id")
    parser.add_argument("--message-id-out")
    args = parser.parse_args()

    path = Path(args.file)
    if args.mode == "mlb":
        message = _format_mlb_status(path, args.run_url)
    elif args.mode == "tests":
        message = _format_test_status(path, args.outcome, args.run_url)
    else:
        message = _format_file(args.title, path, args.run_url)

    if args.edit_message_id:
        message_id = edit_message(message, args.edit_message_id)
        action = "updated"
    else:
        message_id = send_message(message)
        action = "sent"
    if args.message_id_out:
        Path(args.message_id_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.message_id_out).write_text(message_id + "\n", encoding="utf-8")
    print(f"Telegram notification {action}. message_id={message_id}")


if __name__ == "__main__":
    main()
