/**
 * dingersonly-live-hr — Cloudflare Worker
 *
 * Two jobs:
 *   1. scheduled() — runs every minute. Pulls today's MLB schedule, walks each
 *      live/final game's play-by-play, extracts every home run, writes the
 *      assembled feed into Workers KV under `live-hrs:<YYYY-MM-DD>`.
 *   2. fetch()     — serves GET /api/live-hrs from KV with CORS + a short
 *      browser cache so the static site can poll cheaply.
 *
 * MLB Stats API is unauthenticated and free. We try to be polite:
 *   - one /schedule request per cron tick
 *   - per-game /playByPlay only for In-Progress / Final games
 *   - skip games that are already Final and unchanged since last write
 */

const MLB_API = "https://statsapi.mlb.com/api/v1";
const KV_PREFIX = "live-hrs";
const META_KEY = (date) => `${KV_PREFIX}:meta:${date}`;
const FEED_KEY = (date) => `${KV_PREFIX}:${date}`;

// Cache-Control on the public response. 20s is a good tradeoff: cron writes
// every 60s, browser polls every ~30s, edge serves stale-but-fresh between.
const PUBLIC_CACHE_SECONDS = 20;

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

// ---------------------------------------------------------------------------
// Date helpers
// ---------------------------------------------------------------------------

/**
 * Today's date in America/New_York as YYYY-MM-DD.
 * We schedule against ET because MLB's "game day" rolls over there.
 */
function todayInTZ(tz) {
  const now = new Date();
  // en-CA gives YYYY-MM-DD shape directly.
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now);
}

// ---------------------------------------------------------------------------
// MLB Stats API
// ---------------------------------------------------------------------------

async function fetchSchedule(date) {
  // hydrate=team adds team.abbreviation to each game's teams.{home,away}.team
  // block. Without it the API returns only id/link/name, which made
  // battingTeamAbbr come through as "" on every HR card. Cheap addition
  // (~15 games × small payload bump).
  const url = `${MLB_API}/schedule?sportId=1&date=${date}&hydrate=team`;
  const resp = await fetch(url, {
    headers: { "User-Agent": "dingersonly-live-hr/0.1" },
    cf: { cacheTtl: 30, cacheEverything: true },
  });
  if (!resp.ok) throw new Error(`schedule ${resp.status}`);
  const data = await resp.json();
  const games = [];
  for (const d of data.dates ?? []) {
    for (const g of d.games ?? []) {
      games.push({
        gamePk: g.gamePk,
        state: g.status?.detailedState ?? "",
        abstract: g.status?.abstractGameState ?? "",
        homeName: g.teams?.home?.team?.name ?? "",
        awayName: g.teams?.away?.team?.name ?? "",
        homeAbbr: g.teams?.home?.team?.abbreviation ?? "",
        awayAbbr: g.teams?.away?.team?.abbreviation ?? "",
        venue: g.venue?.name ?? "",
        gameDate: g.gameDate ?? null,
      });
    }
  }
  return games;
}

// Field whitelist for the playByPlay fetch. The full payload returns
// pitch-by-pitch metadata, broadcast captions, replay flags, etc. that
// blow it up to ~1MB per game. We only need ~15 fields for HR extraction;
// trimming to those drops payload to ~50-100KB and JSON.parse from
// ~10ms to ~1-2ms per game. The `?fields=` server-side filter is the
// single biggest CPU win for the Worker's free-tier 10ms budget.
const PBP_FIELDS = [
  "allPlays", "result", "eventType", "description",
  "homeScore", "awayScore",
  "about", "inning", "halfInning", "startTime", "endTime", "atBatIndex",
  "matchup", "batter", "pitcher", "id", "fullName",
  "playEvents", "hitData",
  "launchSpeed", "launchAngle", "totalDistance",
  "coordinates", "coordX", "coordY",
  "trajectory", "location", "hardness",
].join(",");

