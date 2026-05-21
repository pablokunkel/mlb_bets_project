# Hosting runbook — moving the daily pipeline off Pablo's laptop

**Status (2026-05-21): LIVE.** All three scheduled workflows are active and have been running cleanly since the 2026-05-12 cutover (`daily-picks` at 13:07 UTC, `outcomes-refresh` at 06:00 UTC, `nightly-refresh` at 08:00 UTC — see Actions tab for recent runs). **Cloudflare R2 is the source of truth for `hr_bets.db`**; the laptop / OneDrive copy is no longer authoritative. Any local DB write that doesn't go through the R2 push/pull cycle (e.g., `python -m etl.backfill_2025` run on the laptop) is doomed — the next scheduled job pulls R2's copy and overwrites the local file.

If you need to mutate the production DB outside the scheduled jobs, the right paths are:
- **Run via a workflow** — preferred. Add a new `workflow_dispatch` job that mirrors the daily/outcomes/nightly pattern (R2 pull → run → R2 push). See `.github/workflows/backfill-2025.yml` for the canonical template.
- **Failover path** — laptop run, but bookended by manual R2 sync. See "Failover" section below. Use this only when GH Actions is down.

This doc walks through the migration that already happened (kept for reference / re-doing on a new repo) and the failover path.

---

## What changes vs. today

| | Today (laptop) | Hosted (GitHub Actions + R2) |
|---|---|---|
| **Picks generation** | `run_daily.bat` at noon ET via Task Scheduler | `daily-picks.yml` at 15:30 UTC |
| **Outcomes refresh** | `run_outcomes.bat` at 1 AM ET | `outcomes-refresh.yml` at 06:00 UTC |
| **Nightly Statcast** | `run_nightly.bat` at 2 AM ET | `nightly-refresh.yml` at 08:00 UTC |
| **`hr_bets.db` home** | `C:\…\data\hr_bets.db` (OneDrive sync) | Cloudflare R2 bucket, pulled at job start / pushed at end |
| **Git push** | local `git push origin main` | runner's `git push origin main` (same effect) |
| **CF Pages auto-deploy** | unchanged | unchanged |
| **Live HR worker (`api.dingersonly.cc`)** | unchanged (already on CF) | unchanged |

Nothing about the site, the Cloudflare workers, or the model itself changes. We're just relocating where the cron runs.

---

## Bootstrap — one-time setup

### 1. Create the R2 bucket

In the Cloudflare dashboard:

1. **R2 → Create bucket** → name it `mlb-hr-bets-db` (or whatever — just remember it).
2. Don't enable public access. The bucket is private; the API token below is the only way in.
3. Note the **Account ID** shown in the dashboard sidebar (32-hex string).

### 2. Create an R2 API token

In the Cloudflare dashboard:

