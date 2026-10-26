"""Bounded, test-only historical collection from Odds-API.io.

The free provider endpoints expose settled events and one-event closing odds. Raw
responses and normalized results are archived, but they are never promoted into a
training example: the feature builder must separately prove a pre-event cutoff and
provide the complete sport-specific feature contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import Any, Iterable

from .http import HttpTransport
from ..multisport_features import training_feature_version


UTC = timezone.utc
PROVIDER_KEY = "odds_api_io"
BASE_URL = "https://api.odds-api.io/v3"
SPORTS_URL = f"{BASE_URL}/sports"
LEAGUES_URL = f"{BASE_URL}/leagues"
EVENTS_URL = f"{BASE_URL}/historical/events"
ODDS_URL = f"{BASE_URL}/historical/odds"
ALL_SPORTS = ("nfl", "mlb", "nba", "wnba", "epl", "mma", "atp", "wta")
PROVIDER_SPORT_DEFAULTS = {
    "nfl": "american-football",
    "mlb": "baseball",
    "nba": "basketball",
    "wnba": "basketball",
    "epl": "football",
    "mma": "mixed-martial-arts",
    "atp": "tennis",
    "wta": "tennis",
}
MARKET_NAMES = {
    "nfl": "Spread",
    "mlb": "ML",
    "nba": "Spread",
    "wnba": "Spread",
    "epl": "ML",
    "mma": "ML",
    "atp": "ML",
    "wta": "ML",
}
EPL_OFFICIAL_VERIFIER_KEY = "football_data_epl_results_archive"


class RequestBudgetReached(RuntimeError):
    pass


@dataclass
class RequestBudget:
    maximum: int
    used: int = 0

    def take(self) -> None:
        if self.maximum < 1:
            raise ValueError("Historical request budget must be positive")
        if self.used >= self.maximum:
            raise RequestBudgetReached("Historical request budget was reached")
        self.used += 1


@dataclass(frozen=True)
class HistoricalEvent:
    sport: str
    provider_sport_slug: str
    league_slug: str
    league_name: str
    source_event_id: str
    event_time: datetime
    home_source_id: str | None
    away_source_id: str | None
    home_name: str
    away_name: str
    home_score: float | None
    away_score: float | None
    result_role: str
    scores: dict[str, Any]
    raw_payload: dict[str, Any]


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Historical timestamps must include a time zone")
    return value.astimezone(UTC).isoformat()


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Provider event date must include a time zone")
    return parsed.astimezone(UTC)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _score(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _result_role(home_score: float | None, away_score: float | None) -> str:
    if home_score is None or away_score is None:
        return "unknown"
    if home_score > away_score:
        return "home"
    if away_score > home_score:
        return "away"
    return "draw"


def parse_historical_events(
    payload: Any,
    *,
    sport: str,
    provider_sport_slug: str,
    expected_league_slug: str,
) -> tuple[HistoricalEvent, ...]:
    if sport not in ALL_SPORTS:
        raise ValueError(f"Unsupported historical sport: {sport}")
    if not isinstance(payload, list):
        raise ValueError("Historical events response must be a list")
    rows: list[HistoricalEvent] = []
    seen: set[str] = set()
    for raw in payload:
        if not isinstance(raw, dict):
            raise ValueError("Historical event row must be an object")
        source_event_id = str(raw.get("id", "")).strip()
        if not source_event_id or source_event_id in seen:
            raise ValueError("Historical event id is missing or duplicated")
        seen.add(source_event_id)
        status = str(raw.get("status", "")).casefold().strip()
        if status != "settled":
            raise ValueError(f"Historical event {source_event_id} is not settled")
        sport_payload = raw.get("sport")
        league_payload = raw.get("league")
        scores = raw.get("scores")
        if not isinstance(sport_payload, dict) or not isinstance(league_payload, dict):
            raise ValueError("Historical event is missing sport or league metadata")
        if not isinstance(scores, dict):
            raise ValueError("Historical event is missing its scores object")
        returned_sport = str(sport_payload.get("slug", "")).strip()
        returned_league = str(league_payload.get("slug", "")).strip()
        if returned_sport != provider_sport_slug or returned_league != expected_league_slug:
            raise ValueError("Historical event does not match the requested sport and league")
        home_name = str(raw.get("home", "")).strip()
        away_name = str(raw.get("away", "")).strip()
        if not home_name or not away_name or home_name.casefold() == away_name.casefold():
            raise ValueError("Historical event has invalid participants")
        home_score = _score(scores.get("home"))
        away_score = _score(scores.get("away"))
        rows.append(
            HistoricalEvent(
                sport=sport,
                provider_sport_slug=provider_sport_slug,
                league_slug=returned_league,
                league_name=str(league_payload.get("name", returned_league)).strip(),
                source_event_id=source_event_id,
                event_time=_timestamp(raw.get("date")),
                home_source_id=str(raw["homeId"]) if raw.get("homeId") is not None else None,
                away_source_id=str(raw["awayId"]) if raw.get("awayId") is not None else None,
                home_name=home_name,
                away_name=away_name,
                home_score=home_score,
                away_score=away_score,
                result_role=_result_role(home_score, away_score),
                scores=scores,
                raw_payload=raw,
            )
        )
    return tuple(rows)


def _words(value: str) -> set[str]:
    normalized = "".join(character if character.isalnum() else " " for character in value.casefold())
    return set(normalized.split())


def _league_matches(sport: str, row: dict[str, Any]) -> bool:
    slug = str(row.get("slug", "")).casefold().strip()
    name = str(row.get("name", "")).casefold().strip()
    words = _words(f"{slug} {name}")
    if sport == "nfl":
        return "nfl" in words or {"super", "bowl"} <= words
    if sport == "mlb":
        return "mlb" in words or {"world", "series"} <= words
    if sport == "nba":
        return "nba" in words and "wnba" not in words
    if sport == "wnba":
        return "wnba" in words
    if sport == "epl":
        return slug in {"england-premier-league", "premier-league"} or (
            "england" in words and "premier" in words and "league" in words
        )
    if sport == "mma":
        return bool(words & {"ufc", "mma"}) or {"ultimate", "fighting"} <= words
    if sport == "atp":
        return "atp" in words
    if sport == "wta":
        return "wta" in words
    return False


def select_leagues(payload: Any, *, sport: str, maximum: int) -> tuple[tuple[str, str], ...]:
    if maximum < 1:
        raise ValueError("maximum leagues must be positive")
    if not isinstance(payload, list):
        raise ValueError("Leagues response must be a list")
    matches: list[tuple[str, str, int]] = []
    for row in payload:
        if not isinstance(row, dict) or not _league_matches(sport, row):
            continue
        slug = str(row.get("slug", "")).strip()
        name = str(row.get("name", "")).strip()
        if not slug or not name:
            continue
        try:
            event_count = int(row.get("eventsCount", 0))
        except (TypeError, ValueError):
            event_count = 0
        matches.append((slug, name, event_count))
    matches.sort(key=lambda row: (-row[2], row[0]))
    return tuple((slug, name) for slug, name, _ in matches[:maximum])


def _sport_slug(payload: Any, sport: str) -> str:
    if not isinstance(payload, list):
        raise ValueError("Sports response must be a list")
    desired = PROVIDER_SPORT_DEFAULTS[sport]
    tokens = {
        "american-football": {"american", "football"},
        "baseball": {"baseball"},
        "basketball": {"basketball"},
        "football": {"football", "soccer"},
        "tennis": {"tennis"},
        "mixed-martial-arts": {"mma", "mixed", "martial"},
    }[desired]
    for row in payload:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug", "")).casefold().strip()
        name = str(row.get("name", "")).casefold().strip()
        words = _words(f"{slug} {name}")
        if sport == "nfl" and (
            slug in {"nfl", "american-football"} or "nfl" in words
        ):
            return str(row.get("slug")).strip()
        if sport == "mlb" and (slug in {"mlb", "baseball"} or "mlb" in words):
            return str(row.get("slug")).strip()
        mma_match = desired == "mixed-martial-arts" and (
            "mma" in words or {"mixed", "martial"} <= words
        )
        if slug == desired or tokens <= words or slug in tokens or mma_match:
            return str(row.get("slug")).strip()
    return desired


def _rotated_sports(
    connection: sqlite3.Connection,
    sports: tuple[str, ...],
) -> tuple[tuple[str, ...], int]:
    """Rotate the first sport after every stored run so a small budget stays fair."""
    prior_runs = int(
        connection.execute("SELECT COUNT(*) FROM multisport_history_runs").fetchone()[0]
    )
    offset = prior_runs % len(sports)
    return sports[offset:] + sports[:offset], offset


def _date_chunks(start: datetime, end: datetime) -> Iterable[tuple[datetime, datetime]]:
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=31), end)
        yield cursor, chunk_end
        cursor = chunk_end


def _insert_raw(
    connection: sqlite3.Connection,
    *,
    sport: str,
    provider_sport_key: str,
    endpoint_name: str,
    collected_at: datetime,
    payload: Any,
) -> None:
    from hashlib import sha256

    encoded = _canonical(payload)
    digest = sha256(encoded.encode("utf-8")).hexdigest()
    connection.execute(
        """
        INSERT OR IGNORE INTO multisport_raw_ingests (
            source_key, sport, provider_sport_key, endpoint_name,
            collected_at, payload_json, content_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (PROVIDER_KEY, sport, provider_sport_key, endpoint_name, _iso(collected_at), encoded, digest),
    )


