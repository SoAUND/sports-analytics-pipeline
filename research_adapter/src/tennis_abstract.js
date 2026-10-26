const PLAYER_PAGE_BYTES = 512 * 1024;
const PLAYER_FRAGMENT_BYTES = 2 * 1024 * 1024;
const PLAYER_HOST = "www.tennisabstract.com";
const RECENT_FORM_MATCHES = 5;

const TOURNAMENT_SURFACES = Object.freeze({
  tennis_wta_cincinnati_open: "Hard",
  tennis_wta_monterrey_open: "Hard"
});

function decodeEntities(value) {
  const named = {
    amp: "&",
    apos: "'",
    gt: ">",
    lt: "<",
    nbsp: " ",
    quot: '"'
  };
  return String(value || "").replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/giu, (match, entity) => {
    const key = String(entity).toLowerCase();
    if (key.startsWith("#x")) return String.fromCodePoint(Number.parseInt(key.slice(2), 16));
    if (key.startsWith("#")) return String.fromCodePoint(Number.parseInt(key.slice(1), 10));
    return named[key] ?? match;
  });
}

function visibleText(value) {
  return decodeEntities(String(value || "").replace(/<[^>]+>/gu, " "))
    .replace(/\s+/gu, " ")
    .trim();
}

function normal(value) {
  return String(value || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/gu, "")
    .replace(/[^a-z0-9]+/giu, " ")
    .toLowerCase()
    .replace(/\s+/gu, " ")
    .trim();
}

function playerSlug(name) {
  const slug = String(name || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/gu, "")
    .replace(/[^a-z0-9]/giu, "");
  if (slug.length < 4 || slug.length > 100) throw new Error("player_identity_required");
  return slug;
}

function jsString(page, variable) {
  const match = String(page || "").match(
    new RegExp(`\\bvar\\s+${variable}\\s*=\\s*'((?:\\\\.|[^'\\\\])*)'\\s*;`, "u")
  );
  if (!match) throw new Error(`tennis_abstract_${variable}_missing`);
  return match[1].replace(/\\'/gu, "'").replace(/\\\\/gu, "\\");
}

function jsPositiveInteger(page, variable) {
  const match = String(page || "").match(new RegExp(`\\bvar\\s+${variable}\\s*=\\s*(\\d+)\\s*;`, "u"));
  const value = match ? Number(match[1]) : Number.NaN;
  if (!Number.isInteger(value) || value < 1) throw new Error(`tennis_abstract_${variable}_missing`);
  return value;
}

function tableRows(fragment, tableId) {
  const table = String(fragment || "").match(
    new RegExp(`<table\\b[^>]*\\bid=["']${tableId}["'][^>]*>([\\s\\S]*?)<\\/table>`, "iu")
  );
  if (!table) throw new Error(`tennis_abstract_${tableId}_missing`);
  const body = table[1].match(/<tbody\b[^>]*>([\s\S]*?)<\/tbody>/iu)?.[1];
  if (!body) throw new Error(`tennis_abstract_${tableId}_body_missing`);
  return [...body.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/giu)].map((row) => (
    [...row[1].matchAll(/<td\b[^>]*>([\s\S]*?)<\/td>/giu)].map((cell) => ({
      html: cell[1],
      text: visibleText(cell[1])
    }))
  ));
}

function positiveNumber(value, label) {
  const number = Number(String(value || "").replace(/,/gu, ""));
  if (!Number.isFinite(number) || number <= 0) throw new Error(`tennis_abstract_${label}_missing`);
  return number;
}

function percent(value, label) {
  const number = Number(String(value || "").replace("%", ""));
  if (!Number.isFinite(number) || number < 0 || number > 100) {
    throw new Error(`tennis_abstract_${label}_missing`);
  }
  return number;
}

function rounded(value) {
  return Number(Number(value).toFixed(6));
}

function isoDate(value) {
  const months = {
    Jan: "01", Feb: "02", Mar: "03", Apr: "04", May: "05", Jun: "06",
    Jul: "07", Aug: "08", Sep: "09", Oct: "10", Nov: "11", Dec: "12"
  };
  const match = String(value || "").match(/^(\d{2})-([A-Z][a-z]{2})-(\d{4})$/u);
  if (!match || !months[match[2]]) throw new Error("tennis_abstract_match_date_unreviewed");
  return `${match[3]}-${months[match[2]]}-${match[1]}`;
}

