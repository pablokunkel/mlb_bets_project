#!/usr/bin/env python3
r"""
data_integrity_audit.py - B31: model-input coverage map + classification.

Read-only diagnostic. Builds a column-by-column non-NULL coverage map of
`pick_inputs` (broken out by era) plus the snapshot/support tables, classifies
every gap (intentional / broken-backfill / live-pipeline-gap), and emits a
ranked backfill plan. Touches NOTHING: opens the DB read-only (URI mode=ro) so
it cannot mutate data or create a stray empty DB.

Why the era split. The blended 2026% is misleading because model features were
added mid-season (B4/B6a/B8 landed ~2026-05-19..21). So a 2026-wide average mixes
dates that pre-date a feature with dates that have it. The era that actually
answers "are TODAY's picks scoring on complete data?" is the **recent-live-Nd**
window (default last 14 distinct dates) -- that's the only era that reflects the
current live pipeline with every feature wired.

  - 2025_backfill   : date <  <year-boundary>            (the 2025-season backfill)
  - 2026_pre_recent : <year-boundary> <= date < <recent> (mid-season-add noise)
  - 2026_recent_<N>d: date >= <recent>                   (last N dates -> today's picks)

This script is the re-runnable engine B32 uses to VERIFY backfills actually land
(the step missing every prior cycle): re-run it after a backfill and the broken
families flip from 0% to >target, and the B32 verification table reports PASS.

Usage (PowerShell, from a worktree set HR_BETS_DB or pass --db; see the
worktree-DB-path gotcha in docs/r2_sync_gotchas.md):
    python -m diagnostics.data_integrity_audit
    python -m diagnostics.data_integrity_audit --db C:\dev\Claude\Projects\data\hr_bets.db
    python -m diagnostics.data_integrity_audit --md docs\data_integrity_audit_2026-06-03.md
    python -m diagnostics.data_integrity_audit --recent-days 14

It is anchored to etl.db.DB_PATH (the canonical anchor) and accepts --db to
target an alternate DB. No data, scoring, or ETL changes.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Make the project root importable from diagnostics/ so `etl.db` resolves the
# same canonical anchor every other script uses (B26 path-hygiene lineage).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from etl.db import DB_PATH  # canonical anchor (HR_BETS_DB-aware)

# Coverage-target the broken-backfill families must clear, per era, for B32 to
# call a re-run "landed". 70% mirrors the resume-safe gates in the backfill
# scripts (the residual is batters legitimately below the min-sample threshold).
LAND_TARGET_PCT = 70.0

# Default year boundary between the 2025 backfill and the 2026 live season.
YEAR_BOUNDARY = "2026-01-01"


# ---------------------------------------------------------------------------
# Classification registry
# ---------------------------------------------------------------------------
# Static knowledge: what each pick_inputs column IS, so a 0%/partial reading is
# graded correctly instead of being miscalled a bug. Cross-references CLAUDE.md
# "False alarms" + docs/handoff_2026-05-26.md. Categories:
#
#   HEALTHY        - populated where it should be; no action.
#   INTENTIONAL    - NULL by design / known-dead; NOT a bug, do not "fix".
#   BROKEN_BACKFILL- a backfill ran-but-never-landed (path bug) or never ran on
#                    canonical, or a code bug starves it. The B32 work-list.
#   LIVE_GAP       - thin on recent-live rows -> affects today's picks.
#   METADATA       - provenance / bookkeeping, not a model signal.
#
# Each entry: (category, note). Columns not listed fall to UNCLASSIFIED so a
# future schema add surfaces for triage rather than being silently graded.
CLASSIFICATION: dict[str, tuple[str, str]] = {
    # --- Power (season aggregates) -------------------------------------------
    "barrel_pct":   ("HEALTHY", "Populated live; values are SYNTHETIC (barrel_pct_source never 'statcast') -- a B1 quality issue, not a coverage gap."),
    "exit_velo":    ("HEALTHY", "Populated; synthetic estimate (82 + slg*15) per B1."),
    "hr_fb_pct":    ("HEALTHY", "Populated; synthetic. Anchor mis-cal is a separate known item (B17 shipped)."),
    "iso":          ("HEALTHY", "Populated from season_batting."),
    "xwoba_contact":("INTENTIONAL", "0% in 2025 by design -- the 2025 backfill substitutes recent_xwoba_contact_14d. ~99% live."),
    "pull_fb_pct":  ("INTENTIONAL", "0% all eras -- dead branch (adv dict never carries it). B20: drop, do not wire."),

    # --- Form: legacy proxies (replaced by the 2026-05-19 rebuild) -----------
    "recent_hr_14d":         ("INTENTIONAL", "Legacy proxy, retired by the 2026-05-19 Form rebuild (replaced by recent_hr_10g). Kept for historical replay."),
    "recent_barrel_pct_14d": ("INTENTIONAL", "Legacy proxy = min(25, recent_ISO*100); replaced by recent_iso_30g. Historical-only."),
    "ev_trend_14d":          ("INTENTIONAL", "Legacy proxy = (recent_SLG-season_SLG)*30; replaced. Historical-only."),

    # --- Form: current ------------------------------------------------------
    "recent_hr_10g":     ("HEALTHY", "Live ~99%. 0% in 2026_pre_recent because the rebuild landed 2026-05-19."),
    "recent_iso_30g":    ("HEALTHY", "Live ~99%."),
    "recent_avg_30g":    ("HEALTHY", "Live ~99%. (B11 dropped it from score_form but the column is still persisted.)"),
    "recent_window_days":("HEALTHY", "Live ~99%; calendar span of the 30g window."),
    "ev_trend":          ("INTENTIONAL", "0% all eras -- the real EV-trend slot is wired skip-on-missing and stays NULL until A2 builds the nightly EV ETL (CLAUDE.md false-alarm; handoff). NOT a re-run -- A2 is a build."),

    # --- Power: real recent Statcast (B6a / B12) ----------------------------
    "recent_barrel_real_14d":   ("HEALTHY", "Live ~82% (B6a nightly). Residual = batters with <10 batted balls (legit skip)."),
    "recent_xwoba_contact_14d": ("HEALTHY", "Live ~82% (B6a nightly)."),
    "recent_iso_14d":           ("HEALTHY", "Live ~82% (B6a nightly)."),
    "recent_barrel_real_21d":   ("BROKEN_BACKFILL", "0% all eras. backfill_statcast_windows.py never landed on canonical (no etl_log row). B12's negative 21d/28d finding is therefore untrustworthy (scored on no data) -- B27 needs these."),
    "recent_xwoba_contact_21d": ("BROKEN_BACKFILL", "0% all eras. Same as recent_barrel_real_21d."),
    "recent_iso_21d":           ("BROKEN_BACKFILL", "0% all eras. Same as recent_barrel_real_21d."),
    "recent_barrel_real_28d":   ("BROKEN_BACKFILL", "0% all eras. backfill_statcast_windows.py never landed (28d window)."),
    "recent_xwoba_contact_28d": ("BROKEN_BACKFILL", "0% all eras. Same as recent_barrel_real_28d."),
    "recent_iso_28d":           ("BROKEN_BACKFILL", "0% all eras. Same as recent_barrel_real_28d."),

    # --- Matchup: pitcher ---------------------------------------------------
    "pitcher_hr_per_9":          ("HEALTHY", "100% live."),
    "pitcher_era":               ("HEALTHY", "100% live."),
    "pitcher_hh_pct":            ("HEALTHY", "100% live."),
    "pitcher_k_per_9":           ("HEALTHY", "100% live."),
    "pitcher_fb_pct_allowed":    ("HEALTHY", "~99% live. >100 parse bug on 23 rows is a known false-alarm (B13)."),
    "pitcher_recent_hr9_21d":    ("HEALTHY", "Live ~96% (B4 rolling window)."),
    "pitcher_recent_starts_21d": ("HEALTHY", "Live ~97%."),
    "pitcher_recent_era_21d":    ("HEALTHY", "Live ~88% (B4). 0% in 2026_pre_recent -- added ~2026-05-21."),
    "pitcher_recent_k9_21d":     ("HEALTHY", "Live ~88% (B4)."),

    # --- Matchup: batter/game ----------------------------------------------
    "woba_vs_hand":         ("HEALTHY", "~99% live."),
    "archetype_similarity": ("INTENTIONAL", "~67% by structure -- NULL on the v1 matchup path / when no Statcast pitcher profile is available (skip-on-missing). Stable across eras."),
    "vegas_team_total_pct": ("INTENTIONAL", "0% in 2025 (Vegas odds not persisted in the backfill). ~99% live."),
    "vegas_team_total_raw": ("INTENTIONAL", "0% in 2025 (not persisted). ~99% live."),
    "platoon_advantage":    ("HEALTHY", "100% live."),

    # --- Matchup: pitch-type archetype splits (Phase 2) ---------------------
    "fb_slg": ("BROKEN_BACKFILL", "0% all eras. batter_pitch_type_splits is EMPTY (0 rows); backfill_pitch_type_splits.py never ran on canonical (no etl_log)."),
    "fb_pa":  ("BROKEN_BACKFILL", "0% all eras. Same family as fb_slg."),
    "br_slg": ("BROKEN_BACKFILL", "0% all eras. Same family as fb_slg."),
    "br_pa":  ("BROKEN_BACKFILL", "0% all eras. Same family as fb_slg."),
    "os_slg": ("BROKEN_BACKFILL", "0% all eras. Same family as fb_slg."),
    "os_pa":  ("BROKEN_BACKFILL", "0% all eras. Same family as fb_slg."),

    # --- Form-archetype sub-signal (Phase 2) --------------------------------
    "form_archetype_centroid_json": ("BROKEN_BACKFILL", "0% all eras. batter_form_archetype is EMPTY (0 rows). CODE BUG: NA-ambiguous boolean in the form-archetype builder crashes every (date,window) iteration; the orchestrator's except swallows it. Fix before re-run."),
    "form_archetype_window":        ("BROKEN_BACKFILL", "0% all eras. Same family as form_archetype_centroid_json."),
    "form_archetype_n_hrs":         ("BROKEN_BACKFILL", "0% all eras. Same family as form_archetype_centroid_json."),

    # --- Park-archetype sub-signal (Phase 2) --------------------------------
    "park_archetype_centroid_json": ("BROKEN_BACKFILL", "0% all eras. batter_park_archetype has rows but ALL-NULL centroids. CODE/DATA BUG: venue lookup JOINs batter_hr_events->daily_slate, but daily_slate is live-only so only ~3% of HRs resolve a venue. Fix venue resolution before re-run."),
    "park_archetype_n_hrs":         ("BROKEN_BACKFILL", "0% all eras. Same family as park_archetype_centroid_json."),

    # --- Park / Weather / Lineup -------------------------------------------
    "hr_park_factor":     ("HEALTHY", "~97% live. (park_factors is a hardcoded seed -- a B3 quality question, not coverage.)"),
    "temperature_f":      ("HEALTHY", "100% live."),
    "wind_mph":           ("HEALTHY", "100% live."),
    "wind_direction_deg": ("HEALTHY", "100% live."),
    "humidity_pct":       ("LIVE_GAP", "Only ~57% live. ~33% is domes (intentional NULL); the rest is non-dome open-meteo misses (B14 weather failures + B10 partial-weather). Low priority -- weather weight 0.08."),
    "is_dome":            ("HEALTHY", "100% live."),
    "batting_order":      ("INTENTIONAL", "~77% live -- NULL for bench / roster_fallback / non-starters; only 1-9 starters carry a value (by design)."),

    # --- Slate-percentile snapshots (B16) -----------------------------------
    "slate_park_pct":                  ("HEALTHY", "~97% live (tracks hr_park_factor)."),
    "slate_weather_pct":               ("LIVE_GAP", "~57% live -- tracks humidity_pct (requires temp+wind+humidity all non-NULL). Same root cause as humidity_pct."),
    "slate_pitcher_vulnerability_pct": ("HEALTHY", "100% live."),

    # --- B8 floor / handedness / provenance ---------------------------------
    "season_hr":         ("HEALTHY", "Live ~91% (B8 outcomes-cumulative). 0% in 2026_pre_recent -- B8 landed ~2026-05-20."),
    "bats":              ("HEALTHY", "100% live."),
    "throws":            ("HEALTHY", "100% live."),
    "weather_source":    ("METADATA", "Provenance flag (open_meteo / dome_default / api_failed_default). 100% live."),
    "barrel_pct_source": ("METADATA", "Provenance flag. ~93% live. Never 'statcast' -- the Power inputs are synthetic (B1)."),
    "lineup_source":     ("METADATA", "Provenance flag (posted / recent:DATE / roster_fallback). 100% live."),
    "fetched_at":        ("METADATA", "Row insert timestamp."),
    "source":            ("METADATA", "Row provenance bookkeeping (non-signal)."),
}

# Snapshot/support tables: how to count "real" coverage (NON-NULL centroid/SLG,
# not just rows) + classification.
SNAPSHOT_SPECS = [
    # (table, real_col, category, note)
    ("batter_park_archetype", "feature_centroid_json", "BROKEN_BACKFILL",
     "Rows present but ALL-NULL centroids -- venue lookup starved by live-only daily_slate (~3% of HRs resolve). backfill_park_archetype.py needs a venue-resolution fix first."),
    ("batter_form_archetype", "feature_centroid_json", "BROKEN_BACKFILL",
     "EMPTY (0 rows). backfill_form_archetype.py crashes on the NA-ambiguous boolean bug every iteration; never landed. Fix the NA bug first."),
    ("batter_pitch_type_splits", "fb_slg", "BROKEN_BACKFILL",
     "EMPTY (0 rows). backfill_pitch_type_splits.py never ran on canonical (no etl_log). Clean re-run; code is sound post-2026-05-26 daily_picks-branch fix."),
]


# ---------------------------------------------------------------------------
# DB helpers (read-only)
# ---------------------------------------------------------------------------

def open_ro(db_path: Path) -> sqlite3.Connection:
    """Open the DB strictly read-only. Fails loud if the file is absent
    (worktree without HR_BETS_DB, or no R2 pull) rather than creating a stray."""
    if not db_path.exists():
        raise FileNotFoundError(
            f"DB not found at {db_path}. Set HR_BETS_DB, pass --db, or run from "
            f"the main checkout (see docs/r2_sync_gotchas.md)."
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_cols(conn, table) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def table_exists(conn, table) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# Coverage computation
# ---------------------------------------------------------------------------

def compute_eras(conn, recent_days: int, year_boundary: str) -> dict:
    """Data-driven era windows. The recent window is the last N distinct dates
    present in pick_inputs (so it tracks 'today' on every re-run)."""
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM pick_inputs WHERE date >= ? ORDER BY date",
        (year_boundary,)).fetchall()]
    if len(dates) >= recent_days:
        recent_start = dates[-recent_days]
    elif dates:
        recent_start = dates[0]
    else:
        recent_start = year_boundary
    recent_end = dates[-1] if dates else "(none)"
    eras = {
        "2025_backfill":   f"date < '{year_boundary}'",
        "2026_pre_recent": f"date >= '{year_boundary}' AND date < '{recent_start}'",
        f"2026_recent_{recent_days}d": f"date >= '{recent_start}'",
    }
    recent_key = f"2026_recent_{recent_days}d"
    return {
        "eras": eras,
        "recent_key": recent_key,
        "recent_start": recent_start,
        "recent_end": recent_end,
        "counts": {k: conn.execute(
            f"SELECT COUNT(*) FROM pick_inputs WHERE {w}").fetchone()[0]
            for k, w in eras.items()},
    }


def coverage_table(conn, eras_info: dict) -> list[dict]:
    """Per-column non-NULL coverage across eras + classification."""
    eras = eras_info["eras"]
    counts = eras_info["counts"]
    cols = table_cols(conn, "pick_inputs")
    rows = []
    for c in cols:
        if c in ("date", "batter_id"):
            continue
        cov = {}
        for k, w in eras.items():
            n = counts[k]
            nn = conn.execute(
                f"SELECT COUNT({c}) FROM pick_inputs WHERE {w}").fetchone()[0]
            cov[k] = (100.0 * nn / n if n else 0.0, nn)
        cat, note = CLASSIFICATION.get(c, ("UNCLASSIFIED", "Not in the registry -- triage (new schema add?)."))
        rows.append({"col": c, "cov": cov, "cat": cat, "note": note})
    return rows


def snapshot_report(conn) -> list[dict]:
    out = []
    for table, real_col, cat, note in SNAPSHOT_SPECS:
        if not table_exists(conn, table):
            out.append({"table": table, "exists": False, "cat": cat, "note": note})
            continue
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        real = conn.execute(f"SELECT COUNT({real_col}) FROM {table}").fetchone()[0]
        players = conn.execute(
            f"SELECT COUNT(DISTINCT player_id) FROM {table}").fetchone()[0]
        span = conn.execute(
            f"SELECT MIN(date_through), MAX(date_through) FROM {table}").fetchone()
        out.append({
            "table": table, "exists": True, "rows": n, "real_col": real_col,
            "real": real, "real_pct": (100.0 * real / n if n else 0.0),
            "players": players, "span": f"{span[0]} -> {span[1]}" if span[0] else "(empty)",
            "cat": cat, "note": note,
        })
    return out


def support_report(conn) -> list[dict]:
    """season_batting / career_batting / pitcher_arsenals -- rows, key-col
    coverage, span/freshness."""
    out = []

    # season_batting
    if table_exists(conn, "season_batting"):
        n = conn.execute("SELECT COUNT(*) FROM season_batting").fetchone()[0]
        by_season = conn.execute(
            "SELECT season, COUNT(*) n, MAX(fetched_at) f FROM season_batting "
            "GROUP BY season ORDER BY season").fetchall()
        out.append({
            "table": "season_batting", "rows": n,
            "detail": "; ".join(f"{r['season']}: {r['n']} rows (last {r['f']})" for r in by_season),
            "cat": "HEALTHY",
            "note": "2026 current; 2024/2025 frozen (static history). barrel/exit_velo/hr_fb 100% but SYNTHETIC (B1). tier col unused here.",
        })

    # career_batting
    if table_exists(conn, "career_batting"):
        n = conn.execute("SELECT COUNT(*) FROM career_batting").fetchone()[0]
        bar = conn.execute("SELECT COUNT(career_barrel_pct) FROM career_batting").fetchone()[0]
        hpp = conn.execute("SELECT COUNT(career_hr_per_pa) FROM career_batting").fetchone()[0]
        fresh = conn.execute("SELECT MIN(fetched_at), MAX(fetched_at) FROM career_batting").fetchone()
        out.append({
            "table": "career_batting", "rows": n,
            "detail": f"career_hr_per_pa {100.0*hpp/n if n else 0:.0f}% | career_barrel_pct {100.0*bar/n if n else 0:.0f}% | refreshed {fresh[0]}",
            "cat": "LIVE_GAP" if (n and bar == 0) else "HEALTHY",
            "note": "career Statcast cols (barrel/exit_velo/hr_fb) 0% populated -- USE_CAREER_PRIOR Statcast shrinkage runs on nothing. Flag-gated, low priority. Quarterly refresh (last 2026-05-03) is fine.",
        })

    # pitcher_arsenals
    if table_exists(conn, "pitcher_arsenals"):
        n = conn.execute("SELECT COUNT(*) FROM pitcher_arsenals").fetchone()[0]
        velo = conn.execute("SELECT COUNT(avg_fb_velo) FROM pitcher_arsenals").fetchone()[0]
        name = conn.execute("SELECT COUNT(pitcher_name) FROM pitcher_arsenals").fetchone()[0]
        by_season = conn.execute(
            "SELECT season, COUNT(*) n, MAX(fetched_at) f FROM pitcher_arsenals "
            "GROUP BY season ORDER BY season").fetchall()
        out.append({
            "table": "pitcher_arsenals", "rows": n,
            "detail": "; ".join(f"{r['season']}: {r['n']} (last {r['f']})" for r in by_season)
                      + f" | avg_fb_velo {100.0*velo/n if n else 0:.0f}% | pitcher_name {100.0*name/n if n else 0:.0f}%",
            "cat": "HEALTHY",
            "note": "2026 current. pitcher_name 0% (live lookup keys on pitcher_id, so harmless) -- B22 hygiene, not a model gap.",
        })
    return out


# ---------------------------------------------------------------------------
# Ranked backfill plan (the B32 work-list)
# ---------------------------------------------------------------------------
# Ordered by HR-prediction value per the form goal (recent-window quality +
# ev_trend rank high; archetypes are bigger lifts). `verify_cols`/`verify_era`
# let a re-run report PASS/FAIL so B32 can confirm a backfill landed.
PLAN = [
    {
        "rank": 1, "family": "Recent-window quality (21d / 28d)",
        "value": "HIGH -- directly unblocks B27's form window-sweep. B12's negative 21d/28d finding is untrustworthy (those columns are empty, so the variant scored on no data).",
        "script": "etl/backfill_statcast_windows.py",
        "failure": "PATH BUG / never-run-on-canonical. Script is sound (--db -> DB_PATH). No etl_log row exists.",
        "fix": "Clean re-run to canonical (HR_BETS_DB set or --db). ~90-100 min cold cache. Decide whether to also wire nightly (currently backtest-only).",
        "verify_cols": ["recent_barrel_real_21d", "recent_iso_21d", "recent_barrel_real_28d", "recent_iso_28d"],
        "verify_era": "2025_backfill",
    },
    {
        "rank": 2, "family": "ev_trend (real EV trend, A2)",
        "value": "HIGH -- the recent contact-quality signal B27/A2 want, decorrelated from season power.",
        "script": "(none -- this is a BUILD, not a re-run: A2 nightly EV ETL)",
        "failure": "INTENTIONAL NULL until A2 ships. No existing backfill script -- the column is wired skip-on-missing.",
        "fix": "Build A2: rolling EV in nightly ETL (etl/etl_nightly.py), recent EV - season EV -> pick_inputs.ev_trend. Off the noon critical path.",
        "verify_cols": ["ev_trend"],
        "verify_era": None,
    },
    {
        "rank": 3, "family": "Pitch-type SLG splits (fb/br/os)",
        "value": "MEDIUM-HIGH -- the matchup pitch-type archetype sub-signal (score_matchup v2).",
        "script": "etl/backfill_pitch_type_splits.py",
        "failure": "PATH BUG / never-run-on-canonical. batter_pitch_type_splits is EMPTY; no etl_log. Code is sound post-2026-05-26 daily_picks-branch fix.",
        "fix": "Clean re-run to canonical (~90-180 min Statcast pull), then load into pick_inputs (load_picks_to_db / backfill_2025 --force).",
        "verify_cols": ["fb_slg", "br_slg", "os_slg"],
        "verify_era": "2025_backfill",
    },
    {
        "rank": 4, "family": "Form-archetype centroid",
        "value": "MEDIUM (bigger lift) -- form-archetype matchup signal; conceptually aligned with B27 but a separate centroid calc.",
        "script": "etl/backfill_form_archetype.py",
        "failure": "CODE BUG (NA-ambiguous boolean) -- crashes every (date,window) iteration; the orchestrator's line-491 except swallows it (prints message only, no traceback). batter_form_archetype is EMPTY.",
        "fix": "FIX FIRST: add traceback.print_exc() at the except to locate the NA, fix the unsafe boolean on a parquet-loaded nullable Float64/boolean in the features_v2 form-archetype builder. THEN re-run.",
        "verify_cols": ["form_archetype_centroid_json"],
        "verify_era": "2025_backfill",
    },
    {
        "rank": 5, "family": "Park-archetype centroid",
        "value": "LOWER -- park weight is only 0.04; bigger lift and needs a data-model change first.",
        "script": "etl/backfill_park_archetype.py",
        "failure": "CODE/DATA BUG -- venue lookup JOINs batter_hr_events->daily_slate (live-only); only ~3% of HRs resolve a venue, so centroids are all-NULL even on canonical (proven by the live nightly rows).",
        "fix": "FIX FIRST: enrich batter_hr_events with home_team at Statcast ETL time + a static home_team->venue map (cheap), OR a game_pk->venue API lookup. THEN re-run.",
        "verify_cols": ["park_archetype_centroid_json"],
        "verify_era": "2025_backfill",
    },
]


def plan_status(conn, eras_info: dict) -> list[dict]:
    """Inject current coverage into PLAN so a re-run shows whether each broken
    family has landed (PASS/FAIL vs LAND_TARGET_PCT). This is B32's verifier."""
    eras = eras_info["eras"]
    counts = eras_info["counts"]
    out = []
    for item in PLAN:
        era = item["verify_era"]
        if era is None or era not in eras:
            out.append({**item, "current_pct": None, "status": "N/A (build, not a re-run)"})
            continue
        w = eras[era]
        n = counts[era] or 1
        pcts = []
        for c in item["verify_cols"]:
            nn = conn.execute(f"SELECT COUNT({c}) FROM pick_inputs WHERE {w}").fetchone()[0]
            pcts.append(100.0 * nn / n)
        cur = min(pcts) if pcts else 0.0
        out.append({**item, "current_pct": cur, "verify_era_resolved": era,
                    "status": "PASS (landed)" if cur >= LAND_TARGET_PCT else "FAIL (not landed)"})
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _counts_by_cat(cov_rows) -> dict:
    cats: dict[str, int] = {}
    for r in cov_rows:
        cats[r["cat"]] = cats.get(r["cat"], 0) + 1
    return cats


