#!/usr/bin/env python3
"""
compute_park_factors.py — empirical HR park factors from our own hr_events,
shrunk toward the curated seed table (B35, 2026-08-21).

Why
---
The live scoring path used the hand-curated seed table (etl.park_factors_seed)
verbatim. Any venue missing from that table silently scored park = 50.0
(neutral). Sutter Health Park — the A's temporary home and the #1 HR park of
2026 by raw HR/game — had no row, so all 46 A's-home slates scored neutral.
This module replaces the static lookup with a nightly-computed blend so a
venue that is not in the curated table still gets a data-driven factor, and
venues that ARE in the table drift toward what the current season shows.

Method (standard one-year park factor, both teams)
--------------------------------------------------
For each venue V with a home team T (the modal home_team of games at V):

    empirical_V = (HR per game at V, both teams)
                  / (HR per game in T's ROAD games, both teams) * 100

Dividing by T's road games controls for team composition: the A's hitting
(and pitching) at home vs the same A's away.

Game universe: 2026 regular-season rows of daily_slate that have evidence of
having been played (an `outcomes` row exists for the same game_pk + date), so
postponed / not-yet-played slate rows do not inflate the denominator. HRs come
from hr_events joined by game_pk (hr_events.venue / batting_team are blank on
~970 early-season rows, so the slate is the authority for venue and teams).
daily_slate coverage starts 2026-04-29; the window is therefore the same for
numerator and denominator and the estimate is unbiased, just smaller-sample.

Shrinkage toward the curated prior
----------------------------------
    blended = w * empirical + (1 - w) * prior,   w = G / (G + K_OVERALL)

G = venue home games played, prior = curated hr_pf_overall (100 if the venue
has no curated row). K_OVERALL = 60: at the ~45-60 home games a venue has by
late August, w is 0.43-0.50 — the season's evidence and the multi-year curated
prior carry roughly equal weight, which is the standard one-year-vs-prior
balance for a stat as noisy as HR park factor (one-season PF has ~15-point
sampling noise at 60 games). With a full 81-game home slate w = 0.57. This is
the resolved choice from the B35 brief; it is NOT a tuned parameter.

Handedness
----------
Per-hand empirical rates use the same method restricted to LHB / RHB HRs
(switch hitters excluded from the per-hand numerators), shrunk with
K_HAND = 100 toward  blended_overall * curated_skew, where
curated_skew = curated hr_pf_<hand> / curated hr_pf_overall (1.0 with no
curated row). Hand comes from season_batting.bats (career_batting carries no
handedness column). GUARD: if the resolved handedness set is degenerate
(fewer than two distinct hands among HR hitters — which is the state of the
DB today: season_batting.bats is 100% 'R' because the /stats endpoint does
not return batSide), per-hand empirical rates are NOT computed and the hand
columns fall back to blended_overall * curated_skew. `hand_source` in the
notes column records which branch ran.

Persistence
-----------
Rows are upserted into park_factors with season=<season> and
source='empirical_blend_v1'. Curated rows (source='seed') are never touched —
they are the prior. The park_factors PK is (venue, season, source) so both
coexist (migrated in etl.db.create_tables). Inputs (G, raw empirical, prior,
w, hand_source) are recorded in `notes` as a compact key=value string.

Read path
---------
`load_park_factors_for_scoring()` is what production scoring consumes:
blended row (latest season that has any) -> curated DB row -> in-code seed
table, per venue, never raising. Every venue carries a `pf_source` column so
callers can log loudly when a venue on today's slate fell through to a
default — a silent neutral default for an active venue is exactly the bug
this module fixes.

Usage
-----
    python -m etl.compute_park_factors                  # compute + write + print
    python -m etl.compute_park_factors --dry-run        # compute + print only
    python -m etl.compute_park_factors --db path.db     # explicit DB
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from etl.park_factors_seed import get_seed_dataframe

BLEND_SOURCE = "empirical_blend_v1"
CURATED_SOURCE = "seed"
K_OVERALL = 60
K_HAND = 100
NEUTRAL_PF = 100.0


# ---------------------------------------------------------------------------
# Pure math (pinned in tests/smoke.py)
# ---------------------------------------------------------------------------

def shrink_weight(n_games: int, k: int) -> float:
    """w = G / (G + K); 0 when no games."""
    if n_games <= 0:
        return 0.0
    return n_games / (n_games + k)


def blend(empirical: float | None, prior: float, n_games: int, k: int) -> tuple[float, float]:
    """Return (blended, w). With no empirical estimate the prior is returned (w=0)."""
    if empirical is None:
        return float(prior), 0.0
    w = shrink_weight(n_games, k)
    return w * float(empirical) + (1.0 - w) * float(prior), w


def _rate_ratio(hr_home: int, g_home: int, hr_road: int, g_road: int) -> float | None:
    """(hr_home/g_home) / (hr_road/g_road) * 100; None if either side is empty."""
    if g_home <= 0 or g_road <= 0 or hr_road <= 0:
        return None
    return (hr_home / g_home) / (hr_road / g_road) * 100.0


# ---------------------------------------------------------------------------
# Data pulls
# ---------------------------------------------------------------------------

def _played_games(conn: sqlite3.Connection, season: int, through_date: str | None) -> pd.DataFrame:
    """Regular-season slate rows with evidence of play (an outcomes row)."""
    sql = """
        SELECT DISTINCT s.game_pk, s.date, s.home_team, s.away_team, s.venue
        FROM daily_slate s
        WHERE s.date LIKE ?
          AND s.venue IS NOT NULL AND s.venue != ''
          AND EXISTS (SELECT 1 FROM outcomes o
                      WHERE o.game_pk = s.game_pk AND o.date = s.date)
    """
    params: list = [f"{season}-%"]
    if through_date:
        sql += " AND s.date < ?"
        params.append(through_date)
    df = pd.read_sql_query(sql, conn, params=params)
    # A game_pk can appear on two slate dates (postponed then replayed); the
    # outcomes join above already restricts to the date it was played, but
    # guard against a double-header style duplicate just in case.
    return df.drop_duplicates("game_pk")


def _hr_by_game(conn: sqlite3.Connection, season: int) -> pd.DataFrame:
    """One row per HR with game_pk, half_inning, batter_id (batting side
    derived from half_inning: top = away, bottom = home)."""
    return pd.read_sql_query(
        """
        SELECT game_pk, half_inning, batter_id
        FROM hr_events
        WHERE date LIKE ?
        """,
        conn, params=(f"{season}-%",),
    )


def _batter_hands(conn: sqlite3.Connection, season: int) -> dict[int, str]:
    """batter_id -> 'L'/'R'/'S' from season_batting (career_batting has no
    handedness column, so there is no second source)."""
    try:
        rows = conn.execute(
            "SELECT player_id, bats FROM season_batting WHERE season = ? AND bats IS NOT NULL",
            (season,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {int(pid): str(b).upper()[:1] for pid, b in rows if b}


def _curated_priors(conn: sqlite3.Connection | None, season: int) -> dict[str, tuple[float, float, float]]:
    """venue -> (overall, lhb, rhb) from the curated DB rows for the latest
    seed season <= season, falling back to the in-code seed table."""
    priors: dict[str, tuple[float, float, float]] = {}
    seed = get_seed_dataframe()
    for _, r in seed.iterrows():
        priors[r["venue"]] = (float(r["hr_pf_overall"]), float(r["hr_pf_lhb"]), float(r["hr_pf_rhb"]))
    if conn is None:
        return priors
    try:
        row = conn.execute(
            "SELECT MAX(season) FROM park_factors WHERE source = ? AND season <= ?",
            (CURATED_SOURCE, season),
        ).fetchone()
        if row and row[0] is not None:
            for venue, o, l, r in conn.execute(
                "SELECT venue, hr_pf_overall, hr_pf_lhb, hr_pf_rhb FROM park_factors "
                "WHERE source = ? AND season = ?",
                (CURATED_SOURCE, row[0]),
            ).fetchall():
                priors[venue] = (float(o), float(l), float(r))
    except sqlite3.OperationalError:
        pass
    return priors


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_empirical_park_factors(
    conn: sqlite3.Connection,
    season: int,
    *,
    through_date: str | None = None,
    k_overall: int = K_OVERALL,
    k_hand: int = K_HAND,
) -> pd.DataFrame:
    """Return one row per venue with games this season.

    Columns: venue, home_team, g_home, hr_home, g_road, hr_road, empirical,
    prior, w, hr_pf_overall, hr_pf_lhb, hr_pf_rhb, hand_source, notes.
    """
    games = _played_games(conn, season, through_date)
    if games.empty:
        return pd.DataFrame()
    hrs = _hr_by_game(conn, season)
    hrs = hrs[hrs["game_pk"].isin(set(games["game_pk"]))]

    hands = _batter_hands(conn, season)
    hr_batters = set(hrs["batter_id"].dropna().astype(int))
    distinct_hands = {hands.get(b) for b in hr_batters} - {None, ""}
    hand_ok = len(distinct_hands & {"L", "R"}) == 2
    hand_source = "season_batting" if hand_ok else "none(degenerate)"

    hrs = hrs.copy()
    hrs["hand"] = hrs["batter_id"].map(lambda b: hands.get(int(b), "") if pd.notna(b) else "")
    hr_total = hrs.groupby("game_pk").size()
    hr_l = hrs[hrs["hand"] == "L"].groupby("game_pk").size()
    hr_r = hrs[hrs["hand"] == "R"].groupby("game_pk").size()

    def _sum(series: pd.Series, gpks) -> int:
        return int(series.reindex(gpks).fillna(0).sum())

    priors = _curated_priors(conn, season)
    out = []
    for venue, vg in games.groupby("venue"):
        home_team = vg["home_team"].mode().iloc[0]
        home_gpks = list(vg["game_pk"])
        road = games[games["away_team"] == home_team]
        road_gpks = list(road["game_pk"])
        g_home, g_road = len(home_gpks), len(road_gpks)
        hr_home, hr_road = _sum(hr_total, home_gpks), _sum(hr_total, road_gpks)

        prior_o, prior_l, prior_r = priors.get(venue, (NEUTRAL_PF, NEUTRAL_PF, NEUTRAL_PF))
        empirical = _rate_ratio(hr_home, g_home, hr_road, g_road)
        blended, w = blend(empirical, prior_o, g_home, k_overall)

        skew_l = prior_l / prior_o if prior_o > 0 else 1.0
        skew_r = prior_r / prior_o if prior_o > 0 else 1.0
        hand_prior_l = blended * skew_l
        hand_prior_r = blended * skew_r
        if hand_ok:
            emp_l = _rate_ratio(_sum(hr_l, home_gpks), g_home, _sum(hr_l, road_gpks), g_road)
            emp_r = _rate_ratio(_sum(hr_r, home_gpks), g_home, _sum(hr_r, road_gpks), g_road)
            pf_l, _ = blend(emp_l, hand_prior_l, g_home, k_hand)
            pf_r, _ = blend(emp_r, hand_prior_r, g_home, k_hand)
        else:
            pf_l, pf_r = hand_prior_l, hand_prior_r

        emp_txt = f"{empirical:.1f}" if empirical is not None else "NA"
        notes = (
            f"G={g_home} hr_home={hr_home} g_road={g_road} hr_road={hr_road} "
            f"empirical={emp_txt} prior={prior_o:.0f} w={w:.3f} "
            f"k={k_overall} k_hand={k_hand} hand_source={hand_source} home_team={home_team}"
        )
        out.append({
            "venue": venue, "home_team": home_team,
            "g_home": g_home, "hr_home": hr_home, "g_road": g_road, "hr_road": hr_road,
            "empirical": empirical, "prior": prior_o, "w": w,
            "hr_pf_overall": round(blended, 1),
            "hr_pf_lhb": round(pf_l, 1),
            "hr_pf_rhb": round(pf_r, 1),
            "hand_source": hand_source,
            "notes": notes,
        })
    return pd.DataFrame(out).sort_values("hr_pf_overall", ascending=False).reset_index(drop=True)


def upsert_blended_rows(conn: sqlite3.Connection, df: pd.DataFrame, season: int) -> int:
    """Idempotent upsert of blended rows (PK venue+season+source)."""
    if df is None or df.empty:
        return 0
    n = 0
    for _, r in df.iterrows():
        conn.execute(
            """
            INSERT OR REPLACE INTO park_factors
            (venue, season, hr_pf_overall, hr_pf_lhb, hr_pf_rhb, source, notes, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (r["venue"], season, float(r["hr_pf_overall"]), float(r["hr_pf_lhb"]),
             float(r["hr_pf_rhb"]), BLEND_SOURCE, r["notes"]),
        )
        n += 1
    conn.commit()
    return n


