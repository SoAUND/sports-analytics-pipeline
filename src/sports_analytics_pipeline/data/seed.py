"""Load controlled teams, venues, aliases, and sources."""

from __future__ import annotations

from importlib.resources import files
import json
import sqlite3


# Explicit nflverse venue names that differ from the current official names.
# Each alias is reviewed and maps to one controlled stadium record.
NFLVERSE_STADIUM_ALIASES = {
    "Reliant Stadium": "nrg_stadium",
    "Arrowhead Stadium": "arrowhead_stadium",
    "Heinz Field": "acrisure_stadium",
    "Paul Brown Stadium": "paycor_stadium",
    "Cleveland Browns Stadium": "huntington_bank_field",
    "FirstEnergy Stadium": "huntington_bank_field",
    "Commanders Field": "northwest_stadium",
    "FedExField": "northwest_stadium",
    "FedEx Field": "northwest_stadium",
    "Maracana Stadium": "maracana_stadium",
    "Bernabeu": "bernabeu_stadium",
    "Santiago Bernabéu": "bernabeu_stadium",
    "Santiago Bernabeu Stadium": "bernabeu_stadium",
    "FC Bayern Munich Stadium": "fc_bayern_munich_arena",
    "Allianz Arena": "fc_bayern_munich_arena",
    "Estadio Azteca": "estadio_banorte",
}


# Current official names plus reviewed historical/sponsor labels still returned
# by schedule providers.  These are identity aliases, not additional venues.
MLB_STADIUM_ALIASES = {
    "UNIQLO Field at Dodger Stadium": "dodger_stadium",
    "Oriole Park": "camden_yards",
    "Camden Yards": "camden_yards",
    "Guaranteed Rate Field": "rate_field",
    "U.S. Cellular Field": "rate_field",
    "Minute Maid Park": "daikin_park",
    "Oakland Coliseum": "sutter_health_park",
    "RingCentral Coliseum": "sutter_health_park",
    "Miller Park": "american_family_field",
    "Safeco Field": "t_mobile_park",
    "SunTrust Park": "truist_park",
    "AT&T Park": "oracle_park",
    "Comerica Park presented by Meijer": "comerica_park",
}


# Stable venue identifiers returned by MLB StatsAPI in the official team and
# schedule payloads. Display names may change without changing these identities.
MLB_STATSAPI_VENUE_IDS = {
    "1": "angel_stadium", "2": "camden_yards", "3": "fenway_park",
    "4": "rate_field", "5": "progressive_field", "7": "kauffman_stadium",
    "12": "tropicana_field", "14": "rogers_centre", "15": "chase_field",
    "17": "wrigley_field", "19": "coors_field", "22": "dodger_stadium",
    "31": "pnc_park", "32": "american_family_field", "680": "t_mobile_park",
    "2392": "daikin_park", "2394": "comerica_park", "2395": "oracle_park",
    "2529": "sutter_health_park", "2602": "great_american_ball_park",
    "2680": "petco_park", "2681": "citizens_bank_park", "2889": "busch_stadium",
    "3289": "citi_field", "3309": "nationals_park", "3312": "target_field",
    "3313": "yankee_stadium", "4169": "loandepot_park", "4705": "truist_park",
    "5325": "globe_life_field",
}


def _json(name: str):
    return json.loads(files("sports_analytics_pipeline.data").joinpath(name).read_text(encoding="utf-8"))


