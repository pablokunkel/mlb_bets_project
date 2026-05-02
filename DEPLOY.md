# Deploy & Release

Single source of truth for how MLB HR Bets ships to production. Last updated 2026-05-02.

## Surfaces

| Surface | URL | Mechanism | Source |
|---|---|---|---|
| Dashboard | https://dingersonly.cc | Cloudflare Worker `dingersonlybot` (Static Assets binding). Auto-deploys via CF Workers Builds GitHub integration on push to `main`. | `mlb_hr_bet_site/` |
| Live HR feed API | https://api.dingersonly.cc | Cloudflare Worker `dingersonly-live-hr`. Cron-triggered every minute, early-exits when no games scheduled. | `workers/live-hr/` |
| Picks generation | (not user-facing) | Local Windows Task Scheduler on Pablo's machine | Project root |

## How the dashboard ships

`wrangler.jsonc` at the repo root tells CF Workers Builds what to deploy:

```jsonc
{
  "name": "dingersonlybot",
  "compatibility_date": "2026-05-02",
  "assets": { "directory": "mlb_hr_bet_site" },
  "compatibility_flags": ["nodejs_compat"]
}
```

Flow: `git push origin main` → CF Workers Builds runs `npx wrangler versions upload` → new version uploaded → automatically promoted to active production traffic. Total time: ~2-3 minutes.

To verify a deploy:

```bash
# expected status code
curl -s -o /dev/null -w "%{http_code}\n" https://dingersonly.cc      # 200

# verify expected new code is in the served HTML
curl -s https://dingersonly.cc | grep -c liveHRSection                # >0

# CI status from any open PR
gh pr checks <PR-number>                                              # Workers Builds: dingersonlybot   pass
```

## How the live HR worker ships (manual)

```cmd
cd workers/live-hr
npx wrangler deploy
```

Requires `wrangler login` once per machine. Worker config in `workers/live-hr/wrangler.toml`. Cron trigger, KV namespace, and custom domain (`api.dingersonly.cc`) are already provisioned in the CF dashboard — wrangler just uploads new code.

To verify the worker:

```bash
curl -s https://api.dingersonly.cc/api/health                # {"ok":true,"ts":"..."}
curl -s https://api.dingersonly.cc/api/live-hrs | head -c 200

# tail live cron log
cd workers/live-hr && npx wrangler tail
# expect "refresh: ..." line each minute
```

## Daily / nightly pipeline (runs on Pablo's PC)

Three Windows scheduled tasks. None of them touch Cloudflare directly — they all `git push` and let the CF Workers Builds integration handle deploy.

| Task | Time (ET) | Script | Purpose |
|---|---|---|---|
| `MLB_HR_Daily_Picks` | 12:00 PM | `run_daily.bat` | Morning ETL → score slate → write picks → export JSON → git push |
| `MLB_HR_Outcomes` | 1:00 AM | `run_outcomes.bat` | Pull yesterday's outcomes → backtest → re-export → git push |
| `MLB_HR_Nightly` | 2:00 AM | `run_nightly.bat` | Statcast / arsenals / season stats refresh (no deploy) |

### `run_daily.bat` (noon)
1. `python -m etl.etl_morning` — schedule, lineups, weather → DB
2. `python generate_picks.py` — score slate → `results/picks_<DATE>.json`
3. `python load_picks_to_db.py` — persist full board to `daily_picks` + `pick_inputs`
4. `python export_site_data.py` — DB → `mlb_hr_bet_site/data/*.json`
5. Commit + push (commit message: `Daily update <DATE>`)

### `run_outcomes.bat` (1 AM)
1. `python -m etl.etl_outcomes` — pulls yesterday's box scores → `outcomes` table
2. `python backtest_factors.py` — re-scores history → `mlb_hr_bet_site/data/factor_accuracy.json`
3. `python export_site_data.py` — re-export so HR recap, hit rates, history reflect last night
4. Commit + push (commit message: `Outcomes + accuracy refresh`)

### `run_nightly.bat` (2 AM)
1. `python -m etl.etl_nightly` — incremental Statcast HR events, pitcher arsenals (>7d stale), victim profiles, season batting/pitching, park factors
2. `python prewarm_cache.py` — pre-warms Statcast cache for next noon run

No deploy here. Refreshes upstream data for tomorrow's noon scoring.

## Database

SQLite, single file: `C:\Users\pablo\OneDrive\Documents\Claude\Projects\data\hr_bets.db`

Sibling of the project dir, NOT inside it. All ETL scripts resolve via `etl/db.py:get_db()` which centralizes the path. WAL mode enabled. Schema in `etl/db.py`.

## How to ship a manual change

For Python pipeline changes (scoring, ETL, backtest):

```cmd
git add <files>
git commit -m "<change description>"
git push origin main
```

The change takes effect on the **next** noon run unless you trigger `run_daily.bat` manually. `git push` auto-deploys the dashboard but doesn't re-run the picks generation.

For dashboard / JSON changes:

```cmd
git add mlb_hr_bet_site/<files>
git commit -m "<change description>"
git push origin main
```

CF auto-deploys within ~2-3 min. Verify with `curl -s https://dingersonly.cc | grep -c '<expected string>'`.

## Environment / secrets

- `VEGAS_ODDS_API_KEY` — in `<project>/.env`, auto-loaded by `features_v2._load_dotenv()`. Required for Vegas implied-totals signal in matchup scoring. Currently unset on production runs (Vegas signal silently degrades to "missing" — see audit MED finding HIGH #5).
- Cloudflare Worker secrets — managed in CF dashboard (NOT in this repo).
- No GitHub Actions secrets currently active. The dead `NETLIFY_AUTH_TOKEN` and `NETLIFY_SITE_ID` should be deleted from repo settings — see [Followup](#followup).

## Failure modes & rollback

If a deploy breaks production:

1. **CF Workers Builds shows red** on the most recent commit's build. Check `gh pr checks <PR>` or open the build URL from the CF dashboard.
2. **Roll back via the CF dashboard**: Workers & Pages → `dingersonlybot` → Deployments → click the previous "active" version → "Promote to production". Takes <30 seconds.
3. **Or revert the commit**: `git revert <hash> && git push origin main`. CF auto-deploys the revert in ~2-3 min.
4. **For a daily-pipeline failure** (noon task errored): the ETL script's failure is logged to `logs/<task>_<DATE>.log`. The site continues serving yesterday's picks until the next successful run. Manually trigger `run_daily.bat` after fixing.

## Related docs

- `How_The_HR_Model_Works.md` — model architecture and scoring methodology
- `ARCHITECTURE.md` — component map and data flow
- `WEIGHT_REFIT_LOG.md` — history of monthly weight refits and their decisions
- `diagnostics/README.md` — investigation tooling (after C4 cleanup lands)

## Followup

Tasks that aren't repo edits (so they can't ride in any of the existing PRs):

- [ ] Delete `NETLIFY_AUTH_TOKEN` and `NETLIFY_SITE_ID` from GitHub repo Settings → Secrets and variables → Actions
- [ ] Delete the stale `cloudflare/workers-autoconfig` branch on `origin` once the bot stops re-pushing it (it's redundant — `wrangler.jsonc` is on `main` directly now)
- [ ] Configure CF Workers Builds to NOT trigger on every branch push — only on `main`. (Currently every PR branch push runs a build, which is harmless but noisy.)
