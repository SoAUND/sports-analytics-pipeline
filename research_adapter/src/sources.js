export const SOURCE_DIRECTORY = Object.freeze({
  nba_official: { sports: ["nba"], category: "official_status", official: true },
  nba_injury_report: { sports: ["nba"], category: "injury_availability", official: true },
  nba_lineups: { sports: ["nba"], category: "lineup_or_participants", official: true },
  nba_schedule_rest: { sports: ["nba"], category: "rest_travel", official: true },
  nba_stats: { sports: ["nba"], category: "statistics_analytics", official: true },
  nba_transactions: { sports: ["nba"], category: "transactions_or_changes", official: true },
  wnba_official: { sports: ["wnba"], category: "official_status", official: true },
  wnba_injury_report: { sports: ["wnba"], category: "injury_availability", official: true },
  wnba_roster_tracker: { sports: ["wnba"], category: "lineup_or_participants", official: true },
  wnba_schedule_rest: { sports: ["wnba"], category: "rest_travel", official: true },
  basketball_reference: { sports: ["wnba"], category: "statistics_analytics", official: false },
  wnba_transactions: { sports: ["wnba"], category: "transactions_or_changes", official: true },
  premier_league: { sports: ["epl"], category: "official_status", official: true },
  premier_league_team_news: { sports: ["epl"], category: "injury_availability", official: true },
  premier_league_lineups: { sports: ["epl"], category: "lineup_or_participants", official: true },
  open_meteo_epl: { sports: ["epl"], category: "weather_environment", official: false },
  premier_league_fixtures_rest: { sports: ["epl"], category: "rest_travel", official: true },
  premier_league_statistics: { sports: ["epl"], category: "statistics_analytics", official: true },
  premier_league_squad_changes: { sports: ["epl"], category: "transactions_or_changes", official: true },
  ufc_news: { sports: ["mma"], category: "official_status", official: true },
  ufc_news_injuries: { sports: ["mma"], category: "injury_availability", official: true },
  ufc_event: { sports: ["mma"], category: "lineup_or_participants", official: true },
  ufc_weigh_in: { sports: ["mma"], category: "weigh_in_status", official: true },
  ufc_event_rest_travel: { sports: ["mma"], category: "rest_travel", official: true },
  ufcstats: { sports: ["mma"], category: "statistics_analytics", official: true },
  tapology: { sports: ["mma"], category: "transactions_or_changes", official: false },
  atp_tour: { sports: ["atp"], category: "official_status", official: true },
  atp_tour_status: { sports: ["atp"], category: "injury_availability", official: true },
  atp_draws: { sports: ["atp"], category: "lineup_or_participants", official: true },
  open_meteo_atp: { sports: ["atp"], category: "weather_environment", official: false },
  atp_schedule_rest: { sports: ["atp"], category: "rest_travel", official: true },
  tennis_abstract: { sports: ["atp", "wta"], category: "statistics_analytics", official: false },
  wta: { sports: ["wta"], category: "official_status", official: true },
  wta_status: { sports: ["wta"], category: "injury_availability", official: true },
  wta_draws: { sports: ["wta"], category: "lineup_or_participants", official: true },
  open_meteo_wta: { sports: ["wta"], category: "weather_environment", official: false },
  wta_schedule_rest: { sports: ["wta"], category: "rest_travel", official: true }
});

export const WEATHER_SOURCES = new Set([
  "open_meteo_epl",
  "open_meteo_atp",
  "open_meteo_wta"
]);

export const OFFICIAL_EPL_SOURCES = new Set([
  "premier_league",
  "premier_league_team_news",
  "premier_league_lineups",
  "premier_league_fixtures_rest",
  "premier_league_statistics",
  "premier_league_squad_changes"
]);

export const OFFICIAL_TENNIS_DRAW_SOURCES = new Set([
  "atp_draws",
  "wta_draws"
]);

export const OFFICIAL_UFC_SOURCES = new Set([
  "ufc_news",
  "ufc_event"
]);

export const OFFICIAL_TENNIS_STATUS_SOURCES = new Set([
  "wta"
]);

export const OFFICIAL_TENNIS_AVAILABILITY_SOURCES = new Set([
  "wta_status"
]);

export const OFFICIAL_TENNIS_SCHEDULE_REST_SOURCES = new Set([
  "wta_schedule_rest"
]);

export const TENNIS_ABSTRACT_SOURCES = new Set([
  "tennis_abstract"
]);

export function publicManifest() {
  return Object.fromEntries(Object.entries(SOURCE_DIRECTORY).map(([key, value]) => [key, {
    ...value,
    implementation: OFFICIAL_EPL_SOURCES.has(key)
      ? "live_official_premier_league"
      : WEATHER_SOURCES.has(key)
      ? "live_open_meteo"
      : OFFICIAL_UFC_SOURCES.has(key)
        ? "live_official_ufc_identity"
      : OFFICIAL_TENNIS_DRAW_SOURCES.has(key)
        ? "live_official_tennis_draw"
        : OFFICIAL_TENNIS_STATUS_SOURCES.has(key)
          ? "live_official_wta_status"
          : OFFICIAL_TENNIS_AVAILABILITY_SOURCES.has(key)
            ? "live_official_wta_availability"
            : OFFICIAL_TENNIS_SCHEDULE_REST_SOURCES.has(key)
              ? "live_official_wta_schedule_rest"
              : TENNIS_ABSTRACT_SOURCES.has(key)
                ? "live_tennis_abstract_statistics"
                : "fail_closed_pending_connector"
  }]));
}