def sync_empirical_park_factors(conn: sqlite3.Connection, season: int) -> int:
    """Nightly ETL entry point: compute + upsert. Never raises on thin data."""
    print("\n  [8/8] Empirical park factors (B35)...")
    try:
        df = compute_empirical_park_factors(conn, season)
    except Exception as e:  # fail-soft: scoring falls back to curated rows
        print(f"    compute failed ({type(e).__name__}: {e}); curated rows remain the read path")
        return 0
    if df.empty:
        print("    no played games in daily_slate for this season yet; nothing written")
        return 0
    n = upsert_blended_rows(conn, df, season)
    hand_src = df["hand_source"].iloc[0]
    print(f"  [8/8] Done. {n} venues written as source={BLEND_SOURCE} (hand_source={hand_src})")
    return n


# ---------------------------------------------------------------------------
# Read path for scoring
# ---------------------------------------------------------------------------

PF_COLS = ["venue", "hr_pf_overall", "hr_pf_lhb", "hr_pf_rhb", "hr_park_factor", "pf_source"]


def _hardcoded_df() -> pd.DataFrame:
    df = get_seed_dataframe()[["venue", "hr_pf_overall", "hr_pf_lhb", "hr_pf_rhb"]].copy()
    df["hr_park_factor"] = df["hr_pf_overall"]
    df["pf_source"] = "hardcoded"
    return df[PF_COLS]