def _embedded_venue_aliases(name: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for venue in _json(name):
        for alias in venue.get("aliases", []):
            aliases[str(alias)] = str(venue["stadium_id"])
    return aliases


def _embedded_provider_ids(name: str, provider_key: str) -> dict[str, str]:
    prefix = f"{provider_key.casefold().strip()}:"
    identities: dict[str, str] = {}
    for venue in _json(name):
        for provider_id in venue.get("provider_ids", []):
            value = str(provider_id).strip()
            if value.casefold().startswith(prefix):
                identities[value[len(prefix):]] = str(venue["stadium_id"])
    return identities


def seed_reference_data(connection: sqlite3.Connection, *, include_mlb: bool = False) -> None:
    stadiums = _json("stadiums.json") + (_json("mlb_stadiums.json") if include_mlb else [])
    nfl_teams = _json("nfl_teams.json")
    mlb_teams = _json("mlb_teams.json")
    sources = _json("sources.json")

    with connection:
        for stadium in stadiums:
            connection.execute(
                """
                INSERT INTO stadiums (
                    stadium_id, official_name, city, state_or_region, country_code,
                    latitude, longitude, time_zone, roof_type, surface_type,
                    weather_exposed, valid_from_season, valid_through_season,
                    verified_on, source_url
                ) VALUES (
                    :stadium_id, :official_name, :city, :state_or_region, :country_code,
                    :latitude, :longitude, :time_zone, :roof_type, :surface_type,
                    :weather_exposed, :valid_from_season, :valid_through_season,
                    :verified_on, :source_url
                )
                ON CONFLICT(stadium_id) DO UPDATE SET
                    official_name = excluded.official_name,
                    city = excluded.city,
                    state_or_region = excluded.state_or_region,
                    country_code = excluded.country_code,
                    latitude = excluded.latitude,
                    longitude = excluded.longitude,
                    time_zone = excluded.time_zone,
                    roof_type = excluded.roof_type,
                    surface_type = excluded.surface_type,
                    weather_exposed = excluded.weather_exposed,
                    valid_from_season = excluded.valid_from_season,
                    valid_through_season = excluded.valid_through_season,
                    verified_on = excluded.verified_on,
                    source_url = excluded.source_url
                """,
                stadium,
            )
        sport_team_sets = [("nfl", nfl_teams)]
        if include_mlb:
            sport_team_sets.append(("mlb", mlb_teams))
        for sport, teams in sport_team_sets:
            for team in teams:
                team_row = {key: value for key, value in team.items() if key != "aliases"}
                team_row["sport"] = sport
                connection.execute(
                    """
                    INSERT INTO teams (
                        team_id, sport, official_name, city_name, nickname, abbreviation,
                        conference, division, primary_stadium_id, active_from_season,
                        active_through_season, verified_on
                    ) VALUES (
                        :team_id, :sport, :official_name, :city_name, :nickname, :abbreviation,
                        :conference, :division, :primary_stadium_id, :active_from_season,
                        :active_through_season, :verified_on
                    )
                    ON CONFLICT(team_id) DO UPDATE SET
                        sport = excluded.sport,
                        official_name = excluded.official_name,
                        city_name = excluded.city_name,
                        nickname = excluded.nickname,
                        abbreviation = excluded.abbreviation,
                        conference = excluded.conference,
                        division = excluded.division,
                        primary_stadium_id = excluded.primary_stadium_id,
                        active_from_season = excluded.active_from_season,
                        active_through_season = excluded.active_through_season,
                        verified_on = excluded.verified_on
                    """,
                    team_row,
                )
                for alias in team["aliases"]:
                    connection.execute(
                        """
                        INSERT INTO team_aliases(alias, sport, team_id, source_key) VALUES (?, ?, ?, ?)
                        ON CONFLICT(sport, alias) DO UPDATE SET
                            team_id = excluded.team_id,
                            source_key = excluded.source_key
                        """,
                        (alias, sport, team["team_id"], "controlled"),
                    )
        for source in sources:
            connection.execute(
                """
                INSERT INTO sources (
                    source_key, display_name, category, priority, base_url,
                    official, enabled, notes
                ) VALUES (
                    :source_key, :display_name, :category, :priority, :base_url,
                    :official, :enabled, :notes
                )
                ON CONFLICT(source_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    category = excluded.category,
                    priority = excluded.priority,
                    base_url = excluded.base_url,
                    official = excluded.official,
                    enabled = excluded.enabled,
                    notes = excluded.notes
                """,
                source,
            )


def alias_mapping(connection: sqlite3.Connection, sport: str = "nfl") -> dict[str, str]:
    """Return controlled aliases for one sport only; unknown aliases stay unresolved."""
    return {
        row["alias"]: row["team_id"]
        for row in connection.execute(
            "SELECT alias, team_id FROM team_aliases WHERE sport = ?", (sport,)
        )
    }


def stadium_alias_mapping(connection: sqlite3.Connection, sport: str = "nfl") -> dict[str, str]:
    """Return home and reviewed neutral venues without mixing sports."""
    aliases = {
        row["official_name"]: row["stadium_id"]
        for row in connection.execute(
            """SELECT DISTINCT s.official_name, s.stadium_id
               FROM stadiums s JOIN teams t ON t.primary_stadium_id = s.stadium_id
               WHERE t.sport = ?""",
            (sport,),
        )
    }
    if sport == "nfl":
        # International venues have no primary home team but host regular-season games.
        for venue in _json("stadiums.json"):
            aliases.setdefault(str(venue["official_name"]), str(venue["stadium_id"]))
        aliases.update(_embedded_venue_aliases("stadiums.json"))
        aliases.update(NFLVERSE_STADIUM_ALIASES)
    elif sport == "mlb":
        aliases.update(_embedded_venue_aliases("mlb_stadiums.json"))
        aliases.update(MLB_STADIUM_ALIASES)
    return aliases


def stadium_provider_id_mapping(sport: str) -> dict[str, str]:
    """Return reviewed stable provider venue IDs for a supported sport."""
    if sport.casefold().strip() == "mlb":
        identities = dict(MLB_STATSAPI_VENUE_IDS)
        identities.update(_embedded_provider_ids("mlb_stadiums.json", "mlb_statsapi"))
        return identities
    return {}

