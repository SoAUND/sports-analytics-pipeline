"""Automatic, review-safe venue name synchronization for all supported sports.

The synchronizer never guesses.  It may link a provider ID to an exact reviewed
name, or accept a new display name for an already-linked provider ID.  A new ID
and new name are reported as pending review and never silently enter the catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, timedelta
import csv
import io
import json
from pathlib import Path
from typing import Any, Callable, Iterable
import urllib.parse
import urllib.request

from .data.seed import MLB_STADIUM_ALIASES, NFLVERSE_STADIUM_ALIASES
from .venue_identity import SUPPORTED_VENUE_SPORTS, normalize_venue_name


MLB_TEAMS_URL = "https://statsapi.mlb.com/api/v1/teams"
ESPN_SCOREBOARD_BASE = "https://site.api.espn.com/apis/site/v2/sports"
NFLVERSE_SCHEDULE_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
ESPN_PATHS = {
    "nfl": "football/nfl",
    "nba": "basketball/nba",
    "wnba": "basketball/wnba",
    "epl": "soccer/eng.1",
    "mma": "mma/ufc",
    "atp": "tennis/atp",
    "wta": "tennis/wta",
}
CATALOG_PATHS = {
    "nfl": "src/sports_analytics_pipeline/data/stadiums.json",
    "mlb": "src/sports_analytics_pipeline/data/mlb_stadiums.json",
    "nba": "src/sports_analytics_pipeline/data/multisport_venues.json",
    "wnba": "src/sports_analytics_pipeline/data/multisport_venues.json",
    "epl": "src/sports_analytics_pipeline/data/multisport_venues.json",
    "mma": "src/sports_analytics_pipeline/data/multisport_venues.json",
    "atp": "src/sports_analytics_pipeline/data/multisport_venues.json",
    "wta": "src/sports_analytics_pipeline/data/multisport_venues.json",
}
TRUSTED_DISPLAY_NAME_PROVIDERS = frozenset({"mlb_statsapi"})


@dataclass(frozen=True)
class VenueObservation:
    sport: str
    provider_key: str
    provider_venue_id: str | None
    venue_name: str
    city: str = ""
    state_or_region: str = ""
    country_code: str = ""
    source_url: str = ""

    @property
    def qualified_provider_id(self) -> str | None:
        if not self.provider_venue_id:
            return None
        return f"{self.provider_key}:{self.provider_venue_id}"


@dataclass(frozen=True)
class SyncAction:
    sport: str
    status: str
    provider_key: str
    provider_venue_id: str | None
    observed_name: str
    venue_id: str | None = None
    previous_name: str | None = None
    city: str = ""
    state_or_region: str = ""
    country_code: str = ""
    source_url: str = ""


def _get_json(url: str, *, params: dict[str, str] | None = None) -> Any:
    query = urllib.parse.urlencode(params or {})
    target = f"{url}?{query}" if query else url
    request = urllib.request.Request(
        target,
        headers={"Accept": "application/json", "User-Agent": "OurSportsBotVenueSync/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_mlb_observations(get_json: Callable[..., Any] = _get_json) -> tuple[VenueObservation, ...]:
    payload = get_json(MLB_TEAMS_URL, params={"sportId": "1", "hydrate": "venue"})
    observations = []
    for team in payload.get("teams", []):
        venue = team.get("venue") or {}
        if venue.get("id") is None or not venue.get("name"):
            continue
        location = venue.get("location") or {}
        observations.append(
            VenueObservation(
                sport="mlb",
                provider_key="mlb_statsapi",
                provider_venue_id=str(venue["id"]),
                venue_name=str(venue["name"]),
                city=str(location.get("city") or ""),
                state_or_region=str(location.get("stateAbbrev") or ""),
                country_code=str(location.get("country") or ""),
                source_url=MLB_TEAMS_URL,
            )
        )
    return tuple(observations)


def fetch_espn_observations(
    sport: str,
    *,
    starting: date,
    ending: date,
    get_json: Callable[..., Any] = _get_json,
) -> tuple[VenueObservation, ...]:
    path = ESPN_PATHS[sport]
    url = f"{ESPN_SCOREBOARD_BASE}/{path}/scoreboard"
    payload = get_json(
        url,
        params={
            "dates": f"{starting:%Y%m%d}-{ending:%Y%m%d}",
            "limit": "1000",
        },
    )
    observations: dict[tuple[str, str], VenueObservation] = {}
    for event in payload.get("events", []):
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        venue = competitions[0].get("venue") or {}
        name = str(venue.get("fullName") or venue.get("name") or "").strip()
        if not name:
            continue
        address = venue.get("address") or {}
        venue_id = str(venue.get("id") or "").strip() or None
        key = (venue_id or "", normalize_venue_name(name))
        observations[key] = VenueObservation(
            sport=sport,
            provider_key="espn",
            provider_venue_id=venue_id,
            venue_name=name,
            city=str(address.get("city") or ""),
            state_or_region=str(address.get("state") or ""),
            country_code=str(address.get("country") or ""),
            source_url=url,
        )
    return tuple(observations.values())


def fetch_nflverse_name_observations(
    *,
    season: int,
    text: str,
) -> tuple[VenueObservation, ...]:
    observations: dict[str, VenueObservation] = {}
    for row in csv.DictReader(io.StringIO(text)):
        if str(row.get("season") or "") != str(season):
            continue
        name = str(row.get("stadium") or "").strip()
        if name:
            observations[normalize_venue_name(name)] = VenueObservation(
                sport="nfl",
                provider_key="nflverse",
                provider_venue_id=None,
                venue_name=name,
                source_url=NFLVERSE_SCHEDULE_URL,
            )
    return tuple(observations.values())


def _row_id(row: dict[str, Any]) -> str:
    return str(row.get("venue_id") or row.get("stadium_id") or "")


def _known_names(row: dict[str, Any]) -> tuple[str, ...]:
    return (str(row["official_name"]), *(str(value) for value in row.get("aliases", [])))


def synchronize_rows(
    rows: list[dict[str, Any]],
    observations: Iterable[VenueObservation],
    *,
    sport: str,
    reviewed_aliases: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], tuple[SyncAction, ...]]:
    """Apply only exact-name links and stable-ID renames to one catalog document."""
    aliases = reviewed_aliases or {}
    name_index: dict[str, dict[str, Any]] = {}
    global_name_index: dict[str, dict[str, Any] | None] = {}
    provider_index: dict[str, dict[str, Any]] = {}
    for row in rows:
        for name in _known_names(row):
            key = normalize_venue_name(name)
            previous = global_name_index.get(key)
            if previous is not None and _row_id(previous) != _row_id(row):
                global_name_index[key] = None
            elif key not in global_name_index:
                global_name_index[key] = row
        if "sports" in row and sport not in row["sports"]:
            continue
        for name in _known_names(row):
            key = normalize_venue_name(name)
            previous = name_index.get(key)
            if previous is not None and _row_id(previous) != _row_id(row):
                raise ValueError(f"Ambiguous {sport} venue name {name!r}")
            name_index[key] = row
        for provider_id in row.get("provider_ids", []):
            provider_index[str(provider_id)] = row
    by_id = {_row_id(row): row for row in rows}
    for alias, venue_id in aliases.items():
        if venue_id in by_id:
            name_index[normalize_venue_name(alias)] = by_id[venue_id]

    actions: list[SyncAction] = []
    for observation in observations:
        if observation.sport != sport:
            continue
        provider_id = observation.qualified_provider_id
        matched = provider_index.get(provider_id or "") if provider_id else None
        if matched is not None:
            known = {normalize_venue_name(name) for name in _known_names(matched)}
            reviewed_venue_id = aliases.get(observation.venue_name)
            if reviewed_venue_id is None:
                normalized_observed = normalize_venue_name(observation.venue_name)
                reviewed_venue_id = next(
                    (
                        venue_id
                        for alias, venue_id in aliases.items()
                        if normalize_venue_name(alias) == normalized_observed
                    ),
                    None,
                )
            if (
                normalize_venue_name(observation.venue_name) in known
                or reviewed_venue_id == _row_id(matched)
            ):
                status = "unchanged"
                previous_name = None
            elif observation.provider_key in TRUSTED_DISPLAY_NAME_PROVIDERS:
                previous_name = str(matched["official_name"])
                aliases_list = [str(value) for value in matched.get("aliases", [])]
                if previous_name not in aliases_list:
                    aliases_list.append(previous_name)
                matched["aliases"] = sorted(set(aliases_list), key=normalize_venue_name)
                matched["official_name"] = observation.venue_name
                matched["verified_on"] = date.today().isoformat()
                matched["source_url"] = observation.source_url or matched.get("source_url")
                status = "safe_name_update"
            else:
                previous_name = None
                aliases_list = [str(value) for value in matched.get("aliases", [])]
                aliases_list.append(observation.venue_name)
                matched["aliases"] = sorted(set(aliases_list), key=normalize_venue_name)
                status = "added_provider_name_alias"
            actions.append(
                SyncAction(
                    sport, status, observation.provider_key, observation.provider_venue_id,
                    observation.venue_name, _row_id(matched), previous_name,
                )
            )
            continue

        normalized_name = normalize_venue_name(observation.venue_name)
        matched = name_index.get(normalized_name)
        added_sport = False
        if matched is None:
            cross_sport_match = global_name_index.get(normalized_name)
            if cross_sport_match is not None and "sports" in cross_sport_match:
                sports = {str(value) for value in cross_sport_match.get("sports", [])}
                sports.add(sport)
                cross_sport_match["sports"] = sorted(sports)
                matched = cross_sport_match
                added_sport = True
        if matched is not None:
            if provider_id:
                provider_ids = {str(value) for value in matched.get("provider_ids", [])}
                provider_ids.add(provider_id)
                matched["provider_ids"] = sorted(provider_ids)
                provider_index[provider_id] = matched
                status = "linked_provider_id_and_sport" if added_sport else "linked_provider_id"
            else:
                status = "matched_reviewed_name_and_sport" if added_sport else "matched_reviewed_name"
            actions.append(
                SyncAction(
                    sport, status, observation.provider_key, observation.provider_venue_id,
                    observation.venue_name, _row_id(matched), None,
                    observation.city, observation.state_or_region,
                    observation.country_code, observation.source_url,
                )
            )
            continue

        actions.append(
            SyncAction(
                sport, "pending_review", observation.provider_key,
                observation.provider_venue_id, observation.venue_name,
                city=observation.city,
                state_or_region=observation.state_or_region,
                country_code=observation.country_code,
                source_url=observation.source_url,
            )
        )
    return rows, tuple(actions)


def synchronize_catalogs(
    project_root: Path,
    observations: Iterable[VenueObservation],
    *,
    sports: Iterable[str],
    apply: bool,
) -> dict[str, Any]:
    observations = tuple(observations)
    requested = tuple(dict.fromkeys(sport.casefold().strip() for sport in sports))
    unknown = set(requested) - SUPPORTED_VENUE_SPORTS
    if unknown:
        raise ValueError(f"Unsupported venue sports: {sorted(unknown)}")
    paths = {CATALOG_PATHS[sport] for sport in requested}
    documents = {path: json.loads((project_root / path).read_text(encoding="utf-8")) for path in paths}
    all_actions: list[SyncAction] = []
    for sport in requested:
        path = CATALOG_PATHS[sport]
        aliases = MLB_STADIUM_ALIASES if sport == "mlb" else NFLVERSE_STADIUM_ALIASES if sport == "nfl" else {}
        rows, actions = synchronize_rows(
            documents[path], observations, sport=sport, reviewed_aliases=aliases
        )
        documents[path] = rows
        all_actions.extend(actions)
    if apply:
        for path, rows in documents.items():
            (project_root / path).write_text(
                json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
    counts: dict[str, int] = {}
    for action in all_actions:
        counts[action.status] = counts.get(action.status, 0) + 1
    return {
        "sports": list(requested),
        "applied": apply,
        "counts": counts,
        "actions": [asdict(action) for action in all_actions],
    }


def default_window(today: date | None = None) -> tuple[date, date]:
    current = today or date.today()
    return current - timedelta(days=7), current + timedelta(days=180)
