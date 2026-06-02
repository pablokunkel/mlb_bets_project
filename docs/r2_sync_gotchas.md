# R2 sync & DB-path gotchas

The `hr_bets.db` SQLite file is the model's whole memory, and **R2 is the
source of truth** for it (since the 2026-05-12 cutover to GitHub Actions).
Two classes of mistake have repeatedly corrupted or stranded that file:
resolving the DB to the *wrong path*, and masking a *failed R2 sync*. Both
are silent until a day's picks come out wrong. This doc is the standing
reference so they don't happen a fourth time.

If you only read one thing: **run DB-writing scripts from the main checkout,
or set `HR_BETS_DB`. Never trust a pull/push that you piped through another
command.**

---

## 1. Where the DB actually lives

Canonical path (a **sibling of the repo**, one level *above* the project
root — NOT inside it):

```
C:\dev\Claude\Projects\data\hr_bets.db          <- canonical (local)
C:\dev\Claude\Projects\MLB HR Bets\             <- the repo / project root
```

Everything resolves from a single anchor in [`etl/db.py`](../etl/db.py):

| name            | resolves to                    | worktree-independent? |
|-----------------|--------------------------------|-----------------------|
| `DB_PATH`       | `<root>/data/hr_bets.db`       | yes, via `HR_BETS_DB` |
| `DATA_DIR`      | `<root>/data`                  | yes                   |
| `CACHE_DIR`     | `<root>/data/cache`            | yes                   |
| `RESULTS_DIR`   | `<root>/results`               | yes                   |
| `SITE_DATA_DIR` | `<repo>/mlb_hr_bet_site/data`  | **no — repo-relative**|

`SITE_DATA_DIR` is the deliberate exception: the dashboard's exported JSON is
committed to git and deployed by Cloudflare, so it must land in the *checkout*
you're working in (so it gets committed), never in the canonical sibling dir.
Every other data path is canonical and shared.

**Rule:** never re-derive any of these with ad-hoc `Path(__file__).parent…`
math. Import them from `etl.db`. A `tests/smoke.py` guard
(`pin_no_stray_db_and_canonical_anchor`) HALTs if a script regresses to an
in-repo DB.

---

## 2. The worktree trap, and the `HR_BETS_DB` fix

The relative fallback in `etl/db.py` is
`Path(__file__).parent.parent.parent / "data" / "hr_bets.db"`. That is
correct from the **main checkout** and on **GitHub Actions** (it lands at
`<project_parent>/data/hr_bets.db`). But from a **git worktree** under
`.claude/worktrees/<name>/`, the same `.parent` math lands at:

```
…\MLB HR Bets\.claude\worktrees\data\hr_bets.db     <- a STRAY, not canonical
```

so a script run from a worktree reads/writes a *different* file than the one
the pipeline (and R2) uses. Writes silently diverge; you debug a number that
the real DB never had.

**Fix — set the env var once per machine (PowerShell):**

```powershell
setx HR_BETS_DB "C:\dev\Claude\Projects\data\hr_bets.db"
```

With `HR_BETS_DB` set, `DB_PATH` / `DATA_DIR` / `CACHE_DIR` / `RESULTS_DIR`
are worktree-independent and always point at canonical — from any cwd.

Belt-and-suspenders (B24): `get_db()` with no explicit path now **fails loud**
(`FileNotFoundError`) when the canonical DB is missing, instead of silently
`mkdir`-ing an empty stray that the pipeline then writes picks into. If you
see *"canonical DB not found at …; set HR_BETS_DB or run from the main
checkout"* — that's this guard doing its job. Set the env var or run from
`C:\dev\Claude\Projects\MLB HR Bets`.

GitHub Actions does **not** set `HR_BETS_DB`: it checks out `main` (not a
worktree) and `r2_sync.py pull` writes to `DB_PATH`, so the relative fallback
resolves canonically there. Do not "fix" the fallback in a way that breaks
the Linux/main-checkout path.

---

## 3. The three-copies incident (what this all prevents)

