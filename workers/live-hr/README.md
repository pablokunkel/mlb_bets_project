# dingersonly-live-hr

Cloudflare Worker that powers the **HR Recap** tab on dingersonly.cc.

- A cron trigger fires every minute, pulls today's MLB schedule, walks each
  live/final game's play-by-play, extracts every home run, and caches the
  assembled feed in **Workers KV**.
- `GET /api/live-hrs` serves the cached JSON to the static site.

## Why a separate Worker (not the existing `dingersonlybot`)

- Independent deploys — pushing the bot can't break the site, and vice versa.
- Different traffic profile — the bot is event-driven, this Worker is cron +
  high-read. They want different cache rules.
- Failure isolation — KV write hiccups here don't take the bot offline.

## One-time setup

Run these from `workers/live-hr/`:

```bash
# 1. Install wrangler if you don't have it.
npm install

# 2. Log in.
npx wrangler login

# 3. Create the KV namespace (production + preview).
npx wrangler kv namespace create LIVE_HR_KV
npx wrangler kv namespace create LIVE_HR_KV --preview
```

The two `id` values printed go into `wrangler.toml` under `[[kv_namespaces]]`
(`id` and `preview_id` respectively).

## Deploy

```bash
npx wrangler deploy
```

That registers the Worker, the cron trigger (`* * * * *`), and the routes.

### Bind to api.dingersonly.cc

`dingersonly.cc` itself is served by a Worker-with-Static-Assets
(`dingersonlybot`), and Cloudflare won't let another Worker take routes on
a hostname owned by an assets Worker. So we bind this Worker to its own
subdomain:

1. Cloudflare dashboard → Workers → `dingersonly-live-hr`.
2. Settings → Domains & Routes → **Add Custom Domain**.
3. Domain: `api.dingersonly.cc`. CF auto-creates the DNS record + cert.

Or, after the first successful deploy, uncomment the `[[routes]]` block in
`wrangler.toml` and `npx wrangler deploy` again — wrangler will create the
custom domain for you.

## Verify

```bash
# Force a refresh and inspect the JSON
curl https://api.dingersonly.cc/api/live-hrs/refresh | jq .

# Normal read (what the browser does)
curl https://api.dingersonly.cc/api/live-hrs | jq '.hrCount, .updatedAt'

# Watch the cron in real time
npx wrangler tail
```

You should see one invocation per minute. During off-hours / off-season the
Worker still fires, hits `/schedule`, sees zero live games, and writes a
mostly-empty payload — that's expected and free.

## What ends up in KV

Two keys per day, both 36h TTL:

- `live-hrs:YYYY-MM-DD` — full payload the site reads.
- `live-hrs:meta:YYYY-MM-DD` — small summary (count, updatedAt) for cheaper
  health checks if we want them later.

## Cost

- Cron: 1440 invocations/day = ~43k/month. Free tier allows 100k requests/day.
- KV writes are **deduped by content fingerprint** — the Worker only writes
  when the set of games + their abstract states + the set of HR ids changes.
  In practice that's:
  - off-day / off-season: 1 write/day (the first tick of the day)
  - in-season: roughly `2 × (games scheduled) + (HRs hit)` writes per day,
    typically well under the 1k/day free-tier write quota.
- KV reads stay bounded by visitor traffic + the 20s edge cache.

If you want to verify the dedupe is working, `wrangler tail` shows
`refresh: unchanged` lines for skipped writes and `refresh: wrote` for
real ones.

## Rollback

If anything goes sideways:

```bash
# Remove the route in dashboard (Settings → Triggers → Routes → trash icon)
# OR roll back to a previous version:
npx wrangler deployments list
npx wrangler rollback --version-id <previous>
```

The site degrades gracefully when `/api/live-hrs` 404s — the panel just shows
"Live feed is unavailable right now."

## Local dev

```bash
npx wrangler dev
# then in another shell
curl http://localhost:8787/api/live-hrs/refresh | jq .
```

`wrangler dev` does NOT run the cron locally. Hit `/api/live-hrs/refresh`
manually to populate the local KV preview.