async function fetchPlayByPlay(gamePk) {
  const url = `${MLB_API}/game/${gamePk}/playByPlay?fields=${PBP_FIELDS}`;
  const resp = await fetch(url, {
    headers: { "User-Agent": "dingersonly-live-hr/0.1" },
    // No CDN cache — we want fresh play data, but the upstream MLB API
    // sets reasonable cache headers anyway (60s).
  });
  if (!resp.ok) return null;
  return await resp.json();
}

/**
 * Extract every HR from a playByPlay payload.
 * Each play has result.eventType === "home_run" when applicable.
 */
function extractHRs(pbp, gameMeta) {
  const out = [];
  for (const play of pbp?.allPlays ?? []) {
    const result = play.result ?? {};
    if (result.eventType !== "home_run") continue;

    const matchup = play.matchup ?? {};
    const about = play.about ?? {};
    const batter = matchup.batter ?? {};
    const pitcher = matchup.pitcher ?? {};
    const batterTeamSide = matchup.batSide ? null : null; // unused
    const halfInning = about.halfInning ?? "";
    // Determine batting team: in top of inning the away team bats.
    const battingTeam =
      halfInning === "top" ? gameMeta.awayName : gameMeta.homeName;
    const battingTeamAbbr =
      halfInning === "top" ? gameMeta.awayAbbr : gameMeta.homeAbbr;
    const pitchingTeam =
      halfInning === "top" ? gameMeta.homeName : gameMeta.awayName;

    // Hit data lives on the last playEvent that has hitData.
    let hitData = null;
    for (const ev of play.playEvents ?? []) {
      if (ev.hitData) hitData = ev.hitData;
    }

    out.push({
      // Stable id so the client can dedupe across polls.
      id: `${gameMeta.gamePk}:${about.atBatIndex ?? play.atBatIndex ?? ""}`,
      gamePk: gameMeta.gamePk,
      time: about.endTime || about.startTime || gameMeta.gameDate,
      inning: about.inning ?? null,
      halfInning,
      batterId: batter.id ?? null,
      batterName: batter.fullName ?? "",
      pitcherId: pitcher.id ?? null,
      pitcherName: pitcher.fullName ?? "",
      battingTeam,
      battingTeamAbbr,
      pitchingTeam,
      venue: gameMeta.venue,
      description: result.description ?? "",
      // Often present:
      launchSpeed: hitData?.launchSpeed ?? null,
      launchAngle: hitData?.launchAngle ?? null,
      totalDistance: hitData?.totalDistance ?? null,
      // Spray-chart fields for the front-end Topps card diamond SVG.
      // MLB coordinate system: home plate at ~(125, 200), X grows toward
      // right field, Y *decreases* toward outfield (top of payload Y axis).
      // Range is roughly 0–250. coordX/coordY are null when Statcast didn't
      // track the hit (rare for HRs but happens — fall back to `location`).
      coordX: hitData?.coordinates?.coordX ?? null,
      coordY: hitData?.coordinates?.coordY ?? null,
      // Trajectory and location (e.g. "fly_ball", "line_drive" / "7"|"8"|"9"
      // for LF/CF/RF) are useful for fallback rendering when coords are null.
      trajectory: hitData?.trajectory ?? null,
      location: hitData?.location ?? null,
      // Score after the HR
      homeScore: result.homeScore ?? null,
      awayScore: result.awayScore ?? null,
    });
  }
  return out;
}

// ---------------------------------------------------------------------------
// The work
// ---------------------------------------------------------------------------

/**
 * Stable fingerprint of the meaningful content of a payload.
 *
 * Excludes `updatedAt` and any other wall-clock fields so that two ticks
 * with identical baseball state hash to the same string. We use this to
 * skip KV writes (free-tier write quota is 1k/day; we tick 1440x/day).
 */