By 2026-06-01 there were **three** diverging `hr_bets.db` files on the dev
machine, because different scripts used different `.parent`-count math and
silent `mkdir(parents=True)` *created* the wrong-location dirs instead of
erroring:

1. `…\Projects\data\hr_bets.db` — **canonical** (live, R2-backed).
2. `…\.claude\worktrees\data\hr_bets.db` — worktree stray. Deleted by **B24**.
3. `…\MLB HR Bets\data\hr_bets.db` — in-repo stray (root scripts hit it from
   2-parent math; diagnostics from 2/3/5/6-parent math). Deleted by **B26**.

The visible symptom: B16's `slate_pct` backfill "ran fine" for days but the
values never appeared in production picks — it had been writing to a stray the
noon job never read. B24 anchored the *write* path + added fail-loud; B26
propagated the anchor to **every** remaining reader/cache/results path and
added the smoke guard so a new stray can't accumulate unnoticed.

There must be **exactly one** `hr_bets.db` on disk locally (the canonical
one). If `Get-ChildItem C:\dev\Claude\Projects -Recurse -Filter hr_bets.db`
shows more than one, a stray has come back — find the script that created it
(it bypassed the anchor) before doing anything else.

---

## 4. R2 pull/push: never mask the exit code

R2 is the source of truth. Any **local** run that writes the DB without
bookending it with an R2 pull/push will be silently overwritten by the next
scheduled GitHub Actions job (daily-picks, outcomes-refresh, nightly-refresh):

```powershell
python infra/r2_sync.py pull        # local DB := R2 source of truth
python -m etl.backfill_2025 <args>  # write to the local DB
python infra/r2_sync.py push        # push back so the next job sees it
```

**The exit-code trap (2026-05-25, WEIGHT_REFIT_LOG.md).** A backfill lost
~98 dates of 7/1–9/30 because the R2 **push** was written as part of a piped /
`&&` chain — `… | tail` — and `tail`'s exit code (0) masked the push's real
failure. The push had *not* succeeded; a later `pull` then overwrote the
locally-ahead DB with the stale R2 copy, and the work was gone.

Lessons committed (do all three):

- **Never pipe a command whose exit code matters.** `r2_sync.py pull`/`push`
  must run on their own line; check `$LASTEXITCODE` (PowerShell) / `$?` and
  stop on failure. A pipe (`| tail`, `| Select-Object`, `&& …`) reports the
  *last* stage's status, not the sync's.
- **Inventory R2 explicitly before any pull that could overwrite
  locally-ahead state.** If your local DB has writes not yet pushed, a pull is
  destructive. Confirm what's in R2 first.
- **Prefer the GitHub Actions workflows for DB-writing jobs.** They pull at
  job start and push at the end, gated on the pipeline steps succeeding
  (`if: success()`), so a failed run never uploads a half-written DB.

---

## Quick reference

| symptom | cause | fix |
|---|---|---|
| `FileNotFoundError: canonical DB not found at …` | worktree without `HR_BETS_DB`, or no R2 pull | `setx HR_BETS_DB …` or run from main checkout; `r2_sync.py pull` |
| edits to the DB "don't take" / wrong numbers in picks | wrote to a stray copy | confirm exactly one `hr_bets.db` on disk; import paths from `etl.db` |
| smoke HALT: *stray in-repo hr_bets.db present* | a script created an in-repo DB (bypassed the anchor) | delete the stray; route the offending script through `etl.db` |
| backfill silently vanished | local run without R2 bookends, or a masked push failure | pull → write → push, each on its own line; never `| tail` a sync |
| local DB overwritten by a scheduled job | wrote locally without pushing to R2 | R2 is source of truth — push, or use the GH Actions workflow |

See also: [`etl/db.py`](../etl/db.py) (the anchor + `get_db` fail-loud),
[`infra/r2_sync.py`](../infra/r2_sync.py) (pull/push), and `BACKLOG.md`
B24 / B26 for the full history.
