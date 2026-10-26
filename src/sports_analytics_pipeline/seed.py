"""Load controlled teams, venues, aliases, and sources."""

from __future__ import annotations

from importlib.resources import files
import json
import sqlite3


# Explicit nflverse venue names that differ from the current official names.
# Each alias is reviewed and maps to one controlled stadium record.
NFLVERSE_STADIUM_ALIASES = {
    "Reliant Stadium": "nrg_stadium",
    "Maracana Stadium": "maracana_stadium",
    "Bernabeu": "bernabeu_stadium",
    "FC Bayern Munich Stadium": "fc_bayern_munich_arena",
}


def _json(name: str):
    return json.loads(files("sports_analytics_pipeline.data").joinpath(name).read_text(encoding="utf-8"))


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
    """Return official home venues for one sport; NFL also keeps reviewed provider aliases."""
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
        aliases.update(NFLVERSE_STADIUM_ALIASES)
    return aliases