def load_park_factors_for_scoring(db_path: Path | str | None = None) -> pd.DataFrame:
    """Per-venue park factors with resolution order
    blended (latest season with rows) -> curated DB row -> in-code seed.

    Never raises. If the DB is unreachable the seed table is returned with
    pf_source='hardcoded' and a loud warning is printed. Each row's
    `pf_source` is one of 'empirical_blend_v1', 'seed', 'hardcoded'.
    """
    hard = _hardcoded_df()
    try:
        if db_path is None:
            from etl.db import DB_PATH
            db_path = DB_PATH
        db_path = Path(db_path)
        if not db_path.exists():
            raise FileNotFoundError(db_path)
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                """
                SELECT venue, hr_pf_overall, hr_pf_lhb, hr_pf_rhb, source, season
                FROM park_factors
                WHERE season = (SELECT MAX(season) FROM park_factors p2
                                WHERE p2.source = park_factors.source)
                """
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        print(f"  [park_factors] WARNING: DB read failed ({type(e).__name__}: {e}); "
              f"using HARDCODED seed table for all venues")
        return hard

    blended = {r[0]: r for r in rows if r[4] == BLEND_SOURCE}
    curated = {r[0]: r for r in rows if r[4] != BLEND_SOURCE}
    out = []
    for venue in sorted(set(blended) | set(curated) | set(hard["venue"])):
        if venue in blended:
            r, src = blended[venue], BLEND_SOURCE
        elif venue in curated:
            r, src = curated[venue], curated[venue][4] or CURATED_SOURCE
        else:
            h = hard[hard["venue"] == venue].iloc[0]
            r, src = (venue, h["hr_pf_overall"], h["hr_pf_lhb"], h["hr_pf_rhb"]), "hardcoded"
        out.append({
            "venue": venue, "hr_pf_overall": float(r[1]), "hr_pf_lhb": float(r[2]),
            "hr_pf_rhb": float(r[3]), "hr_park_factor": float(r[1]), "pf_source": src,
        })
    return pd.DataFrame(out, columns=PF_COLS)