function fingerprint(games, hrs) {
  const g = games
    .map((x) => `${x.gamePk}:${x.abstract}:${x.state}`)
    .sort()
    .join("|");
  const h = hrs
    .map((x) => x.id)
    .sort()
    .join("|");
  return `g=${games.length};lf=${games.filter((x) => x.abstract === "Live" || x.abstract === "Final").length};hr=${hrs.length};gs=${g};hs=${h}`;
}

// Round-robin tick size. With CF Workers free tier capping CPU at 10ms
// per invocation, parsing all 12+ live-game playByPlay payloads in one
// tick blew the budget (errors ~5-10% of cron invocations during
// game-time hours, 142 errors / 24h on 2026-05-02). We now process up
// to N_PER_TICK games per minute, round-robin across the full slate.
//
// With 12 live games and N_PER_TICK=3, full refresh cycle = 4 minutes.
// User-visible lag for a new HR: 0-4 min (avg ~2 min). Acceptable for
// a "Live Today" feed; well under the 36h KV TTL anyway.
//
// Combined with the ?fields= filter on fetchPlayByPlay, each tick's
// JSON.parse cost should drop to ~3-6ms total — comfortably inside 10ms.
const N_PER_TICK = 3;
const STATE_KEY = (date) => `${KV_PREFIX}:state:${date}`;

/**
 * Smart round-robin refresh:
 *   - Tracks per-game state in KV (cursor + doneFinal set)
 *   - Processes only N_PER_TICK games each invocation
 *   - Skips Final games we've already fetched once (their content
 *     never changes, so re-fetching is pure waste)
 *   - Merges new HRs with existing KV-stored HRs from games we
 *     DIDN'T process this tick
 *
 * State stored in KV:
 *   { cursor: int, doneFinal: { gamePk: true, ... } }
 */
