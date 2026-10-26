const API = "https://footballapi.pulselive.com/football";
const ORIGIN = "https://www.premierleague.com";
const KICKOFF_TOLERANCE_MS = 10 * 60 * 1000;

const TEAM_ALIASES = new Map(Object.entries({
  "afc bournemouth": "bournemouth", "bournemouth": "bournemouth",
  "brighton and hove albion": "brighton", "brighton": "brighton",
  "coventry city": "coventry", "coventry": "coventry",
  "hull city": "hull city", "ipswich town": "ipswich town",
  "leeds united": "leeds united", "manchester city": "manchester city",
  "manchester united": "manchester united", "newcastle united": "newcastle",
  "newcastle": "newcastle", "nottingham forest": "nottingham forest",
  "tottenham hotspur": "tottenham", "tottenham": "tottenham",
  "west ham united": "west ham", "west ham": "west ham"
}));

function clean(value) {
  return String(value || "").normalize("NFKD").replace(/[^a-z0-9]+/giu, " ").trim().toLowerCase();
}

function teamKey(value) {
  const name = clean(value).replace(/\b(fc|afc)\b/gu, "").replace(/\s+/gu, " ").trim();
  return TEAM_ALIASES.get(name) || name;
}

function kickoffMillis(fixture) {
  const raw = fixture?.kickoff?.millis ?? fixture?.kickoff?.timestamp ?? fixture?.kickoff;
  if (Number.isFinite(Number(raw))) return Number(raw);
  return new Date(fixture?.kickoff?.label || fixture?.kickoff || "").getTime();
}

function sides(fixture) {
  const teams = Array.isArray(fixture?.teams) ? fixture.teams : [];
  return {
    home: teams.find((row) => row?.team?.id === fixture?.homeTeam?.id || row?.team?.name === fixture?.homeTeam?.name) || teams[0],
    away: teams.find((row) => row?.team?.id === fixture?.awayTeam?.id || row?.team?.name === fixture?.awayTeam?.name) || teams[1]
  };
}

function sideName(side) { return side?.team?.name || side?.name || ""; }
function sideId(side) { return String(side?.team?.id ?? side?.id ?? ""); }

async function jsonFetch(url, fetchImpl) {
  const response = await fetchImpl(url, { headers: { Accept: "application/json", Origin: ORIGIN } });
  if (!response.ok) throw new Error(`premier_league_http_${response.status}`);
  return response.json();
}

async function seasons(fetchImpl) {
  const url = new URL(`${API}/competitions/1/compseasons`);
  url.searchParams.set("page", "0"); url.searchParams.set("pageSize", "10");
  const payload = await jsonFetch(url, fetchImpl);
  return (payload.content || payload || []).slice(0, 2).map((row) => String(row.id));
}

async function fixtures(fetchImpl) {
  const ids = await seasons(fetchImpl);
  if (!ids.length) throw new Error("premier_league_season_not_found");
  const url = new URL(`${API}/fixtures`);
  url.searchParams.set("comp", "1"); url.searchParams.set("compSeasons", ids[0]);
  url.searchParams.set("page", "0"); url.searchParams.set("pageSize", "400");
  url.searchParams.set("sort", "asc"); url.searchParams.set("altIds", "true");
  const payload = await jsonFetch(url, fetchImpl);
  return { ids, rows: payload.content || payload || [] };
}

async function teamHistory(ids, fixture, fetchImpl) {
  const { home, away } = sides(fixture);
  const url = new URL(`${API}/fixtures`);
  url.searchParams.set("comp", "1"); url.searchParams.set("compSeasons", ids.join(","));
  url.searchParams.set("teams", `${sideId(home)},${sideId(away)}`);
  url.searchParams.set("statuses", "C"); url.searchParams.set("page", "0");
  url.searchParams.set("pageSize", "100"); url.searchParams.set("sort", "desc");
  url.searchParams.set("altIds", "true");
  const payload = await jsonFetch(url, fetchImpl);
  return payload.content || payload || [];
}

function matchFixture(rows, event) {
  const target = new Date(event.commence_time).getTime();
  return rows.find((fixture) => {
    const { home, away } = sides(fixture);
    return teamKey(sideName(home)) === teamKey(event.home?.name)
      && teamKey(sideName(away)) === teamKey(event.away?.name)
      && Math.abs(kickoffMillis(fixture) - target) <= KICKOFF_TOLERANCE_MS;
  });
}

function completed(fixture) {
  const status = clean(fixture?.status);
  return ["c", "complete", "completed", "finished", "final"].includes(status);
}

function score(side) {
  const raw = side?.score;
  if (typeof raw === "number") return raw;
  return Number(raw?.score ?? raw?.value ?? raw?.goals);
}

function pointsFor(fixture, teamId) {
  const { home, away } = sides(fixture); const hs = score(home); const as = score(away);
  if (!Number.isFinite(hs) || !Number.isFinite(as)) return null;
  if (hs === as) return 1;
  return (hs > as) === (sideId(home) === teamId) ? 3 : 0;
}

function prior(rows, teamId, cutoff) {
  return rows.filter((fixture) => completed(fixture) && kickoffMillis(fixture) < cutoff
    && Object.values(sides(fixture)).some((side) => sideId(side) === teamId))
    .sort((a, b) => kickoffMillis(b) - kickoffMillis(a));
}

