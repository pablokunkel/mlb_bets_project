#!/usr/bin/env python3
"""
backfill_slate_pct.py - one-shot backfill for B16's three new pick_inputs
columns: slate_park_pct, slate_weather_pct, slate_pitcher_vulnerability_pct.

Why this exists: production scoring runs the three factors through
compute_slate_context, which computes within-slate percentile ranks (using
score_batters.percentile_rank_dict, the same mid-rank scaling production
uses). Before B16, pick_inputs didn't persist those percentile values, so
backtest_factors.rescore_row and refit_weights.rescore_row had to fall back
to v1 anchored formulas, training weight refits on scores that don't match
what production composites. This script replays compute_slate_context's
logic per-date against the already-persisted raw columns and UPDATEs the
three new slate_*_pct columns on existing rows.

Methodology (must match production):
  - PERCENTILE METHOD: percentile_rank_dict in score_batters.py (mid-rank
    average of count_less + count_less_or_equal, scaled to 0-100).
  - PARK: per-row hr_park_factor lookup, percentile within-date, then
    handedness-adjusted using park_factors_seed L/R splits — matches the
    post-handedness value score_park's slate-relative branch produces.
  - WEATHER: per-game composite `temp + wind*0.5 + humidity*0.05`, skip-
    on-missing per the compute_slate_context contract (any of temp/wind/
    humidity NULL -> skip row from the percentile pool). Domes stay NULL
    in slate_weather_pct (score_weather short-circuits to 50.0 there).
  - PITCHER: composite of `hr9*30 + era*5 + hh*0.6 + (-k9*2) + (fb-35)*0.8`
    using effective_hr9/effective_era/effective_k9 blends — matches
    compute_slate_context lines 244-294 exactly. Pitchers with <2 measured
    signals are EXCLUDED from the rank (mirrors compute_slate_context's
    `if len(components) < 2: continue`). The low-IP pull-toward-neutral
    nudge from compute_slate_context cannot be replayed because IP isn't
    persisted in pick_inputs — this is a documented small divergence that
    affects only pitchers with <10 IP (very rare; ~0.5% of rows).

Caveats:
  - The backfill writes the same value across all rows sharing a venue /
    game_pk / pitcher_name on a given date — production produces the same
    value per slate-group too, so this matches.
  - score_park's handedness adjustment is per-row (depends on batter's
    `bats`); we replay that per-row using the row's persisted `bats`.
  - Rows where the slate-relative path wasn't active in production (e.g.,
    venue/game_pk/pitcher missing from slate_ctx, dome weather, pitcher
    with <2 signals) STAY NULL — consistent with what compute_composite
    would have written.

Idempotent: re-running the script on the same DB produces identical
values. Safe to re-run after a partial run.

Usage:
    python diagnostics/backfill_slate_pct.py                 # all dates
    python diagnostics/backfill_slate_pct.py --since 2025-03-27 --end 2025-09-30
    python diagnostics/backfill_slate_pct.py --db custom.db
    python diagnostics/backfill_slate_pct.py --dry-run       # report only
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Make project root importable
_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from score_batters import percentile_rank_dict
from pitcher_profile import effective_hr9, effective_era, effective_k9

# Default DB path. Mirrors etl/db.py's convention
# (project_root.parent / "data") so the script works the same way from the
# main repo / production. When running from a git worktree the path math
# diverges (`.parent.parent.parent` lands in `.claude/worktrees/data/`,
# a stale snapshot) — pass `--db <path>` to override. CLAUDE.md documents
# this gotcha for backfill scripts running outside the production cwd.
DEFAULT_DB = _THIS.parent.parent.parent / "data" / "hr_bets.db"


def _seed_park_lookup() -> dict[str, tuple[float, float, float]]:
    """venue -> (overall, lhb, rhb) handedness factors from park_factors_seed.

    Mirrors production's get_hardcoded_park_factors() so the backfill applies
    the same L/R adjustment score_park does.
    """
    try:
        from etl.park_factors_seed import get_seed_dataframe
        df = get_seed_dataframe()
    except Exception:
        return {}
    out: dict[str, tuple[float, float, float]] = {}
    for _, row in df.iterrows():
        venue = row.get("venue")
        if not venue:
            continue
        lhb = float(row.get("hr_pf_lhb") or 100)
        rhb = float(row.get("hr_pf_rhb") or 100)
        overall = float(row.get("hr_pf_overall") or (lhb + rhb) / 2.0)
        out[str(venue)] = (overall, lhb, rhb)
    return out


def _seed_park_by_overall() -> dict[float, tuple[float, float, float]]:
    """hr_pf_overall -> (overall, lhb, rhb) reverse lookup.

    `daily_slate` is live-only (~last 28 days of 2026), so historical 2025
    backfill rows have no `venue` to JOIN on. Each pick_inputs row DOES
    persist `hr_park_factor` though, which IS the seed's `hr_pf_overall`
    for any stadium in the curated 30-venue seed. Mapping back from the
    overall PF to (overall, lhb, rhb) lets the backfill apply the same
    handedness adjustment score_park does without needing the venue name.

    Collisions are possible in principle (two venues with the same overall
    PF) but extremely rare in the seed; if a clash exists the dict picks
    the last-wins value, which only matters for the L/R split (the overall
    is identical by construction). Acceptable for backfill.
    """
    seed = _seed_park_lookup()
    out: dict[float, tuple[float, float, float]] = {}
    for venue, (overall, lhb, rhb) in seed.items():
        out[float(overall)] = (overall, lhb, rhb)
    return out


def _compute_pitcher_vuln_raw(p: dict) -> float | None:
    """Replicate compute_slate_context's pitcher vulnerability composite.

    Returns the raw value to feed into percentile_rank_dict, or None when
    fewer than 2 measured signals are available (mirrors the production
    `if len(components) < 2: continue` skip).

    Does NOT replay the low-IP pull-toward-neutral nudge because `ip` isn't
    persisted in pick_inputs. Affects only pitchers with measured ip<10
    (rare). Documented divergence from production.
    """
    components: list[float] = []
    hr9 = effective_hr9(
        p.get("pitcher_hr_per_9"),
        p.get("pitcher_recent_hr9_21d"),
        p.get("pitcher_recent_starts_21d"),
    )
    if hr9 is not None and hr9 > 0:
        components.append(hr9 * 30.0)
    era = effective_era(
        p.get("pitcher_era"),
        p.get("pitcher_recent_era_21d"),
        p.get("pitcher_recent_starts_21d"),
    )
    if era is not None and era > 0:
        components.append(era * 5.0)
    hh = p.get("pitcher_hh_pct")
    if hh is not None and hh > 0:
        components.append(hh * 0.6)
    k9 = effective_k9(
        p.get("pitcher_k_per_9"),
        p.get("pitcher_recent_k9_21d"),
        p.get("pitcher_recent_starts_21d"),
    )
    if k9 is not None and k9 > 0:
        components.append(-k9 * 2.0)
    fb_pct = p.get("pitcher_fb_pct_allowed")
    if fb_pct is not None:
        components.append((fb_pct - 35) * 0.8)
    if len(components) < 2:
        return None
    return sum(components)


def _backfill_one_date(conn: sqlite3.Connection, date_str: str,
                       park_seed: dict, park_seed_by_overall: dict,
                       dry_run: bool) -> tuple[int, int, int, int]:
    """Compute the three slate_*_pct columns for one date and UPDATE rows.

    Returns (n_rows, n_park_set, n_weather_set, n_pitcher_set).
    """
    # Some pick_inputs rows have multiple daily_picks rows (rare — happens
    # when a batter appears under two opp_pitcher candidates on the same
    # date — see DB quirk). Use GROUP BY pi.rowid + MIN/MAX to pick a
    # deterministic single matching daily_picks row per pick_inputs row.
    rows = conn.execute(
        """
        SELECT pi.rowid AS rid, pi.batter_id, pi.hr_park_factor, pi.bats,
               pi.temperature_f, pi.wind_mph, pi.humidity_pct, pi.is_dome,
               pi.pitcher_hr_per_9, pi.pitcher_era, pi.pitcher_hh_pct,
               pi.pitcher_k_per_9, pi.pitcher_fb_pct_allowed,
               pi.pitcher_recent_hr9_21d, pi.pitcher_recent_starts_21d,
               pi.pitcher_recent_era_21d, pi.pitcher_recent_k9_21d,
               MIN(dp.game_pk) AS game_pk,
               MIN(dp.opp_pitcher) AS opp_pitcher,
               MIN(ds.venue) AS game_venue
        FROM pick_inputs pi
        LEFT JOIN daily_picks dp
            ON dp.date = pi.date AND dp.batter_id = pi.batter_id
        LEFT JOIN daily_slate ds
            ON ds.game_pk = dp.game_pk AND ds.date = pi.date
        WHERE pi.date = ?
        GROUP BY pi.rowid
        """,
        (date_str,),
    ).fetchall()
    if not rows:
        return (0, 0, 0, 0)

    # Build venue identifier -> park_factor map. `daily_slate.venue` is
    # only populated for live 2026 dates (~last 28 days); backfilled 2025
    # rows have venue=NULL on the JOIN. We use a composite key:
    #   - venue name if present (live rows)
    #   - else `_pf_<rounded_overall>` so each unique hr_park_factor on
    #     the slate is a distinct percentile bucket (1 venue == 1 bucket,
    #     since each ballpark has a single overall PF in the seed).
    venue_pf: dict[str, float] = {}
    # Build game_pk -> (temp, wind, humidity, is_dome) map
    weather_per_game: dict[int, tuple[float, float, float, int]] = {}
    # Build pitcher_name -> pitcher_stats_dict map
    pitcher_stats: dict[str, dict] = {}
    # rowid -> the venue key used in venue_pf (for the percentile lookup
    # after the percentile_rank_dict call below).
    rowid_to_venue_key: dict[int, str] = {}

    for r in rows:
        (rowid, batter_id, hr_park_factor, bats, temp, wind, hum, is_dome,
         hr9, era, hh, k9, fb, rec_hr9, rec_starts, rec_era, rec_k9,
         game_pk, opp_pitcher, venue) = r

        # Pick the best available venue identifier for this row.
        venue_key: str | None = None
        if venue:
            venue_key = venue
            if venue_key not in venue_pf:
                seed = park_seed.get(venue)
                if seed:
                    venue_pf[venue_key] = seed[0]
                elif hr_park_factor is not None:
                    venue_pf[venue_key] = float(hr_park_factor)
        elif hr_park_factor is not None:
            # Synthetic venue key from PF — distinguishes the day's
            # unique PFs without needing the venue name.
            venue_key = f"_pf_{float(hr_park_factor):.3f}"
            if venue_key not in venue_pf:
                venue_pf[venue_key] = float(hr_park_factor)
        if venue_key is not None:
            rowid_to_venue_key[rowid] = venue_key

        if (game_pk is not None
                and game_pk not in weather_per_game
                and not is_dome
                and temp is not None and wind is not None and hum is not None):
            weather_per_game[game_pk] = (float(temp), float(wind), float(hum), int(is_dome or 0))

        if opp_pitcher and opp_pitcher not in pitcher_stats:
            pitcher_stats[opp_pitcher] = {
                "pitcher_hr_per_9": hr9,
                "pitcher_era": era,
                "pitcher_hh_pct": hh,
                "pitcher_k_per_9": k9,
                "pitcher_fb_pct_allowed": fb,
                "pitcher_recent_hr9_21d": rec_hr9,
                "pitcher_recent_starts_21d": rec_starts,
                "pitcher_recent_era_21d": rec_era,
                "pitcher_recent_k9_21d": rec_k9,
            }

    # Compute the three percentile maps using production's percentile_rank_dict
    park_pct_by_key = percentile_rank_dict(venue_pf)

    weather_raw_by_gpk: dict[int, float] = {}
    for gpk, (t, w, h, _dome) in weather_per_game.items():
        weather_raw_by_gpk[gpk] = t + w * 0.5 + h * 0.05
    weather_pct_by_gpk = percentile_rank_dict(weather_raw_by_gpk)

    pitcher_vuln_raw: dict[str, float] = {}
    for pname, p in pitcher_stats.items():
        raw = _compute_pitcher_vuln_raw(p)
        if raw is not None:
            pitcher_vuln_raw[pname] = raw
    pitcher_pct_by_name = percentile_rank_dict(pitcher_vuln_raw)

    # Now update each row with its per-row values.
    n_park = n_weather = n_pitcher = 0
    updates: list[tuple] = []
    for r in rows:
        (rowid, batter_id, hr_park_factor, bats, temp, wind, hum, is_dome,
         _hr9, _era, _hh, _k9, _fb, _rec_hr9, _rec_starts, _rec_era, _rec_k9,
         game_pk, opp_pitcher, venue) = r

        # Park: handedness-adjusted percentile, mirroring score_park's
        # slate-relative path.
        spp: float | None = None
        venue_key = rowid_to_venue_key.get(rowid)
        if venue_key is not None and venue_key in park_pct_by_key:
            base_pct = park_pct_by_key[venue_key]
            # Find L/R splits either via the venue name (if present) or
            # via reverse-lookup on the persisted overall PF.
            seed: tuple[float, float, float] | None = None
            if venue and venue in park_seed:
                seed = park_seed[venue]
            elif hr_park_factor is not None:
                seed = park_seed_by_overall.get(float(hr_park_factor))
            if seed:
                overall, lhb, rhb = seed
                if overall > 0:
                    bats_norm = (bats or "R") or "R"
                    if bats_norm == "L":
                        adj = (lhb - overall) / overall
                    elif bats_norm == "R":
                        adj = (rhb - overall) / overall
                    else:
                        adj = 0.0
                    base_pct = max(0, min(100, base_pct + adj * 50))
            spp = float(base_pct)

        # Weather: NULL for domes (production short-circuits to 50.0 at
        # score time) and for games missing any of temp/wind/humidity.
        swp: float | None = None
        if (game_pk is not None
                and not (is_dome or 0)
                and game_pk in weather_pct_by_gpk):
            swp = float(weather_pct_by_gpk[game_pk])

        # Pitcher: NULL when pitcher missing or has <2 signals.
        spv: float | None = None
        if opp_pitcher and opp_pitcher in pitcher_pct_by_name:
            spv = float(pitcher_pct_by_name[opp_pitcher])

        if spp is not None:
            n_park += 1
        if swp is not None:
            n_weather += 1
        if spv is not None:
            n_pitcher += 1

        updates.append((spp, swp, spv, rowid))

    if not dry_run and updates:
        conn.executemany(
            """
            UPDATE pick_inputs
            SET slate_park_pct = ?,
                slate_weather_pct = ?,
                slate_pitcher_vulnerability_pct = ?
            WHERE rowid = ?
            """,
            updates,
        )
        conn.commit()

    return (len(rows), n_park, n_weather, n_pitcher)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("--db", default=str(DEFAULT_DB),
                    help=f"DB path (default: {DEFAULT_DB})")
    ap.add_argument("--since", default=None, help="Start date YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", default=None, help="End date YYYY-MM-DD (inclusive)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be updated; no writes.")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        # Ensure the columns exist (idempotent — create_tables is a no-op
        # if they're already there).
        from etl.db import create_tables
        create_tables(conn)
    except Exception as e:
        print(f"WARN: create_tables migration call failed: {e}", file=sys.stderr)

    # Enumerate dates to process.
    where = []
    params: list = []
    if args.since:
        where.append("date >= ?")
        params.append(args.since)
    if args.end:
        where.append("date <= ?")
        params.append(args.end)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    dates_sql = f"SELECT DISTINCT date FROM pick_inputs {where_clause} ORDER BY date"
    dates = [r[0] for r in conn.execute(dates_sql, params).fetchall()]
    if not dates:
        print(f"No dates match (--since={args.since}, --end={args.end})")
        return 0

    park_seed = _seed_park_lookup()
    park_seed_by_overall = _seed_park_by_overall()
    if not park_seed:
        print("WARN: park_factors_seed unavailable; slate_park_pct will be "
              "computed from persisted hr_park_factor only (no L/R adjustment)",
              file=sys.stderr)

    total_rows = total_park = total_weather = total_pitcher = 0
    print(f"Backfilling {len(dates)} dates ({dates[0]} -> {dates[-1]})"
          f"{' [DRY RUN]' if args.dry_run else ''}")
    for d in dates:
        n_rows, n_park, n_weather, n_pitcher = _backfill_one_date(
            conn, d, park_seed, park_seed_by_overall, args.dry_run
        )
        total_rows += n_rows
        total_park += n_park
        total_weather += n_weather
        total_pitcher += n_pitcher

    conn.close()
    print(f"\nDone. Touched {total_rows} rows across {len(dates)} dates.")
    if total_rows > 0:
        print(f"  slate_park_pct                  non-NULL: "
              f"{total_park}/{total_rows} ({total_park/total_rows*100:.1f}%)")
        print(f"  slate_weather_pct               non-NULL: "
              f"{total_weather}/{total_rows} ({total_weather/total_rows*100:.1f}%)")
        print(f"  slate_pitcher_vulnerability_pct non-NULL: "
              f"{total_pitcher}/{total_rows} ({total_pitcher/total_rows*100:.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