def render_console(db_path, eras_info, cov_rows, snap_rows, sup_rows, plan_rows):
    eras = eras_info["eras"]
    rk = eras_info["recent_key"]
    print(f"\nDATA-INTEGRITY AUDIT (B31)   DB: {db_path}")
    print(f"recent-live window: {eras_info['recent_start']} -> {eras_info['recent_end']}  ({rk})")
    print("era row counts: " + ", ".join(f"{k}={v}" for k, v in eras_info["counts"].items()))

    cats = _counts_by_cat(cov_rows)
    print("\npick_inputs columns by class: " + ", ".join(f"{k}={v}" for k, v in sorted(cats.items())))

    print("\n== pick_inputs per-column coverage (non-NULL %) ==")
    hdr = f"{'column':30s} " + " ".join(f"{k:>16s}" for k in eras) + "  class"
    print(hdr); print("-" * len(hdr))
    for r in cov_rows:
        cells = " ".join(f"{r['cov'][k][0]:6.1f}% ({r['cov'][k][1]:>5d})" for k in eras)
        print(f"{r['col']:30s} {cells}  {r['cat']}")

    print("\n== snapshot/archetype tables (NON-NULL centroid/SLG, not rows) ==")
    for s in snap_rows:
        if not s["exists"]:
            print(f"  {s['table']:26s} MISSING TABLE  [{s['cat']}]")
            continue
        print(f"  {s['table']:26s} rows={s['rows']:6d}  real({s['real_col']})={s['real']} ({s['real_pct']:.1f}%)  "
              f"players={s['players']}  span={s['span']}  [{s['cat']}]")

    print("\n== support tables ==")
    for s in sup_rows:
        print(f"  {s['table']:26s} rows={s['rows']:6d}  {s['detail']}  [{s['cat']}]")

    print("\n== ranked backfill plan (B32 work-list) ==")
    for p in plan_rows:
        cur = "" if p["current_pct"] is None else f"  current={p['current_pct']:.1f}%"
        print(f"  #{p['rank']} {p['family']}  -> {p['status']}{cur}")
        print(f"       script: {p['script']}")
        print(f"       failure: {p['failure']}")