def report_slate_park_coverage(pf_df: pd.DataFrame, venues, status=None, label: str = "Park Factor Match") -> list[str]:
    """Print per-venue resolution for today's slate; return venues that fell
    through to a default (missing entirely, or only the hardcoded table).

    Loud by design: a venue scoring park=50 silently is the B35 bug.
    """
    src_by_venue = dict(zip(pf_df["venue"], pf_df["pf_source"])) if not pf_df.empty else {}
    fell_through = []
    counts: dict[str, int] = {}
    for v in sorted(set(venues)):
        src = src_by_venue.get(v, "MISSING->neutral 100")
        counts[src] = counts.get(src, 0) + 1
        if src not in (BLEND_SOURCE, CURATED_SOURCE):
            fell_through.append(f"{v} [{src}]")
    summary = ", ".join(f"{k}={n}" for k, n in sorted(counts.items()))
    if fell_through:
        print(f"  [park_factors] WARNING: {len(fell_through)} slate venue(s) fell through to a "
              f"default park factor: {'; '.join(fell_through)}")
        if status is not None:
            status.warn(label, f"{len(fell_through)} defaulted: {'; '.join(fell_through)} ({summary})")
    else:
        print(f"  [park_factors] slate venues resolved: {summary}")
        if status is not None:
            status.ok(label, f"{len(set(venues))} venues resolved ({summary})")
    return fell_through


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_table(df: pd.DataFrame, priors_flag_delta: float = 20.0) -> None:
    print(f"\n  {'Venue':<30} {'G':>3} {'HR/G':>5} {'RoadHR/G':>8} {'Emp':>6} {'Prior':>5} "
          f"{'w':>5} {'Blend':>6} {'LHB':>6} {'RHB':>6}  flag")
    print("  " + "-" * 100)
    for _, r in df.iterrows():
        hrg = r["hr_home"] / r["g_home"] if r["g_home"] else 0
        rhg = r["hr_road"] / r["g_road"] if r["g_road"] else 0
        emp = f"{r['empirical']:.1f}" if r["empirical"] is not None and not pd.isna(r["empirical"]) else "NA"
        delta = r["hr_pf_overall"] - r["prior"]
        flag = f"MOVED {delta:+.0f}" if abs(delta) > priors_flag_delta else ""
        print(f"  {r['venue']:<30} {r['g_home']:>3} {hrg:>5.2f} {rhg:>8.2f} {emp:>6} {r['prior']:>5.0f} "
              f"{r['w']:>5.2f} {r['hr_pf_overall']:>6.1f} {r['hr_pf_lhb']:>6.1f} {r['hr_pf_rhb']:>6.1f}  {flag}")
    print(f"\n  hand_source = {df['hand_source'].iloc[0] if not df.empty else 'n/a'}")


def main() -> int:
    from etl.db import DB_PATH, get_db, create_tables
    ap = argparse.ArgumentParser(description="Empirical HR park factors (B35)")
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--dry-run", action="store_true", help="compute + print, no write")
    ap.add_argument("--through", default=None, help="only games strictly before this date")
    args = ap.parse_args()

    from datetime import date
    season = args.season or date.today().year
    if args.dry_run:
        conn = sqlite3.connect(f"file:{Path(args.db).as_posix()}?mode=ro", uri=True)
    else:
        conn = get_db(args.db)
        create_tables(conn)
    try:
        df = compute_empirical_park_factors(conn, season, through_date=args.through)
        if df.empty:
            print("  no played games found")
            return 1
        print_table(df)
        if not args.dry_run:
            n = upsert_blended_rows(conn, df, season)
            print(f"\n  wrote {n} rows (source={BLEND_SOURCE}, season={season}) to {args.db}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