def _upsert_event(
    connection: sqlite3.Connection,
    event: HistoricalEvent,
    *,
    collected_at: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO multisport_historical_events (
            provider_key, sport, provider_sport_slug, provider_league_slug,
            provider_league_name, source_event_id, event_time, status,
            home_source_id, away_source_id, home_name, away_name,
            home_score, away_score, result_role, scores_json, result_payload_json,
            first_collected_at, last_collected_at, training_eligible, test_only
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'settled', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1)
        ON CONFLICT(provider_key, source_event_id) DO UPDATE SET
            status='settled',
            home_score=excluded.home_score,
            away_score=excluded.away_score,
            result_role=excluded.result_role,
            scores_json=excluded.scores_json,
            result_payload_json=excluded.result_payload_json,
            last_collected_at=excluded.last_collected_at,
            training_eligible=0,
            test_only=1
        """,
        (
            PROVIDER_KEY,
            event.sport,
            event.provider_sport_slug,
            event.league_slug,
            event.league_name,
            event.source_event_id,
            _iso(event.event_time),
            event.home_source_id,
            event.away_source_id,
            event.home_name,
            event.away_name,
            event.home_score,
            event.away_score,
            event.result_role,
            _canonical(event.scores),
            _canonical(event.raw_payload),
            _iso(collected_at),
            _iso(collected_at),
        ),
    )


def _record_odds(
    connection: sqlite3.Connection,
    *,
    event: HistoricalEvent,
    market: str,
    payload: dict[str, Any],
    collected_at: datetime,
) -> None:
    if str(payload.get("id", "")).strip() != event.source_event_id:
        raise ValueError("Historical odds response event id does not match the request")
    connection.execute(
        """
        UPDATE multisport_historical_events
        SET odds_market_name=?, odds_payload_json=?, odds_collected_at=?,
            last_collected_at=?, training_eligible=0, test_only=1
        WHERE provider_key=? AND source_event_id=?
        """,
        (
            market,
            _canonical(payload),
            _iso(collected_at),
            _iso(collected_at),
            PROVIDER_KEY,
            event.source_event_id,
        ),
    )


def _pending_odds_events(
    connection: sqlite3.Connection,
    *,
    sports: tuple[str, ...],
    start: datetime,
    end: datetime,
    verified_epl_only: bool = False,
) -> tuple[HistoricalEvent, ...]:
    pending: list[HistoricalEvent] = []
    for sport in sports:
        verified_clause = ""
        parameters: tuple[Any, ...] = (
            PROVIDER_KEY,
            sport,
            _iso(start),
            _iso(end),
        )
        if verified_epl_only:
            verified_clause = """
              AND EXISTS (
                    SELECT 1
                    FROM multisport_historical_event_verifications AS verification
                    WHERE verification.provider_key=event.provider_key
                      AND verification.source_event_id=event.source_event_id
                      AND verification.sport=event.sport
                      AND verification.verifier_key=?
                      AND verification.verification_status='verified'
              )
            """
            parameters += (EPL_OFFICIAL_VERIFIER_KEY,)
        rows = connection.execute(
            f"""
            SELECT sport, provider_sport_slug, provider_league_slug,
                   provider_league_name, source_event_id, event_time,
                   home_source_id, away_source_id, home_name, away_name,
                   home_score, away_score, result_role, scores_json,
                   result_payload_json
            FROM multisport_historical_events AS event
            WHERE event.provider_key=?
              AND event.sport=?
              AND event.status='settled'
              AND event.odds_payload_json IS NULL
              AND event.event_time >= ?
              AND event.event_time < ?
              {verified_clause}
            ORDER BY event.event_time DESC, event.historical_event_id DESC
            """,
            parameters,
        ).fetchall()
        for row in rows:
            scores = json.loads(row["scores_json"])
            raw_payload = json.loads(row["result_payload_json"])
            if not isinstance(scores, dict) or not isinstance(raw_payload, dict):
                raise ValueError("Stored historical event payload is invalid")
            pending.append(
                HistoricalEvent(
                    sport=row["sport"],
                    provider_sport_slug=row["provider_sport_slug"],
                    league_slug=row["provider_league_slug"],
                    league_name=row["provider_league_name"],
                    source_event_id=row["source_event_id"],
                    event_time=_timestamp(row["event_time"]),
                    home_source_id=row["home_source_id"],
                    away_source_id=row["away_source_id"],
                    home_name=row["home_name"],
                    away_name=row["away_name"],
                    home_score=row["home_score"],
                    away_score=row["away_score"],
                    result_role=row["result_role"],
                    scores=scores,
                    raw_payload=raw_payload,
                )
            )
    return tuple(pending)


def _collect_pending_odds(
    connection: sqlite3.Connection,
    *,
    api_key: str,
    transport: HttpTransport,
    sports: tuple[str, ...],
    start: datetime,
    end: datetime,
    bookmakers: tuple[str, ...],
    maximum: int,
    budget: RequestBudget,
    collected_at: datetime,
    verified_epl_only: bool = False,
) -> int:
    collected = 0
    for event in _pending_odds_events(
        connection,
        sports=sports,
        start=start,
        end=end,
        verified_epl_only=verified_epl_only,
    ):
        if collected >= maximum:
            break
        budget.take()
        market = MARKET_NAMES[event.sport]
        payload = transport.get(
            ODDS_URL,
            params={
                "apiKey": api_key,
                "eventId": event.source_event_id,
                "bookmakers": ",".join(bookmakers),
                "markets": market,
            },
        ).json()
        if not isinstance(payload, dict):
            raise ValueError("Historical odds response must be an object")
        _insert_raw(
            connection,
            sport=event.sport,
            provider_sport_key=event.provider_sport_slug,
            endpoint_name=f"historical_odds:{event.source_event_id}:{market}",
            collected_at=collected_at,
            payload=payload,
        )
        _record_odds(
            connection,
            event=event,
            market=market,
            payload=payload,
            collected_at=collected_at,
        )
        collected += 1
        connection.commit()
    return collected


def collect_historical_data(
    connection: sqlite3.Connection,
    *,
    api_key: str,
    transport: HttpTransport,
    sports: tuple[str, ...],
    start: datetime,
    end: datetime,
    bookmakers: tuple[str, ...] = (),
    include_odds: bool = False,
    max_requests: int = 30,
    max_leagues_per_sport: int = 5,
    max_odds_events: int = 10,
    collected_at: datetime | None = None,
    verified_epl_only: bool = False,
    odds_only: bool = False,
) -> dict[str, Any]:
    """Collect a bounded range and return a safe audit summary."""
    if not api_key.strip():
        raise ValueError("ODDS_API_IO_KEY is required for historical collection")
    invalid = tuple(sport for sport in sports if sport not in ALL_SPORTS)
    if invalid or not sports:
        raise ValueError(f"Unsupported or empty historical sports: {', '.join(invalid)}")
    if len(set(sports)) != len(sports):
        raise ValueError("Historical sports cannot contain duplicates")
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("Historical range requires ordered timezone-aware timestamps")
    if include_odds and not bookmakers:
        raise ValueError("At least one bookmaker is required when historical odds are enabled")
    if verified_epl_only and (not include_odds or sports != ("epl",)):
        raise ValueError(
            "Verified EPL odds mode requires --include-odds and only the epl sport"
        )
    if odds_only and not include_odds:
        raise ValueError("Odds-only mode requires --include-odds")
    if len(bookmakers) > 30:
        raise ValueError("Odds-API.io permits at most 30 bookmakers")
    if max_odds_events < 0:
        raise ValueError("max_odds_events cannot be negative")
    if max_requests < 1 or max_requests > 200:
        raise ValueError("max_requests must be between 1 and 200")
    if max_leagues_per_sport < 1 or max_leagues_per_sport > 25:
        raise ValueError("max_leagues_per_sport must be between 1 and 25")
    if max_odds_events > 100:
        raise ValueError("max_odds_events cannot exceed 100")

    now = (collected_at or datetime.now(UTC)).astimezone(UTC)
    processing_sports, rotation_offset = _rotated_sports(connection, sports)
    budget = RequestBudget(max_requests)
    run = connection.execute(
        """
        INSERT INTO multisport_history_runs (
            provider_key, requested_sports_json, range_start, range_end,
            bookmakers_json, include_odds, max_requests, status, started_at, test_only
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, 1)
        """,
        (
            PROVIDER_KEY,
            _canonical(list(sports)),
            _iso(start),
            _iso(end),
            _canonical(list(bookmakers)),
            int(include_odds),
            max_requests,
            _iso(now),
        ),
    )
    run_id = int(run.lastrowid)
    connection.commit()
    events_seen = 0
    events_upserted = 0
    odds_collected = 0
    warnings: list[str] = []
    status = "complete"

    try:
        if include_odds and max_odds_events:
            odds_collected += _collect_pending_odds(
                connection,
                api_key=api_key,
                transport=transport,
                sports=processing_sports,
                start=start,
                end=end,
                bookmakers=bookmakers,
                maximum=max_odds_events,
                budget=budget,
                collected_at=now,
                verified_epl_only=verified_epl_only,
            )

        if not odds_only:
            budget.take()
            sports_payload = transport.get(SPORTS_URL, params={"apiKey": api_key}).json()
            for sport in processing_sports:
                provider_sport_slug = _sport_slug(sports_payload, sport)
                budget.take()
                leagues_payload = transport.get(
                    LEAGUES_URL,
                    params={"apiKey": api_key, "sport": provider_sport_slug, "all": "true"},
                ).json()
                leagues = select_leagues(
                    leagues_payload,
                    sport=sport,
                    maximum=max_leagues_per_sport,
                )
                if not leagues:
                    warnings.append(f"No matching historical league was discovered for {sport}")
                    status = "partial"
                    continue
                for league_slug, _league_name in leagues:
                    for chunk_start, chunk_end in _date_chunks(start, end):
                        budget.take()
                        payload = transport.get(
                            EVENTS_URL,
                            params={
                                "apiKey": api_key,
                                "sport": provider_sport_slug,
                                "league": league_slug,
                                "from": _iso(chunk_start),
                                "to": _iso(chunk_end),
                            },
                        ).json()
                        endpoint_name = (
                            f"historical_events:{league_slug}:"
                            f"{chunk_start.date().isoformat()}:{chunk_end.date().isoformat()}"
                        )
                        _insert_raw(
                            connection,
                            sport=sport,
                            provider_sport_key=provider_sport_slug,
                            endpoint_name=endpoint_name,
                            collected_at=now,
                            payload=payload,
                        )
                        parsed = parse_historical_events(
                            payload,
                            sport=sport,
                            provider_sport_slug=provider_sport_slug,
                            expected_league_slug=league_slug,
                        )
                        events_seen += len(parsed)
                        for event in parsed:
                            _upsert_event(connection, event, collected_at=now)
                            events_upserted += 1
                        connection.commit()

            remaining_odds = max_odds_events - odds_collected
            if include_odds and remaining_odds:
                odds_collected += _collect_pending_odds(
                    connection,
                    api_key=api_key,
                    transport=transport,
                    sports=processing_sports,
                    start=start,
                    end=end,
                    bookmakers=bookmakers,
                    maximum=remaining_odds,
                    budget=budget,
                    collected_at=now,
                    verified_epl_only=verified_epl_only,
                )
    except RequestBudgetReached as exc:
        status = "partial"
        warnings.append(str(exc))
    except Exception as exc:
        status = "partial" if events_upserted or odds_collected else "failed"
        warnings.append(str(exc).replace(api_key, "[REDACTED]"))
    finally:
        connection.execute(
            """
            UPDATE multisport_history_runs
            SET requests_made=?, events_seen=?, events_upserted=?,
                odds_events_collected=?, status=?, error_summary=?, completed_at=?
            WHERE history_run_id=?
            """,
            (
                budget.used,
                events_seen,
                events_upserted,
                odds_collected,
                status,
                " | ".join(warnings) or None,
                _iso(now),
                run_id,
            ),
        )
        connection.commit()

    return {
        "history_run_id": run_id,
        "provider": PROVIDER_KEY,
        "status": status,
        "processing_order": list(processing_sports),
        "rotation_offset": rotation_offset,
        "requests_made": budget.used,
        "request_limit": max_requests,
        "events_seen": events_seen,
        "events_upserted": events_upserted,
        "odds_events_collected": odds_collected,
        "verified_epl_odds_only": verified_epl_only,
        "odds_only": odds_only,
        "training_examples_created": 0,
        "warnings": warnings,
        "test_only": True,
    }


def historical_collection_status(connection: sqlite3.Connection) -> dict[str, Any]:
    """Return stored evidence counts and the latest run without exposing raw payloads."""
    training_table_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='multisport_training_examples'
        """
    ).fetchone() is not None
    sports: dict[str, dict[str, int]] = {}
    for sport in ALL_SPORTS:
        feature_version = training_feature_version(sport)
        row = connection.execute(
            """
            SELECT COUNT(*) AS events,
                   SUM(CASE WHEN event.odds_payload_json IS NOT NULL THEN 1 ELSE 0 END) AS odds_events
            FROM multisport_historical_events AS event
            WHERE event.sport=?
            """,
            (sport,),
        ).fetchone()
        training_examples = 0
        if training_table_exists:
            training_examples = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM multisport_training_examples
                    WHERE sport=? AND feature_version=?
                    """,
                    (sport, feature_version),
                ).fetchone()[0]
            )
        sports[sport] = {
            "settled_events": int(row["events"] or 0),
            "events_with_closing_odds": int(row["odds_events"] or 0),
            "training_examples_created": training_examples,
        }
    latest = connection.execute(
        """
        SELECT history_run_id, provider_key, range_start, range_end,
               requests_made, max_requests, events_seen, events_upserted,
               odds_events_collected, status, error_summary, completed_at, test_only
        FROM multisport_history_runs
        ORDER BY history_run_id DESC LIMIT 1
        """
    ).fetchone()
    return {
        "sports": sports,
        "latest_run": dict(latest) if latest is not None else None,
        "automatic_training_promotion": False,
        "test_only": True,
    }