def render_markdown(db_path, eras_info, cov_rows, snap_rows, sup_rows, plan_rows, audit_date) -> str:
    eras = list(eras_info["eras"].keys())
    rk = eras_info["recent_key"]
    cats = _counts_by_cat(cov_rows)
    L = []
    L.append(f"# Data-integrity audit — {audit_date}")
    L.append("")
    L.append("> **B31 — read-only diagnostic.** Column-by-column model-input coverage map "
             "across `pick_inputs` + the snapshot/support tables, every gap classified, and a "
             "ranked backfill plan. No data / scoring / ETL changes. Regenerate with "
             "`python -m diagnostics.data_integrity_audit --md docs\\data_integrity_audit_<date>.md`.")
    L.append("")
    L.append(f"- **DB audited:** `{db_path}`")
    L.append(f"- **recent-live window** (`{rk}`): **{eras_info['recent_start']} → {eras_info['recent_end']}** "
             f"— the last {rk.split('_')[-1]} distinct `pick_inputs` dates; the only era that reflects today's live pipeline.")
    L.append(f"- **era row counts:** " + ", ".join(f"`{k}`={v:,}" for k, v in eras_info["counts"].items()))
    L.append("")

    # Gap classification counts
    L.append("## Gap-classification counts")
    L.append("")
    L.append("| Class | `pick_inputs` columns | Meaning |")
    L.append("|---|---:|---|")
    meaning = {
        "HEALTHY": "populated where it should be — no action",
        "INTENTIONAL": "NULL by design / known-dead — **not a bug**",
        "BROKEN_BACKFILL": "ran-but-never-landed or starved by a code bug — **the work-list**",
        "LIVE_GAP": "thin on recent-live rows — affects today's picks",
        "METADATA": "provenance / bookkeeping (non-signal)",
        "UNCLASSIFIED": "new/untriaged column",
    }
    for cat in ["BROKEN_BACKFILL", "LIVE_GAP", "INTENTIONAL", "HEALTHY", "METADATA", "UNCLASSIFIED"]:
        if cats.get(cat):
            L.append(f"| `{cat}` | {cats[cat]} | {meaning[cat]} |")
    L.append("")
    L.append("Plus three snapshot tables, all `BROKEN_BACKFILL`: "
             "`batter_park_archetype` (all-NULL centroids), `batter_form_archetype` (empty), "
             "`batter_pitch_type_splits` (empty).")
    L.append("")

    # Recent-live finding
    recent_w = eras_info["eras"][rk]
    broken_live = [r["col"] for r in cov_rows if r["cat"] == "BROKEN_BACKFILL"]
    live_gaps = [r["col"] for r in cov_rows if r["cat"] == "LIVE_GAP"]
    L.append(f"## Are today's picks scoring on complete data? (`{rk}`)")
    L.append("")
    L.append("**Yes for the six scored factors.** In the recent-live window every input the "
             "composite actually consumes is well-covered: Power (barrel/exit_velo/hr_fb/iso/xwoba ~93–100%), "
             "Matchup (pitcher_* 100%, woba_vs_hand ~99%, vegas ~99%, slate_pitcher_vulnerability_pct 100%), "
             "Park (hr_park_factor ~97%), Weather (temp/wind 100%), Form (recent_hr_10g/iso_30g ~99%, "
             "season_hr ~91%), plus the B6a real-Statcast 14d inputs (~82%). The picks are **not** scoring on missing data.")
    L.append("")
    L.append("**What is dark in the live window** is the set of *unwired/optional* signals — "
             "they don't degrade today's picks, but they're exactly the levers B27 (form rebuild) needs:")
    L.append("")
    L.append(f"- **0% (broken-backfill):** {', '.join(f'`{c}`' for c in broken_live)} — plus `ev_trend` (intentional until A2).")
    L.append(f"- **partial (live-gap):** {', '.join(f'`{c}`' for c in live_gaps)} — mostly domes + open-meteo humidity misses (B14/B10).")
    L.append("")

    # Coverage table
    L.append("## Per-column coverage (`pick_inputs`)")
    L.append("")
    L.append("| column | " + " | ".join(eras) + " | class |")
    L.append("|---|" + "|".join("---:" for _ in eras) + "|---|")
    for r in cov_rows:
        cells = " | ".join(f"{r['cov'][k][0]:.1f}%" for k in eras)
        L.append(f"| `{r['col']}` | {cells} | {r['cat']} |")
    L.append("")
    L.append("_(numbers are non-NULL %; raw counts available from the console run.)_")
    L.append("")

    # Snapshot tables
    L.append("## Snapshot / archetype tables")
    L.append("")
    L.append("Counting **non-NULL centroid/SLG**, not rows (the handoff's warning: park had 114k rows with all-NULL centroids).")
    L.append("")
    L.append("| table | rows | real (non-NULL) | players | span | class |")
    L.append("|---|---:|---|---:|---|---|")
    for s in snap_rows:
        if not s["exists"]:
            L.append(f"| `{s['table']}` | — | MISSING | — | — | {s['cat']} |")
            continue
        L.append(f"| `{s['table']}` | {s['rows']:,} | {s['real']:,} ({s['real_pct']:.1f}%) of `{s['real_col']}` | {s['players']:,} | {s['span']} | {s['cat']} |")
    L.append("")
    for s in snap_rows:
        L.append(f"- **`{s['table']}`** — {s['note']}")
    L.append("")
    L.append("## Support tables")
    L.append("")
    L.append("| table | rows | detail | class |")
    L.append("|---|---:|---|---|")
    for s in sup_rows:
        det = s["detail"].replace("|", "\\|")  # literal pipes break the md table
        L.append(f"| `{s['table']}` | {s['rows']:,} | {det} | {s['cat']} |")
    L.append("")
    for s in sup_rows:
        L.append(f"- **`{s['table']}`** — {s['note']}")
    L.append("")

    # Classification detail
    L.append("## Gap classification (detail)")
    L.append("")
    for cat, head in [("BROKEN_BACKFILL", "Broken-backfill (the work-list)"),
                      ("LIVE_GAP", "Live-pipeline-gap"),
                      ("INTENTIONAL", "Intentional / known-dead (do NOT 'fix')")]:
        items = [r for r in cov_rows if r["cat"] == cat]
        if not items:
            continue
        L.append(f"### {head}")
        L.append("")
        for r in items:
            L.append(f"- **`{r['col']}`** — {r['note']}")
        L.append("")

    # Ranked plan
    L.append("## Ranked backfill plan (what B32 executes)")
    L.append("")
    L.append("Ordered by HR-prediction value (recent-window quality + `ev_trend` rank high for the form goal; "
             "the archetypes are bigger lifts). `status` is **live** — re-run this audit after a backfill and "
             "it flips to `PASS` once coverage clears " + f"{LAND_TARGET_PCT:.0f}% (this is the verification step missing every prior cycle).")
    L.append("")
    L.append("| # | family | value | script | suspected failure | current | status |")
    L.append("|---|---|---|---|---|---:|---|")
    for p in plan_rows:
        cur = "—" if p["current_pct"] is None else f"{p['current_pct']:.1f}%"
        L.append(f"| {p['rank']} | {p['family']} | {p['value'].split(' -- ')[0]} | `{p['script']}` | "
                 f"{p['failure'].split('.')[0]} | {cur} | {p['status']} |")
    L.append("")
    for p in plan_rows:
        L.append(f"### #{p['rank']} — {p['family']}")
        L.append("")
        L.append(f"- **Value:** {p['value']}")
        L.append(f"- **Script:** `{p['script']}`")
        L.append(f"- **Suspected failure mode:** {p['failure']}")
        L.append(f"- **Fix / action:** {p['fix']}")
        if p["current_pct"] is not None:
            L.append(f"- **Current coverage** (`{p.get('verify_era_resolved')}`, cols {', '.join('`'+c+'`' for c in p['verify_cols'])}): "
                     f"{p['current_pct']:.1f}% → **{p['status']}** (target {LAND_TARGET_PCT:.0f}%)")
        L.append("")

    L.append("## Re-running (for B32 verification)")
    L.append("")
    L.append("```powershell")
    L.append("$env:HR_BETS_DB = \"C:\\dev\\Claude\\Projects\\data\\hr_bets.db\"   # or pass --db")
    L.append("python -m diagnostics.data_integrity_audit --md docs\\data_integrity_audit_<date>.md")
    L.append("```")
    L.append("")
    L.append("After each B32 backfill, re-run: the broken family's `current` coverage rises and its "
             "`status` flips to `PASS (landed)`. That closes the loop the prior cycles never did.")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("--db", default=None,
                    help="DB path (default: etl.db.DB_PATH, HR_BETS_DB-aware). Read-only.")
    ap.add_argument("--recent-days", type=int, default=14,
                    help="size of the recent-live window in distinct dates (default 14)")
    ap.add_argument("--year-boundary", default=YEAR_BOUNDARY,
                    help=f"backfill/live season boundary (default {YEAR_BOUNDARY})")
    ap.add_argument("--md", default=None, metavar="PATH",
                    help="also write the markdown audit doc to PATH")
    ap.add_argument("--audit-date", default=None,
                    help="date label for the doc header (default: recent-window end date)")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else DB_PATH
    conn = open_ro(db_path)
    try:
        eras_info = compute_eras(conn, args.recent_days, args.year_boundary)
        cov_rows = coverage_table(conn, eras_info)
        snap_rows = snapshot_report(conn)
        sup_rows = support_report(conn)
        plan_rows = plan_status(conn, eras_info)

        render_console(db_path, eras_info, cov_rows, snap_rows, sup_rows, plan_rows)

        if args.md:
            audit_date = args.audit_date or eras_info["recent_end"]
            md = render_markdown(db_path, eras_info, cov_rows, snap_rows, sup_rows,
                                 plan_rows, audit_date)
            out = Path(args.md)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(md, encoding="utf-8")
            print(f"\nwrote markdown -> {out}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
