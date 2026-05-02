# Archived 2026-05-02

These docs were the source of truth at various points but are now superseded.
**Do NOT use them for current architecture/deploy guidance** — see `DEPLOY.md`
and `ARCHITECTURE.md` at the repo root once those land.

## Contents

| File | What it was | Why it's archived |
|---|---|---|
| `DEPLOYMENT.md` | Described the Netlify CLI deploy pipeline (site ID `0fade6bd-...`, three-task flow). | Netlify team was killed 2026-05-01. Deploy is now `git push origin main` → Cloudflare Worker `dingersonlybot` auto-deploys. See `DEPLOY.md`. |
| `HANDOFF_NETLIFY_PIPELINE.md` | Mid-flight handoff from 2026-04-29 about wiring the DB-backed daily flow. | Pipeline wiring is shipped. Netlify-specific guidance is wrong. The "Weight refit log" section inside has been preserved as `WEIGHT_REFIT_LOG.md` at the repo root. |
| `AUDIT_REPORT.md` | 2026-04-30 audit that concluded "PIPELINE FULLY FUNCTIONAL — No code changes required." | Was correct on 4/30. Then 5/1 found four real bugs (within-tier normalization mangling barrels, `score_power` averaging zero, missing `season_batting` fallback, `etl_outcomes` `UnboundLocalError`) and 5/2 found two more (lineup data corruption + 5 of 9 SEA starters missing). The "fully functional" verdict no longer holds. Snapshot value only. |
| `AUDIT_REPORT_V2.md` | 2026-04-30 strict reverse-mapping audit. Concluded "PRODUCTION-READY." | Same as V1 — overtaken by the bugs found in the next 48 hours. |

## Where to look instead

- `DEPLOY.md` (repo root) — current deploy/release process for `dingersonly.cc` and `api.dingersonly.cc`.
- `ARCHITECTURE.md` (repo root) — component map and data flow.
- `How_The_HR_Model_Works.md` (repo root) — model architecture and scoring methodology.
- `WEIGHT_REFIT_LOG.md` (repo root) — history of monthly weight refit decisions (extracted from `HANDOFF_NETLIFY_PIPELINE.md`).
- `diagnostics/README.md` — investigation tooling.

## Also deleted in this cleanup

`handoff.md` — March 2026 doc describing pre-Netlify architecture with a `scripts/` subdir, Mac/Linux setup, crontab, and "Priority 1 bugs to fix before going live." The file structure was wrong (no `scripts/` dir), the bugs were fixed long ago, and the Mac instructions never applied. Fully obsolete — not worth archiving.
