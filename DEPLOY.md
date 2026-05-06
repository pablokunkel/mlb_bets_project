# Deploy & Release

Single source of truth for how MLB HR Bets ships to production. Last updated 2026-05-06.

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

## How the live HR worker ships

### Auto-deploy (preferred): GitHub Action

`.github/workflows/deploy-live-hr.yml` (added 2026-05-04, PR #31) auto-deploys `dingersonly-live-hr` on every push to `main` that touches `workers/live-hr/**`. The companion `dingersonlybot` worker (the static-site assets one) auto-deploys via the CF Workers Builds GitHub integration wired up at the dashboard level — but `dingersonly-live-hr` lives in `workers/live-hr/` with its own `wrangler.toml` and has no Workers Builds binding. Without the Action, a merged worker change never reached production. That bit us on PR #22 (KV cost fix): merged 2026-05-03, never deployed, the KV-write quota warning email re-fired the next day with the patch sitting unused on `main`.

The Action requires the `CLOUDFLARE_API_TOKEN` repo secret. `workflow_dispatch` is enabled so you can manually re-deploy from the Actions tab without a code change (e.g., to force-resync after a CF outage rollback).

### Manual deploy (fallback)

```cmd
cd workers/live-hr
npx wrangler deploy --config wrangler.toml
```

The `--config wrangler.toml` flag is **required**. Without it, wrangler walks up the directory tree to the repo-root `wrangler.jsonc` and tries to deploy `dingersonlybot` (the static-assets worker) using the live-hr source, which fails. Burned us once; always pass `--config` for manual deploys of this worker.

Requires `wrangler login` once per machine. Cron trigger, KV namespace, and custom domain (`api.dingersonly.cc`) are already provisioned in the CF dashboard — wrangler just uploads new code.

To verify the worker:

```bash
curl -s https://api.dingersonly.cc/api/health                # {"ok":true,"ts":"..."}
curl -s https://api.dingersonly.cc/api/live-hrs | head -c 200

# tail live cron log
cd workers/live-hr && npx wrangler tail
# expect "refresh: ..." line each minute
```

### KV write budget

The worker is on Cloudflare's Free tier — 1,000 KV writes/day. The cron runs every minute (1,440 invocations) and originally wrote the FEED + STATE keys on every tick, which silently exceeded the daily budget on busy game days. Two optimizations brought writes down to ~80-130/day:

- **PR #22 (2026-05-03):** content-fingerprint check on the FEED before writing. If `hash(games + hrs)` matches the previous tick, skip the write.
- **PR #37 (2026-05-05):** same fingerprint check on the STATE write (cursor + doneFinal). Off-game minutes and quiet stretches now skip both writes entirely.

Tail `wrangler tail` and look for `feed unchanged, skip` / `state unchanged, skip` lines as the steady-state behavior between game events.

## Daily / nightly pipeline (runs on Pablo's PC)

Three Windows scheduled tasks. None of them touch Cloudflare directly — they all `git push` and let the CF Workers Builds integration handle deploy.

| Task | Time (ET) | Script | Purpose |
|---|---|---|---|
| `MLB_HR_Daily_Picks` | 12:00 PM | `run_daily.bat` | Morning ETL → score slate → write picks → export JSON → git push |
| `MLB_HR_Outcomes` | 1:00 AM | `run_outcomes.bat` | Pull yesterday's outcomes → backtest → re-export → git push |
| `MLB_HR_Nightly` | 2:00 AM | `run_nightly.bat` | Statcast / arsenals / season stats refresh (no deploy) |

### `run_daily.bat` (noon)
0a. `taskkill /F /IM python.exe` — reap any zombie `python.exe` processes from a Ctrl-Cd or scheduler-killed prior run. Without this, zombies hold pybaseball cache locks + DB connections and step `[2]` hangs. Added 2026-05-05 after two consecutive days of noon hangs.
0b. `git pull --rebase --autostash origin main` — pull merged PRs into local main BEFORE running picks. Soft-fail (warns + proceeds with stale code) if the pull fails. Added 2026-05-03 after a noon-run failure caused by `main` being 12 commits behind the merged PR that fixed the crashing code.
1. `python -m etl.etl_morning` — schedule, lineups, weather → DB
2. `python generate_picks.py` — score slate → `results/picks_<DATE>.json`
3. `python load_picks_to_db.py` — persist full board to `daily_picks` + `pick_inputs`
4. `python -m etl.etl_outcomes` — refresh yesterday's outcomes + HR events. Self-heal step (added 2026-05-03): if last night's 1 AM `run_outcomes.bat` failed or didn't push, the dashboard's HR Recap goes blank for yesterday at the same time the live tracker advances to today. Re-running `etl_outcomes` here is idempotent (`INSERT OR REPLACE`) and a no-op when 1 AM already succeeded. Soft-fail.
5. `python export_site_data.py` — DB → `mlb_hr_bet_site/data/*.json`
6. `git pull --rebase --autostash origin main` then `git push origin main` (commit message: `Daily update <DATE>`). The pre-push pull was added 2026-05-02 after a noon push got rejected non-fast-forward when 4 PRs merged during the noon window.

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

- [ ] Delete `NETLIFY_AUTH_TOKEN` and `NETLIFY_SITE_ID` from GitHub repo Settings → Secrets and variables → Actions. Status unverified from local — check the Settings page.
- [x] ~~Delete the stale `cloudflare/workers-autoconfig` branch on `origin`~~ — confirmed gone from `git ls-remote origin` as of 2026-05-06.
- [ ] Configure CF Workers Builds to NOT trigger on every branch push — only on `main`. (Currently every PR branch push runs a build, which is harmless but noisy.) Status unverified — check the Workers Builds settings in the CF dashboard.