function currentRatings(fragment, expectedRank, surface) {
  const row = tableRows(fragment, "year-end-rankings").find((cells) => /^Current \(\d{4}-\d{2}-\d{2}\)$/u.test(cells[0]?.text));
  if (!row || row.length < 11) throw new Error("tennis_abstract_current_ratings_missing");
  const updateDate = row[0].text.match(/\((\d{4}-\d{2}-\d{2})\)/u)?.[1];
  const ranking = positiveNumber(row[1].text, "current_rank");
  if (ranking !== expectedRank) throw new Error("tennis_abstract_ranking_mismatch");
  const surfaceIndex = { Hard: 6, Clay: 8, Grass: 10 }[surface];
  if (!surfaceIndex) throw new Error("unsupported_tennis_surface");
  return {
    ranking,
    overallElo: positiveNumber(row[4].text, "overall_elo"),
    surfaceElo: positiveNumber(row[surfaceIndex].text, "surface_elo"),
    updateDate
  };
}

function surfaceStats(fragment, surface) {
  const row = tableRows(fragment, "last52-splits").find((cells) => normal(cells[0]?.text) === normal(surface));
  if (!row || row.length < 23) throw new Error("tennis_abstract_surface_split_missing");
  const matches = positiveNumber(row[1].text, "surface_matches");
  const matchesWithStats = positiveNumber(row[11].text, "surface_stat_matches");
  return {
    matches,
    matchesWithStats,
    servePointsWon: percent(row[19].text, "surface_spw"),
    returnPointsWon: percent(row[20].text, "surface_rpw")
  };
}

function playerWon(outcomeHtml) {
  const sides = String(outcomeHtml || "").split(/\s+d\.\s+/iu);
  if (sides.length !== 2) throw new Error("tennis_abstract_result_unreviewed");
  const left = /<b\b[^>]*>/iu.test(sides[0]);
  const right = /<b\b[^>]*>/iu.test(sides[1]);
  if (left === right) throw new Error("tennis_abstract_result_identity_ambiguous");
  return left;
}

function recentForm(fragment, eventDate) {
  const matches = [];
  for (const row of tableRows(fragment, "recent-results")) {
    if (row.length < 9 || !row[7].text) continue;
    const date = isoDate(row[0].text);
    if (date >= eventDate) continue;
    const playerRank = positiveNumber(row[4].text, "recent_player_rank");
    const opponentRank = positiveNumber(row[5].text, "recent_opponent_rank");
    const result = playerWon(row[6].html) ? 1 : 0;
    const expectedWin = opponentRank / (playerRank + opponentRank);
    matches.push({
      date,
      playerRank,
      opponentRank,
      result,
      expectedWin: rounded(expectedWin),
      adjustedResult: rounded(result - expectedWin)
    });
    if (matches.length === RECENT_FORM_MATCHES) break;
  }
  if (matches.length !== RECENT_FORM_MATCHES) {
    throw new Error("tennis_abstract_recent_form_sample_incomplete");
  }
  return {
    score: rounded(matches.reduce((sum, match) => sum + match.adjustedResult, 0) / matches.length),
    matches
  };
}

async function boundedText(url, { accept, contentTypes, maxBytes }, fetchImpl) {
  const response = await fetchImpl(url, { headers: { Accept: accept }, redirect: "follow" });
  if (!response.ok) throw new Error(`upstream_http_${response.status}`);
  if (response.url && new URL(response.url).hostname !== PLAYER_HOST) {
    throw new Error("tennis_abstract_redirect_rejected");
  }
  const contentType = String(response.headers.get("content-type") || "").toLowerCase();
  if (!contentTypes.some((value) => contentType.includes(value))) {
    throw new Error("tennis_abstract_content_type");
  }
  const contentLength = Number(response.headers.get("content-length") || 0);
  if (contentLength > maxBytes) throw new Error("tennis_abstract_page_too_large");
  const text = await response.text();
  if (new TextEncoder().encode(text).length > maxBytes) throw new Error("tennis_abstract_page_too_large");
  return text;
}