async function refresh(env) {
  const tz = env.MLB_TIMEZONE || "America/New_York";
  const date = todayInTZ(tz);

  let games;
  try {
    games = await fetchSchedule(date);
  } catch (err) {
    console.log(`schedule fetch failed: ${err.message}`);
    return { date, error: err.message };
  }

  // Sort by gamePk for deterministic round-robin ordering.
  const liveOrFinal = games
    .filter((g) => g.abstract === "Live" || g.abstract === "Final")
    .sort((a, b) => a.gamePk - b.gamePk);

  // Read prior state + existing payload from KV. New day → both empty.
  let state = { cursor: 0, doneFinal: {} };
  try {
    const raw = await env.LIVE_HR_KV.get(STATE_KEY(date));
    if (raw) state = { ...state, ...JSON.parse(raw) };
  } catch (err) {
    console.log(`state read failed: ${err.message}`);
  }

  let existingHRs = [];
  let existingFP = null;
  try {
    const raw = await env.LIVE_HR_KV.get(FEED_KEY(date));
    if (raw) {
      const prev = JSON.parse(raw);
      existingHRs = prev.hrs || [];
      // Pre-existing payloads written before fingerprinting was added carry
      // no `fp` field — treat as null so the first post-deploy tick always
      // writes once (re-establishing the fp), then skip-writes thereafter.
      existingFP = prev.fp || null;
    }
  } catch (err) {
    console.log(`feed read failed: ${err.message}`);
  }

  // Pending = games still worth fetching this cycle (non-final OR
  // final-but-not-yet-processed).
  const pending = liveOrFinal.filter((g) => !state.doneFinal[g.gamePk]);

  // Build today's-state-of-the-world payload (used regardless of
  // whether we have any games to fetch this tick).
  //
  // `fp` is a content fingerprint (excludes wall-clock fields) so two
  // ticks with identical baseball state produce identical fingerprints.
  // Compared against `existingFP` from KV to skip no-op writes — see the
  // 2026-05-03 KV-cost reduction below.
  const updatedAt = new Date().toISOString();
  const buildPayload = (hrs, fp) => ({
    date,
    updatedAt,
    gamesTotal: games.length,
    gamesLiveOrFinal: liveOrFinal.length,
    hrCount: hrs.length,
    hrs: hrs.slice().sort((a, b) => (b.time || "").localeCompare(a.time || "")),
    games: games.map((g) => ({
      gamePk: g.gamePk,
      state: g.state,
      abstract: g.abstract,
      home: g.homeAbbr || g.homeName,
      away: g.awayAbbr || g.awayName,
      venue: g.venue,
      gameDate: g.gameDate,
    })),
    fp,
  });

  // Off-day or all-final-and-processed — nothing to fetch this tick.
  //
  // 2026-05-03 KV-cost fix: previously this path always wrote FEED to
  // refresh updatedAt. With cron firing 1440x/day and a 1k/day write
  // budget on the free tier, idle-tick writes alone exceeded the limit
  // (90% warning email 2026-05-03). Now we fingerprint the payload and
  // skip the write when nothing meaningful changed. The dashboard's
  // existing payload (with its original updatedAt) is still served from
  // KV — slightly stale `updatedAt` is acceptable when the underlying
  // content is identical.
  if (pending.length === 0) {
    const idleFP = fingerprint(games, existingHRs);
    if (idleFP === existingFP) {
      console.log(
        `refresh: idle tick (skip-write, fp-stable; games=${games.length} ` +
        `live=${liveOrFinal.length} hrs=${existingHRs.length} ` +
        `done=${Object.keys(state.doneFinal).length})`
      );
      return {
        date,
        updatedAt,
        hrCount: existingHRs.length,
        skipped: true,
        reason: "no-pending-fp-stable",
      };
    }
    // Fingerprint changed — write once to capture the new state. Most
    // common cases: first idle tick after a Final game finished (games
    // array states changed), or first tick of a new day (existingFP=null).
    const payload = buildPayload(existingHRs, idleFP);
    await env.LIVE_HR_KV.put(FEED_KEY(date), JSON.stringify(payload), {
      expirationTtl: 60 * 60 * 36,
    });
    console.log(
      `refresh: idle tick (write, fp-changed; games=${games.length} ` +
      `live=${liveOrFinal.length} hrs=${existingHRs.length} ` +
      `done=${Object.keys(state.doneFinal).length})`
    );
    return { ...payload, skipped: true, reason: "no-pending-fp-changed" };
  }

  // Pick this tick's games via round-robin cursor. Wrap-around when
  // cursor >= pending.length so we don't strand games at the tail.
  const start = pending.length > 0 ? state.cursor % pending.length : 0;
  const tickGames = [];
  for (let i = 0; i < N_PER_TICK && i < pending.length; i++) {
    tickGames.push(pending[(start + i) % pending.length]);
  }

  // Parallel fetch — concurrency capped by N_PER_TICK so total in-flight
  // requests stay tiny. JSON.parse is CPU-bound per response, not concurrent.
  const concurrency = Math.min(N_PER_TICK, tickGames.length);
  let i = 0;
  const newHRsByGame = {};
  async function workerFn() {
    while (i < tickGames.length) {
      const idx = i++;
      const g = tickGames[idx];
      try {
        const pbp = await fetchPlayByPlay(g.gamePk);
        if (pbp) newHRsByGame[g.gamePk] = extractHRs(pbp, g);
      } catch (err) {
        console.log(`pbp ${g.gamePk} failed: ${err.message}`);
      }
    }
  }
  await Promise.all(Array.from({ length: concurrency }, workerFn));

  // Mark Final games as done so we never refetch them.
  for (const g of tickGames) {
    if (g.abstract === "Final") {
      state.doneFinal[g.gamePk] = true;
    }
  }

  // Merge: keep existing HRs from games NOT processed this tick;
  // replace HRs for games we DID process (in case at-bats progressed).
  const processedGpks = new Set(tickGames.map((g) => g.gamePk));
  const keptHRs = existingHRs.filter((h) => !processedGpks.has(h.gamePk));
  const allHRs = [...keptHRs];
  for (const gpk of Object.keys(newHRsByGame)) {
    allHRs.push(...newHRsByGame[gpk]);
  }

  // Advance cursor past the games we just processed.
  state.cursor = pending.length > 0
    ? (start + tickGames.length) % pending.length
    : 0;

  // 2026-05-03 KV-cost fix: skip the FEED write when the fingerprint
  // matches what's already in KV. Cursor/doneFinal mutations require a
  // STATE write either way (round-robin progress is real state), but the
  // FEED only needs to update when baseball-relevant content actually
  // changed. Most active ticks during games-in-progress complete a PBP
  // fetch that yields zero new HRs (HRs are sparse events) — we still
  // mark Final games done in STATE, but we don't need to rewrite the
  // identical FEED.
  const tickFP = fingerprint(games, allHRs);
  const feedChanged = tickFP !== existingFP;

  if (feedChanged) {
    const payload = buildPayload(allHRs, tickFP);
    await env.LIVE_HR_KV.put(FEED_KEY(date), JSON.stringify(payload), {
      expirationTtl: 60 * 60 * 36,
    });
  }
  await env.LIVE_HR_KV.put(STATE_KEY(date), JSON.stringify(state), {
    expirationTtl: 60 * 60 * 36,
  });

  console.log(
    `refresh: tick ${tickGames.length}/${pending.length} pending ` +
    `(cursor->${state.cursor}, doneFinal=${Object.keys(state.doneFinal).length}, ` +
    `hrs=${allHRs.length}, gpks=${tickGames.map((g) => g.gamePk).join(",")}, ` +
    `feed=${feedChanged ? "write" : "skip"})`
  );

  // Return the payload regardless of whether we wrote it — the manual
  // refresh endpoint and the cold-cache fetch path both rely on the
  // return value. When we skipped the write, fall back to a payload
  // built from existing data so the response shape stays the same.
  return feedChanged ? buildPayload(allHRs, tickFP) : buildPayload(existingHRs, existingFP);
}

