# Archived 2026-05-02

This directory was an abandoned skeleton from the early days of the
Cloudflare Workers Static Assets migration. The previous title was
"HR Parlay Tracker" — predates the rebrand to "DingersOnly.cc".

## DO NOT USE THIS DIRECTORY

The CANONICAL site directory is **`mlb_hr_bet_site/`** at the repo root.
That's what the `dingersonlybot` Worker (configured in the Cloudflare
dashboard) serves at https://dingersonly.cc.

If you want to make changes to the dashboard:
- HTML / JS / CSS → `mlb_hr_bet_site/index.html`
- Data → `mlb_hr_bet_site/data/*.json` (regenerated nightly by `export_site_data.py`)

## Why this is archived rather than deleted

This directory confused multiple sessions on 2026-05-01. Archiving with an
unmistakable `_DO_NOT_USE` suffix makes it impossible to mistake for the
canonical dir again. Once we're confident no historical reference points
back at `site/`, this archive can be deleted entirely.

## What was in here

- `index.html` (31 KB) — abandoned dashboard skeleton with old branding
- `data/factor_trends.json`, `data/performance.json`, `data/picks_history.json`, `data/picks_latest.json` — stale JSON snapshots from the abandoned site
