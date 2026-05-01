# Deployment Architecture

## How the site gets to Netlify

The dashboard at `mlb-hr-bets.netlify.app` is deployed **directly via the Netlify CLI** — not through a GitHub-linked auto-deploy.

### Why not GitHub auto-deploy?

The original plan was: `run_daily.bat` generates picks → exports JSON → `git push` → Netlify auto-deploys from the repo. This requires linking the GitHub repo (`pablokunkel/mlb_bets_project`) to the Netlify site so Netlify watches for pushes. That linking step can only be done through the Netlify web UI and wasn't automated, so we switched to a simpler approach.

### Current approach: three scheduled tasks, Netlify CLI direct deploy

The pipeline runs as **three separate Windows scheduled tasks** so failures stay isolated and each step runs at the right time of day for its data dependencies:

| Task | Time | Script | Writes |
|---|---|---|---|
| `MLB_HR_Daily_Picks` | 12:00 PM | `run_daily.bat` | `daily_slate`, `daily_lineup`, `daily_picks` → deploys |
| `MLB_HR_Outcomes` | 1:00 AM | `run_outcomes.bat` | `outcomes` → re-deploys |
| `MLB_HR_Nightly` | 2:00 AM | `run_nightly.bat` | Statcast / arsenals / season stats |

#### `run_daily.bat` (noon)
1. `python -m etl.etl_morning` — schedule, lineups, weather to DB
2. `python generate_picks.py` — scores the slate, writes `results/picks_<DATE>.json`
3. `python load_picks_to_db.py` — persists the full board to `daily_picks`, flagging the 8 card picks as `selected=1`
4. `python export_site_data.py` — DB → `mlb_hr_bet_site/data/*.json`
5. `netlify deploy --prod --dir=mlb_hr_bet_site --site=...`

Any step failing aborts the run — we never deploy a half-generated state.

#### `run_outcomes.bat` (1 AM)
1. `python -m etl.etl_outcomes` — pulls yesterday's box scores, fills `outcomes`
2. `python export_site_data.py` — re-export so hit rates, streak, factor analysis reflect last night
3. `netlify deploy --prod ...`

This is the second deploy of the day. Without it, the dashboard would show today's picks but never their results.

#### `run_nightly.bat` (2 AM)
1. `python -m etl.etl_nightly` — incremental Statcast HR events, pitcher arsenals (>7d stale), victim profiles, season batting/pitching, park factors

No deploy here. This refreshes the upstream data that tomorrow's noon scoring run will read from.

### Why this split?

- **Morning ETL must run before scoring.** Probable pitchers and lineups aren't reliable until late morning, so the noon kickoff is the earliest realistic time.
- **Outcomes can only be recorded after games end.** West-coast games finish ~1 AM ET, so the 1 AM ET task is the earliest we can pull complete box scores. (Adjust slightly later if you start missing games.)
- **Nightly Statcast refresh is heavy.** Pitcher arsenals can hit ~250 Savant requests when several go stale at once. Running it at 2 AM keeps it off the noon path so a Savant slowdown doesn't delay the deploy.
- **Failure isolation.** If outcomes ETL fails, tomorrow's picks still ship. If nightly Statcast fails, today's deploy still happens (it just scores against slightly stale arsenals).

### Netlify site details

- **Site ID:** `0fade6bd-ae06-43a8-aaef-22ee692ecbba`
- **URL:** `mlb-hr-bets.netlify.app`
- **Team:** Squall (free tier)
- **Publish directory:** `mlb_hr_bet_site/` (contains `index.html` + `data/*.json`)

### Database

SQLite, single file at `<project parent>/data/hr_bets.db` (i.e. `C:\Users\pablo\OneDrive\Documents\Claude\Projects\data\hr_bets.db`). Schema and helpers in `etl/db.py`. WAL mode enabled so the dashboard exporter can read while the ETLs write.

### Prerequisites on Pablo's machine

- `npm install -g netlify-cli`
- `netlify login` (one-time auth)
- Python 3.9+ with `pybaseball pandas numpy requests` installed
- DB initialized once: `python etl/db.py --create`
- One-time backfill from raw_data.csv: `python backfill_from_csv.py`

### Setting up the new scheduled tasks

The existing `MLB_HR_Daily_Picks` task already points at `run_daily.bat` — no change needed there. Add the two new tasks from an Admin command prompt:

```
schtasks /create /tn "MLB_HR_Outcomes" /tr "\"C:\Users\pablo\OneDrive\Documents\Claude\Projects\MLB HR Bets\run_outcomes.bat\"" /sc daily /st 01:00 /rl HIGHEST /f

schtasks /create /tn "MLB_HR_Nightly" /tr "\"C:\Users\pablo\OneDrive\Documents\Claude\Projects\MLB HR Bets\run_nightly.bat\"" /sc daily /st 02:00 /rl HIGHEST /f
```

Verify all three are registered:
```
schtasks /query /tn "MLB_HR_Daily_Picks"
schtasks /query /tn "MLB_HR_Outcomes"
schtasks /query /tn "MLB_HR_Nightly"
```

### Manual run / debugging

Each `.bat` is idempotent and can be triggered by hand:
```
"C:\Users\pablo\OneDrive\Documents\Claude\Projects\MLB HR Bets\run_daily.bat"
```
Logs land in `logs/daily_YYYY-MM-DD.log`, `logs/outcomes_YYYY-MM-DD.log`, `logs/nightly_YYYY-MM-DD.log`.

### If you want to switch back to GitHub auto-deploy later

1. Go to `https://app.netlify.com/projects/mlb-hr-bets` → Site configuration → Build & deploy
2. Link to `pablokunkel/mlb_bets_project`, set publish directory to `mlb_hr_bet_site`
3. Replace the `netlify deploy` line in `run_daily.bat` and `run_outcomes.bat` with `git add mlb_hr_bet_site/data && git commit && git push`