// ---------------------------------------------------------------------------
// Worker entry points
// ---------------------------------------------------------------------------

export default {
  // Cron trigger.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(refresh(env));
  },

  // HTTP fetch handler.
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    if (url.pathname === "/api/live-hrs") {
      const tz = env.MLB_TIMEZONE || "America/New_York";
      const date = url.searchParams.get("date") || todayInTZ(tz);

      let body = await env.LIVE_HR_KV.get(FEED_KEY(date));

      // Cold start / first request of the day: do a synchronous refresh so
      // the user doesn't see an empty page until the first cron tick fires.
      if (!body && date === todayInTZ(tz)) {
        const fresh = await refresh(env);
        body = JSON.stringify(fresh);
      }

      if (!body) {
        return new Response(
          JSON.stringify({ date, hrs: [], hrCount: 0, updatedAt: null }),
          {
            status: 200,
            headers: {
              "Content-Type": "application/json",
              "Cache-Control": `public, max-age=${PUBLIC_CACHE_SECONDS}`,
              ...CORS_HEADERS,
            },
          }
        );
      }

      return new Response(body, {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "Cache-Control": `public, max-age=${PUBLIC_CACHE_SECONDS}`,
          ...CORS_HEADERS,
        },
      });
    }

    // Manual refresh hook — handy for debugging. Hit it from your browser
    // to force a fetch outside the cron cadence.
    if (url.pathname === "/api/live-hrs/refresh") {
      const fresh = await refresh(env);
      return new Response(JSON.stringify(fresh), {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          ...CORS_HEADERS,
        },
      });
    }

    if (url.pathname === "/api/health") {
      return new Response(
        JSON.stringify({ ok: true, ts: new Date().toISOString() }),
        {
          status: 200,
          headers: { "Content-Type": "application/json", ...CORS_HEADERS },
        }
      );
    }

    return new Response("Not found", { status: 404, headers: CORS_HEADERS });
  },
};