async function playerEvidence(name, surface, eventDate, requestedDate, cache, fetchImpl) {
  const key = `${normal(name)}:${surface}:${eventDate}`;
  let evidence = cache.get(key);
  if (evidence) return evidence;
  const slug = playerSlug(name);
  const playerUrl = `https://${PLAYER_HOST}/cgi-bin/wplayer.cgi?p=${encodeURIComponent(slug)}`;
  const page = await boundedText(playerUrl, {
    accept: "text/html,application/xhtml+xml",
    contentTypes: ["text/html"],
    maxBytes: PLAYER_PAGE_BYTES
  }, fetchImpl);
  const sourceName = jsString(page, "fullname");
  if (normal(sourceName) !== normal(name)) throw new Error("tennis_abstract_player_identity_mismatch");
  const currentRank = jsPositiveInteger(page, "currentrank");
  const fragmentMatch = page.match(
    /<script\b[^>]*\bsrc=["'](https:\/\/www\.tennisabstract\.com\/jsfrags\/([a-z0-9]+)\.js)["'][^>]*><\/script>/iu
  );
  if (!fragmentMatch) throw new Error("tennis_abstract_player_fragment_missing");
  const fragmentUrl = fragmentMatch[1];
  const fragment = await boundedText(fragmentUrl, {
    accept: "text/javascript,application/javascript",
    contentTypes: ["text/javascript", "application/javascript"],
    maxBytes: PLAYER_FRAGMENT_BYTES
  }, fetchImpl);
  const ratings = currentRatings(fragment, currentRank, surface);
  if (!ratings.updateDate || ratings.updateDate > eventDate || ratings.updateDate > requestedDate) {
    throw new Error("tennis_abstract_source_date_after_cutoff");
  }
  const stats = surfaceStats(fragment, surface);
  const form = recentForm(fragment, eventDate);
  evidence = {
    sourceName,
    playerUrl,
    fragmentUrl,
    ranking: ratings.ranking,
    overallElo: ratings.overallElo,
    surfaceElo: ratings.surfaceElo,
    sourceDataDate: ratings.updateDate,
    surfaceMatches: stats.matches,
    surfaceMatchesWithStats: stats.matchesWithStats,
    servePointsWon: stats.servePointsWon,
    returnPointsWon: stats.returnPointsWon,
    recentForm: form.score,
    recentMatches: form.matches
  };
  cache.set(key, evidence);
  return evidence;
}

export async function tennisAbstractObservations({
  sourceKey,
  spec,
  events,
  requestedAt,
  fetchImpl = fetch
}) {
  const observations = [];
  const unavailable = [];
  const playerCache = new Map();
  const requestedDate = String(requestedAt).slice(0, 10);
  for (const event of events) {
    const surface = TOURNAMENT_SURFACES[event.provider_sport_key];
    if (event.sport !== "wta" || !surface || spec.category !== "statistics_analytics") {
      unavailable.push({ event_id: event.event_id, reason: "unsupported_tournament_provider_key" });
      continue;
    }
    try {
      const eventDate = String(event.commence_time).slice(0, 10);
      const home = await playerEvidence(
        event.home?.name,
        surface,
        eventDate,
        requestedDate,
        playerCache,
        fetchImpl
      );
      const away = await playerEvidence(
        event.away?.name,
        surface,
        eventDate,
        requestedDate,
        playerCache,
        fetchImpl
      );
      if (home.sourceDataDate !== away.sourceDataDate) {
        throw new Error("tennis_abstract_source_date_mismatch");
      }
      observations.push({
        event_id: event.event_id,
        sport: event.sport,
        source_key: sourceKey,
        category: spec.category,
        verification_status: "verified",
        collected_at: requestedAt,
        payload: {
          event_id: event.event_id,
          provider_sport_key: event.provider_sport_key,
          source_event_id: event.source_event_id,
          surface,
          source_data_date: home.sourceDataDate,
          statistics_basis: "tennis_abstract_current_ratings_last52_surface_splits_and_prior_results",
          recent_form_basis: "mean_actual_minus_rank_expected_result_over_last_five_completed_pre_event_matches",
          participants: [event.home, event.away].map((participant, index) => {
            const row = index === 0 ? home : away;
            return {
              participant_id: participant.participant_id,
              source_name: participant.name,
              tennis_abstract_name: row.sourceName,
              player_url: row.playerUrl,
              fragment_url: row.fragmentUrl,
              ranking: row.ranking,
              overall_elo: row.overallElo,
              surface_elo: row.surfaceElo,
              surface_matches_last_52_weeks: row.surfaceMatches,
              surface_matches_with_stats_last_52_weeks: row.surfaceMatchesWithStats,
              surface_serve_points_won_pct: row.servePointsWon,
              surface_return_points_won_pct: row.returnPointsWon,
              opponent_adjusted_recent_form: row.recentForm,
              recent_form_matches: row.recentMatches
            };
          }),
          ranking_diff: away.ranking - home.ranking,
          surface_elo_diff: rounded(home.surfaceElo - away.surfaceElo),
          serve_points_won_diff: rounded(home.servePointsWon - away.servePointsWon),
          return_points_won_diff: rounded(home.returnPointsWon - away.returnPointsWon),
          recent_form_diff: rounded(home.recentForm - away.recentForm),
          material_change: false,
          test_only: true
        }
      });
    } catch (error) {
      unavailable.push({
        event_id: event.event_id,
        reason: String(error?.message || "tennis_abstract_statistics_unavailable")
      });
    }
  }
  return { observations, unavailable };
}
