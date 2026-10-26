"""Shared, non-fatal venue identity for every sport Our bot supports.

Venue names are display labels and can change when sponsorship changes.  A venue
must therefore be matched by a provider ID first, then by a reviewed name/alias.
An unknown venue remains visible to operations, but it never becomes a guessed
match and never stops an otherwise valid collection batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import json
import re
import unicodedata
from typing import Iterable, Mapping


SUPPORTED_VENUE_SPORTS = frozenset({"nfl", "mlb", "nba", "wnba", "epl", "mma", "atp", "wta"})

# Team leagues have reviewed home-venue catalogs.  The remaining sports use an
# event/tournament venue supplied by the official schedule source.
VENUE_RESOLUTION_MODE = {
    "nfl": "team_and_neutral_catalog",
    "mlb": "team_and_neutral_catalog",
    "nba": "team_and_neutral_catalog",
    "wnba": "team_and_neutral_catalog",
    "epl": "team_and_neutral_catalog",
    "mma": "event_supplied",
    "atp": "tournament_supplied",
    "wta": "tournament_supplied",
}

EPL_REVIEWED_HOME_VENUES = {
    "bournemouth": "vitality_stadium",
    "arsenal": "emirates_stadium",
    "aston villa": "villa_park",
    "brentford": "gtech_community_stadium",
    "brighton": "american_express_stadium",
    "chelsea": "stamford_bridge",
    "coventry": "coventry_building_society_arena",
    "crystal palace": "selhurst_park",
    "everton": "hill_dickinson_stadium",
    "fulham": "craven_cottage",
    "hull city": "mkm_stadium",
    "ipswich town": "portman_road",
    "leeds united": "elland_road",
    "liverpool": "anfield",
    "manchester city": "etihad_stadium",
    "manchester united": "old_trafford",
    "newcastle": "st_james_park",
    "nottingham forest": "city_ground",
    "sunderland": "stadium_of_light",
    "tottenham": "tottenham_hotspur_stadium",
}


def normalize_venue_name(value: str) -> str:
    """Normalize harmless provider formatting without inventing fuzzy matches."""
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    folded = folded.casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", folded).split())


@dataclass(frozen=True)
class VenueIdentity:
    venue_id: str
    official_name: str
    sports: tuple[str, ...]
    city: str
    state_or_region: str
    country_code: str
    time_zone: str
    venue_type: str
    aliases: tuple[str, ...] = ()
    provider_ids: tuple[str, ...] = ()
    provider_sport_keys: tuple[str, ...] = ()
    verified_on: str | None = None
    source_url: str | None = None


@dataclass(frozen=True)
class VenueResolution:
    sport: str
    source_name: str | None
    source_id: str | None
    venue: VenueIdentity | None
    matched_by: str | None

    @property
    def resolved(self) -> bool:
        return self.venue is not None


class VenueDirectory:
    """Resolve reviewed venue identities with ID-first, alias-second matching."""

    def __init__(self, venues: Iterable[VenueIdentity]):
        self._by_venue_id: dict[str, VenueIdentity] = {}
        self._by_provider_id: dict[tuple[str, str], VenueIdentity] = {}
        self._by_provider_sport_key: dict[tuple[str, str], VenueIdentity] = {}
        self._by_name: dict[tuple[str, str], VenueIdentity] = {}
        for venue in venues:
            self._by_venue_id[venue.venue_id] = venue
            for sport in venue.sports:
                if sport not in SUPPORTED_VENUE_SPORTS:
                    raise ValueError(f"Unsupported venue sport: {sport!r}")
                for provider_id in venue.provider_ids:
                    self._add(self._by_provider_id, (sport, provider_id.strip()), venue)
                for provider_sport_key in venue.provider_sport_keys:
                    self._add(
                        self._by_provider_sport_key,
                        (sport, provider_sport_key.casefold().strip()),
                        venue,
                    )
                for name in (venue.official_name, *venue.aliases):
                    self._add(self._by_name, (sport, normalize_venue_name(name)), venue)

    @staticmethod
    def _add(index: dict, key: tuple[str, str], venue: VenueIdentity) -> None:
        previous = index.get(key)
        if previous is not None and previous.venue_id != venue.venue_id:
            raise ValueError(f"Ambiguous venue identity for {key!r}")
        index[key] = venue

    @classmethod
    def from_records(cls, rows: Iterable[Mapping[str, object]]) -> "VenueDirectory":
        venues = []
        for row in rows:
            venues.append(
                VenueIdentity(
                    venue_id=str(row["venue_id"]),
                    official_name=str(row["official_name"]),
                    sports=tuple(str(value) for value in row["sports"]),
                    city=str(row.get("city") or ""),
                    state_or_region=str(row.get("state_or_region") or ""),
                    country_code=str(row.get("country_code") or ""),
                    time_zone=str(row.get("time_zone") or ""),
                    venue_type=str(row.get("venue_type") or "event"),
                    aliases=tuple(str(value) for value in row.get("aliases", ())),
                    provider_ids=tuple(str(value) for value in row.get("provider_ids", ())),
                    provider_sport_keys=tuple(
                        str(value) for value in row.get("provider_sport_keys", ())
                    ),
                    verified_on=str(row["verified_on"]) if row.get("verified_on") else None,
                    source_url=str(row["source_url"]) if row.get("source_url") else None,
                )
            )
        return cls(venues)

    def resolve_optional(
        self,
        sport: str,
        *,
        source_name: str | None = None,
        source_id: str | int | None = None,
        provider_key: str | None = None,
        provider_sport_key: str | None = None,
    ) -> VenueResolution:
        sport_key = sport.casefold().strip()
        if sport_key not in SUPPORTED_VENUE_SPORTS:
            raise ValueError(f"Unsupported venue sport: {sport!r}")
        clean_id = str(source_id).strip() if source_id is not None else None
        clean_name = str(source_name).strip() if source_name is not None else None
        if clean_id:
            candidates = [clean_id]
            if provider_key:
                candidates.insert(0, f"{provider_key.casefold().strip()}:{clean_id}")
            for candidate in candidates:
                venue = self._by_provider_id.get((sport_key, candidate))
                if venue is not None:
                    return VenueResolution(sport_key, clean_name, clean_id, venue, "provider_id")
        if clean_name:
            venue = self._by_name.get((sport_key, normalize_venue_name(clean_name)))
            if venue is not None:
                return VenueResolution(sport_key, clean_name, clean_id, venue, "reviewed_name")
        clean_provider_sport_key = (
            str(provider_sport_key).casefold().strip() if provider_sport_key else None
        )
        if clean_provider_sport_key and not clean_id and not clean_name:
            venue = self._by_provider_sport_key.get((sport_key, clean_provider_sport_key))
            if venue is not None:
                return VenueResolution(
                    sport_key,
                    clean_name,
                    clean_id,
                    venue,
                    "provider_sport_key",
                )
        return VenueResolution(sport_key, clean_name, clean_id, None, None)

    def resolve_reviewed_home(self, sport: str, home_participant_name: str) -> VenueIdentity | None:
        """Resolve only an exact reviewed home-team mapping; never fuzzy match."""
        sport_key = sport.casefold().strip()
        if sport_key != "epl":
            return None
        team = normalize_venue_name(home_participant_name)
        aliases = {
            "afc bournemouth": "bournemouth",
            "brighton and hove albion": "brighton",
            "coventry city": "coventry",
            "newcastle united": "newcastle",
            "tottenham hotspur": "tottenham",
        }
        venue_id = EPL_REVIEWED_HOME_VENUES.get(aliases.get(team, team))
        return self._by_venue_id.get(venue_id or "")


def _catalog_rows(name: str) -> list[dict[str, object]]:
    return json.loads(files("sports_analytics_pipeline.data").joinpath(name).read_text(encoding="utf-8"))


def multisport_venue_directory() -> VenueDirectory:
    return VenueDirectory.from_records(_catalog_rows("multisport_venues.json"))