1. **R2 → Manage R2 API Tokens → Create API Token**.
2. Permissions: **Object Read & Write**.
3. Scope: limit to the `mlb-hr-bets-db` bucket only.
4. TTL: forever (or 1 year if you'd rather rotate annually).
5. Save the **Access Key ID** and **Secret Access Key** somewhere safe — Cloudflare only shows the secret once.

### 3. Add GitHub Actions secrets

In the GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**. Add five:

| Secret name | Value |
|---|---|
| `R2_ACCOUNT_ID` | the 32-hex Cloudflare account ID from step 1 |
| `R2_ACCESS_KEY_ID` | from step 2 |
| `R2_SECRET_ACCESS_KEY` | from step 2 |
| `R2_BUCKET` | `mlb-hr-bets-db` (or whatever you named it) |
| `VEGAS_ODDS_API_KEY` | copy from your local `.env` |

`CLOUDFLARE_API_TOKEN` and `GITHUB_TOKEN` are already in place from the existing live-hr workflow.

### 4. Seed R2 with your current DB (from the laptop)

The scheduled jobs pull `hr_bets.db` from R2 every time. We need to upload your laptop's DB once so the first job has something to pull.

From the project root **on the laptop** (not in a worktree):

```powershell
# Make sure your .env at the repo root has the five R2_* values added.
# (Or pass them as env vars on the command line.)
python infra/seed_r2_db.py --dry-run    # confirm wiring
python infra/seed_r2_db.py               # actual upload
```

The script refuses to overwrite an existing remote DB unless you pass `--force`. That's the safety net for "oops I ran this twice."

### 5. Test the daily workflow manually

In GitHub: **Actions → Daily picks (noon ET) → Run workflow → main → Run**.

Watch the run. If it goes green and you see today's `Daily update …` commit on `main` from `github-actions[bot]`, the wiring works. Verify the site shows the new card at https://dingersonly.cc within 2-3 minutes.

Repeat for `Outcomes + accuracy refresh` and `Nightly Statcast refresh`.

### 6. Cut over: enable the schedules + retire the laptop crons

When all three workflows have run cleanly via manual dispatch a few times:

1. Edit each `.yml` and uncomment the `schedule:` block.
2. On the laptop, open Task Scheduler and **disable** (don't delete) the three daily tasks. Keep them around as a known-good fallback.
3. Watch the next morning's noon UTC run land on its own.

---

## Secrets summary

| Secret | Where it's set | Used by |
|---|---|---|
| `R2_ACCOUNT_ID` | GH Actions secrets | all three workflows (composite action) |
| `R2_ACCESS_KEY_ID` | GH Actions secrets | all three workflows |
| `R2_SECRET_ACCESS_KEY` | GH Actions secrets | all three workflows |
| `R2_BUCKET` | GH Actions secrets | all three workflows |
| `VEGAS_ODDS_API_KEY` | GH Actions secrets | daily-picks.yml, outcomes-refresh.yml |
| `CLOUDFLARE_API_TOKEN` | GH Actions secrets (existing) | deploy-live-hr.yml only |
| `GITHUB_TOKEN` | injected automatically | daily-picks.yml, outcomes-refresh.yml (push) |

You also still need the five `R2_*` values in your laptop's `.env` if you ever want to run `infra/seed_r2_db.py` or `infra/r2_sync.py` locally (e.g., for a manual sync after debugging).

---

## Verifying a run

For each workflow, the GH Actions log will surface:

- The `Pull hr_bets.db from R2` step inside `setup-mlb-env`: prints DB size + a row count from `daily_picks` as a sanity check.
- Each numbered pipeline step (`[1/5]`, `[2/5]`, etc.) — matches the .bat output for grep-friendliness.
- For `daily-picks.yml`, a final smoke check polls `https://dingersonly.cc/data/slate_date.json` after a 3-minute CF deploy delay and warns (not fails) if the new date isn't yet present.

If something looks off mid-run, the workflow's `Push hr_bets.db back to R2` step is gated on `if: success()` — so a failed pipeline does **not** clobber the remote DB. Re-running the workflow re-pulls the last good state.

---

## Failover — running the pipeline manually if GH Actions is down

GitHub Actions does have outages. When that happens:

1. **Pull the latest DB from R2 to the laptop:**
   ```powershell
   python infra/r2_sync.py pull
   ```
2. **Re-enable the Task Scheduler tasks** (they're disabled, not deleted) or run the `.bat` files manually.
3. After the laptop run finishes, **push back to R2:**
   ```powershell
   python infra/r2_sync.py push
   ```
4. When GH Actions is back up, just let the next scheduled run resume — it'll pull the laptop-updated DB and continue normally.

---

## DST + cron drift — the timezone footnote

GitHub Actions cron is UTC-only, and best-effort (5-30 min drift during peak hours). The schedules:

| Workflow | Cron | EDT (Apr-Oct) | EST (Nov-Mar) |
|---|---|---|---|
| daily-picks | `30 15 * * *` | 11:30 AM ET | 10:30 AM ET |
| outcomes-refresh | `0 6 * * *` | 2:00 AM ET | 1:00 AM ET |
| nightly-refresh | `0 8 * * *` | 4:00 AM ET | 3:00 AM ET |

The MLB regular season is entirely in EDT (Apr-Oct), so the EDT row is what matters. The EST drift in March and November is fine — March games are rare and late; November is postseason and the model isn't tuned for it anyway.

We schedule `daily-picks` at 11:30 AM ET rather than mirror the laptop's noon: it gives ~90 min of cushion before the earliest first pitches (1:05 PM ET on weekday day games), absorbing typical GH Actions cron drift.

---

## What we are NOT changing

- **`run_daily.bat` / `run_outcomes.bat` / `run_nightly.bat`** stay in the repo. They're the documented fallback. Don't delete them.
- **`setup_daily_task.bat`** stays — same reason.
- **`etl/db.py`'s `DB_PATH`** stays at `<project_parent>/data/hr_bets.db`. The R2 sync writes to that exact path on the runner; no code change needed.
- **Local `.env` loading in `features_v2.py`** stays. It's harmless on GH Actions (the file isn't there, env vars are set directly).

---

## Cost expectations

Estimates use typical observed runtimes, not the workflow `timeout-minutes:` ceilings.

| Resource | Free tier | Expected use | Net |
|---|---|---|---|
| GH Actions minutes (private repo, Free plan) | 2,000 min/mo | ~1,800 min/mo (daily ~600 + outcomes ~300 + nightly ~900) | $0 (inside free tier) |
| GH Actions minutes (private repo, Pro $4/mo) | 3,000 min/mo | ~1,800 min/mo | $4/mo flat |
| GH Actions minutes (public repo) | unlimited | ~1,800 min/mo | $0 |
| R2 storage | 10 GB | ~32 MB | $0 |
| R2 Class A ops (write) | 1M/mo | ~120/mo (3 jobs × ~40 PUT/copy ops × 30 days) | $0 |
| R2 Class B ops (read) | 10M/mo | ~90/mo | $0 |
| R2 egress | free (all egress) | — | $0 |

**Decision: stay private.** ~1,800 min/mo fits inside the 2,000 Free-plan minute budget with ~200 min of headroom for manual reruns and debugging. If usage drifts past free, GitHub Pro at $4/mo bumps the cap to 3,000 min — still cheap, and avoids the "audit every commit before flipping public" tax.

**Cost levers if you ever need to trim minutes:**
- Drop `nightly-refresh.yml` to every other day (saves ~450 min/mo). The Statcast deltas day-over-day are usually small; an every-48h refresh would barely affect picks quality.
- Make `prewarm_cache.py` no-op when the GH Actions cache already has yesterday's pulls.
- Move `nightly-refresh.yml` to a manual-only trigger and run it weekly via `workflow_dispatch`.

---

## Open questions for after the migration

(Not blockers — capture them for `BACKLOG.md`.)

- **pybaseball cache hit rate** — the composite action caches `/tmp/cache` per `run_id` which is too aggressive. After the first week of runs, look at cache hit/miss in the logs and consider keying by date-week instead.
- **R2 lifecycle policy** — set the `.staging` key pattern to auto-delete after 1 day so orphaned uploads from failed pushes get cleaned up.
- **Cron drift monitoring** — add a check at the end of each workflow that emits a metric (or just a `::warning::`) if the scheduled-vs-actual gap exceeded N minutes. Helps catch the "GH Actions started lagging by 45 min" failure mode before we miss a first pitch.
- **Workflow concurrency under retries** — `concurrency.group: hr-bets-db` serializes within a workflow; verify it also serializes across the three. (It does — group is global within the repo — but worth a manual test.)