function sumPoints(rows, teamId) {
  return rows.reduce((total, row) => total + (pointsFor(row, teamId) ?? 0), 0);
}

function baselineFeatures(rows, fixture) {
  const { home, away } = sides(fixture); const homeId = sideId(home); const awayId = sideId(away);
  const cutoff = kickoffMillis(fixture); const homePrior = prior(rows, homeId, cutoff); const awayPrior = prior(rows, awayId, cutoff);
  const homeFive = homePrior.slice(0, 5); const awayFive = awayPrior.slice(0, 5);
  const homeVenue = homePrior.filter((row) => sideId(sides(row).home) === homeId).slice(0, 5);
  const awayVenue = awayPrior.filter((row) => sideId(sides(row).away) === awayId).slice(0, 5);
  if (homeFive.length < 5 || awayFive.length < 5 || homeVenue.length < 5 || awayVenue.length < 5) return null;
  const day = 86400000; const windowStart = cutoff - 14 * day;
  return {
    recent5_points_diff: sumPoints(homeFive, homeId) - sumPoints(awayFive, awayId),
    home_away_strength_diff: (sumPoints(homeVenue, homeId) / 5) - (sumPoints(awayVenue, awayId) / 5),
    rest_days_diff: ((cutoff - kickoffMillis(homePrior[0])) / day) - ((cutoff - kickoffMillis(awayPrior[0])) / day),
    fixture_congestion_diff: homePrior.filter((row) => kickoffMillis(row) >= windowStart).length
      - awayPrior.filter((row) => kickoffMillis(row) >= windowStart).length
  };
}

function player(row) {
  const rawName = row?.player?.name || row?.name || "";
  const name = typeof rawName === "string"
    ? rawName
    : rawName.display || [rawName.first, rawName.middle, rawName.last].filter(Boolean).join(" ");
  return { id: String(row?.player?.id ?? row?.id ?? ""), name };
}

function players(teamList) {
  const rows = [...(teamList?.lineup || []), ...(teamList?.substitutes || [])];
  return rows.map(player)
    .filter((row) => row.id && row.name);
}

async function detail(fixture, seasonId, fetchImpl) {
  const url = new URL(`${API}/fixtures/${fixture.id}`);
  url.searchParams.set("comp", "1"); url.searchParams.set("compSeasons", seasonId); url.searchParams.set("altIds", "true");
  return jsonFetch(url, fetchImpl);
}

export async function officialEplObservations({ sourceKey, spec, events, requestedAt, fetchImpl = fetch }) {
  const observations = []; const unavailable = [];
  const historyCache = new Map();
  let catalog;
  try { catalog = await fixtures(fetchImpl); }
  catch (error) { return { observations, unavailable: events.map((event) => ({ event_id: event.event_id, reason: String(error.message) })) }; }
  for (const event of events) {
    try {
      const fixture = matchFixture(catalog.rows, event);
      if (!fixture) throw new Error("official_fixture_not_found");
      const full = await detail(fixture, catalog.ids[0], fetchImpl);
      const lists = Array.isArray(full.teamLists) ? full.teamLists : [];
      const squad = lists.flatMap(players);
      let payload;
      if (sourceKey === "premier_league") {
        payload = { fixture_id: String(fixture.id), status: full.status || fixture.status, kickoff: new Date(kickoffMillis(fixture)).toISOString(), home: event.home.name, away: event.away.name, ground: full.ground || fixture.ground, material_change: false };
      } else if (sourceKey === "premier_league_lineups") {
        if (!squad.length) throw new Error("official_lineups_not_published");
        const fixtureSides = Object.values(sides(fixture));
        payload = { fixture_id: String(fixture.id), team_lists: lists.map((row, index) => ({ team: row.team?.name || row.team || sideName(fixtureSides[index]), starters: (row.lineup || []).map(player).filter((p) => p.id && p.name), substitutes: (row.substitutes || []).map(player).filter((p) => p.id && p.name) })), material_change: false };
      } else if (sourceKey === "premier_league_team_news") {
        if (!squad.length) throw new Error("official_matchday_squads_not_published");
        payload = { fixture_id: String(fixture.id), scope: "official_matchday_squad", listed_players: squad, medical_status_claimed: false, material_change: false };
      } else if (sourceKey === "premier_league_fixtures_rest" || sourceKey === "premier_league_statistics") {
        const pairKey = Object.values(sides(fixture)).map(sideId).sort().join(":");
        if (!historyCache.has(pairKey)) historyCache.set(pairKey, await teamHistory(catalog.ids, fixture, fetchImpl));
        const features = baselineFeatures(historyCache.get(pairKey), fixture);
        if (!features) throw new Error("five_safe_prior_matches_required");
        payload = { fixture_id: String(fixture.id), baseline_features: features, derivation: "official_pre_kickoff_results_only", material_change: false };
      } else {
        if (!squad.length) throw new Error("official_matchday_squads_not_published");
        payload = { fixture_id: String(fixture.id), current_matchday_squad: squad, comparison_scope: "official_current_squad", material_change: false };
      }
      observations.push({ event_id: event.event_id, sport: event.sport, source_key: sourceKey, category: spec.category, verification_status: "verified", source_updated_at: requestedAt, collected_at: requestedAt, payload: { ...payload, test_only: true } });
    } catch (error) { unavailable.push({ event_id: event.event_id, reason: String(error?.message || "premier_league_unavailable") }); }
  }
  return { observations, unavailable };
}
