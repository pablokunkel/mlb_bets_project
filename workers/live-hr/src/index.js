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
  const url = `${MLB_API}/schedule?sportId=1&date=${date}`;
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

async function fetchPlayByPlay(gamePk) {
  const url = `${MLB_API}/game/${gamePk}/playByPlay`;
  const resp = await fetch(url, {
    headers: { "User-Agent": "dingersonly-live-hr/0.1" },
    // No CDN cache here — we want fresh play data.
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

  // Short-circuit: no games today means nothing to fetch and (after the
  // first tick of the day) nothing to write either. The fingerprint check
  // below handles the "after the first tick" part automatically.
  const liveOrFinal = games.filter(
    (g) => g.abstract === "Live" || g.abstract === "Final"
  );

  let allHRs = [];
  if (liveOrFinal.length > 0) {
    // Pull play-by-play in parallel, but cap concurrency so we don't hammer
    // the API. 6 in flight is plenty for ~15 games.
    const concurrency = 6;
    let i = 0;
    async function worker() {
      while (i < liveOrFinal.length) {
        const idx = i++;
        const g = liveOrFinal[idx];
        try {
          const pbp = await fetchPlayByPlay(g.gamePk);
          if (pbp) allHRs.push(...extractHRs(pbp, g));
        } catch (err) {
          console.log(`pbp ${g.gamePk} failed: ${err.message}`);
        }
      }
    }
    await Promise.all(
      Array.from({ length: Math.min(concurrency, liveOrFinal.length) }, worker)
    );

    // Newest first.
    allHRs.sort((a, b) => (b.time || "").localeCompare(a.time || ""));
  }

  const fp = fingerprint(games, allHRs);

  // Compare fingerprint to last write. If unchanged, skip both KV writes.
  // We still return the assembled payload for /refresh callers.
  let prevFingerprint = null;
  try {
    const prevMetaRaw = await env.LIVE_HR_KV.get(META_KEY(date));
    if (prevMetaRaw) {
      const prev = JSON.parse(prevMetaRaw);
      prevFingerprint = prev.fingerprint || null;
    }
  } catch (err) {
    console.log(`meta read failed: ${err.message}`);
  }

  const updatedAt = new Date().toISOString();
  const payload = {
    date,
    updatedAt,
    gamesTotal: games.length,
    gamesLiveOrFinal: liveOrFinal.length,
    hrCount: allHRs.length,
    hrs: allHRs,
    games: games.map((g) => ({
      gamePk: g.gamePk,
      state: g.state,
      abstract: g.abstract,
      home: g.homeAbbr || g.homeName,
      away: g.awayAbbr || g.awayName,
      venue: g.venue,
      gameDate: g.gameDate,
    })),
  };

  if (prevFingerprint === fp) {
    console.log(
      `refresh: unchanged (games=${games.length} live=${liveOrFinal.length} hrs=${allHRs.length}) — skipping KV write`
    );
    return { ...payload, skipped: true };
  }

  await env.LIVE_HR_KV.put(FEED_KEY(date), JSON.stringify(payload), {
    // Expire after 36h — keeps yesterday around briefly for late-night fans,
    // then garbage-collects automatically.
    expirationTtl: 60 * 60 * 36,
  });
  await env.LIVE_HR_KV.put(
    META_KEY(date),
    JSON.stringify({
      updatedAt,
      hrCount: allHRs.length,
      gamesLiveOrFinal: liveOrFinal.length,
      fingerprint: fp,
    }),
    { expirationTtl: 60 * 60 * 36 }
  );

  console.log(
    `refresh: wrote (games=${games.length} live=${liveOrFinal.length} hrs=${allHRs.length})`
  );
  return payload;
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
