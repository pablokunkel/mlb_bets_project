#!/usr/bin/env python3
"""
export_site_data.py — Export DB data to static JSON for the dashboard at dingersonly.cc.

Reads from the SQLite database and writes JSON files that the frontend
can fetch() at runtime. Run this after generate_picks.py each day,
then git push to deploy.

Usage:
    python export_site_data.py                     # export to mlb_hr_bet_site/data/
    python export_site_data.py --out ./docs/data   # custom output dir
    python export_site_data.py --days 30           # last 30 days of history

Output files:
    picks_latest.json      — today's card + full board
    picks_history.json     — daily pick results with outcomes
    performance.json       — aggregate stats (hit rates, streaks, factor analysis)
    factor_trends.json     — per-factor score averages over time (for charts)
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from etl.db import get_db, create_tables


def atomic_write_json(path: Path, data, indent: int = 2) -> None:
    """
    Write JSON atomically: serialize fully into a temp file in the same
    directory, fsync, then os.replace() to the destination. This prevents
    OneDrive (or any sync watcher) from observing or syncing a half-written
    file — the rename is atomic on Windows and POSIX.

    A previous attempt left truncated picks_history.json / picks_latest.json
    on the deployed site because plain json.dump-into-open-file is NOT atomic.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Clean up temp file if anything went wrong
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def export_latest_picks(conn, out_dir: Path):
    """
    Export the most recent day's picks and full board.

    2026-05-01 augmentation for the new modal + Big Board sidebar:
      - Per-factor RANK within the full board (rank_power, rank_matchup, ...)
        so the modal can show "ranks 12th in power on the slate" per pick.
      - Season-to-date stats (HR, AB, AVG, ISO, barrel_pct, exit_velo, etc.)
        joined from season_batting — gives the modal real "this year" numbers
        instead of just our model scores.
      - Game time + venue + dome status from daily_slate, so each pick knows
        when its game starts (surface as "7:05 PM" in the UI).
      - Model hits / days picked / hit rate when picked — tracks how this
        player has performed historically *for our card*.
      - Per-game slate summary block (slate_games) — list of today's games
        with weather/Vegas/"X in top 25" counts, used by the Big Board
        sidebar.
    """

    # Find the latest date with picks
    row = conn.execute(
        "SELECT MAX(date) FROM daily_picks WHERE selected = 1"
    ).fetchone()
    latest_date = row[0] if row and row[0] else None

    if not latest_date:
        results_dir = Path(__file__).parent.parent / "results"
        json_files = sorted(results_dir.glob("picks_*.json"), reverse=True)
        if json_files:
            with open(json_files[0]) as f:
                data = json.load(f)
            atomic_write_json(out_dir / "picks_latest.json", data)
            print(f"  Exported picks_latest.json from {json_files[0].name}")
            return
        print("  WARNING: No picks found in DB or results/")
        return

    season = int(latest_date[:4])

    # Build full board with factor ranks via SQL window functions. RANK()
    # gives ties the same rank (e.g., two players tied for 5th both get 5).
    # NULL scores get sorted last via NULLS LAST trick.
    board_rows = conn.execute("""
        WITH base AS (
            SELECT
                p.batter_id, p.batter_name, p.team, p.tier, p.tier_label,
                p.opp_pitcher, p.composite,
                p.power_score, p.matchup_score, p.park_score,
                p.form_score, p.weather_score, p.lineup_score,
                p.batting_order, p.matchup_version, p.game_pk,
                p.selected, p.rank_in_board,
                -- 2026-05-04: pull lineup_source from pick_inputs so the
                -- dashboard Topps modal can render a "📋 from {date}"
                -- badge on picks whose batting_order came from a
                -- recent-lineup fallback (PR #33). NULL on historical
                -- rows that pre-date the pick_inputs.lineup_source column.
                pi.lineup_source AS lineup_source
            FROM daily_picks p
            LEFT JOIN pick_inputs pi
                ON pi.date = p.date AND pi.batter_id = p.batter_id
            WHERE p.date = ?
        )
        SELECT
            base.*,
            RANK() OVER (ORDER BY power_score   DESC) AS rank_power,
            RANK() OVER (ORDER BY matchup_score DESC) AS rank_matchup,
            RANK() OVER (ORDER BY park_score    DESC) AS rank_park,
            RANK() OVER (ORDER BY form_score    DESC) AS rank_form,
            RANK() OVER (ORDER BY weather_score DESC) AS rank_weather,
            RANK() OVER (ORDER BY lineup_score  DESC) AS rank_lineup,
            (SELECT COUNT(*) FROM base) AS board_size
        FROM base
        ORDER BY composite DESC
    """, (latest_date,)).fetchall()

    board = [dict(r) for r in board_rows]

    # Season stats lookup — keyed on batter_id. season_batting has snapshot of
    # season-to-date numbers; refreshed nightly by ETL. Stale by ≤1 day.
    season_rows = conn.execute("""
        SELECT player_id, hr, ab, pa, hr_per_pa, avg, slg, obp, iso, woba,
               barrel_pct, exit_velo, hr_fb_pct
        FROM season_batting
        WHERE season = ?
    """, (season,)).fetchall()
    season_by_id = {r["player_id"]: dict(r) for r in season_rows}

    # Model track record per batter — across all selected picks in this DB,
    # how many days were they picked and on how many of those did they HR.
    track_rows = conn.execute("""
        SELECT
            dp.batter_id,
            COUNT(*)                                            AS days_picked,
            SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END)     AS model_hits
        FROM daily_picks dp
        LEFT JOIN outcomes o ON o.date = dp.date AND o.batter_id = dp.batter_id
        WHERE dp.selected = 1
        GROUP BY dp.batter_id
    """).fetchall()
    track_by_id = {r["batter_id"]: dict(r) for r in track_rows}

    # Game-level data for today: time, venue, weather, Vegas total. Used to
    # surface game_time in each pick + power the slate sidebar.
    game_rows = conn.execute("""
        SELECT game_pk, home_team, away_team, venue, game_time,
               temperature_f, wind_mph, wind_dir_deg, humidity_pct, dome,
               home_pitcher, away_pitcher
        FROM daily_slate
        WHERE date = ?
    """, (latest_date,)).fetchall()
    games_by_pk = {r["game_pk"]: dict(r) for r in game_rows}

    # Vegas implied totals per team — same source the matchup score uses.
    # We pull from the cache the morning ETL hit, which lives in slate_ctx
    # at scoring time but isn't persisted per-team. Approximation here:
    # if pick_inputs has vegas_team_total_pct for any batter on this team
    # today, that's their team's percentile rank (already 0-100).
    # 2026-05-03: column renamed from vegas_implied_total — see migration
    # in etl/db.py. Old DBs are auto-renamed on first create_tables call.
    vegas_rows = conn.execute("""
        SELECT DISTINCT dp.team, AVG(pi.vegas_team_total_pct) AS team_total_pct
        FROM daily_picks dp
        LEFT JOIN pick_inputs pi ON pi.date = dp.date AND pi.batter_id = dp.batter_id
        WHERE dp.date = ? AND pi.vegas_team_total_pct IS NOT NULL
        GROUP BY dp.team
    """, (latest_date,)).fetchall()
    vegas_by_team = {r["team"]: r["team_total_pct"] for r in vegas_rows}

    # Top-25 board members per game_pk — for the slate sidebar's
    # "X in top 25" badge that flags where today's HRs are coming from.
    top25_pks = [b["game_pk"] for b in board[:25] if b.get("game_pk")]
    top25_per_game: dict = {}
    for gpk in top25_pks:
        top25_per_game[gpk] = top25_per_game.get(gpk, 0) + 1

    # 2026-05-05 Hot Streak rework: per-batter HR count over the last 7
    # days STRICTLY BEFORE `latest_date`. Powers the Lab tab's reworked
    # Hot Streak view ("top 10 by 7d HR with favorable matchup × park ×
    # weather"). Distinct from `recent_hr_14d` already on pick_inputs —
    # 7d is the spec Pablo asked for; the 14d field stays for the form_
    # score calc.
    #
    # Date math: latest_date is the picks date (e.g., today). We want
    # the 7 days BEFORE that — strict less-than so today's in-progress
    # HRs don't double-count once they're recorded.
    recent_hr_7d_rows = conn.execute("""
        SELECT batter_id, SUM(hr_count) AS hr_7d
        FROM outcomes
        WHERE date >= date(?, '-7 days') AND date < ?
        GROUP BY batter_id
        HAVING SUM(hr_count) > 0
    """, (latest_date, latest_date)).fetchall()
    recent_hr_7d_by_id = {r["batter_id"]: r["hr_7d"] for r in recent_hr_7d_rows}

    # Augment each batter row with season stats + model track + game info.
    def _augment(b: dict) -> dict:
        season = season_by_id.get(b.get("batter_id"), {})
        track  = track_by_id.get(b.get("batter_id"), {"days_picked": 0, "model_hits": 0})
        game   = games_by_pk.get(b.get("game_pk"), {})
        days_picked = track.get("days_picked") or 0
        model_hits  = track.get("model_hits")  or 0
        b["season_stats"] = {
            "hr":         season.get("hr"),
            "ab":         season.get("ab"),
            "pa":         season.get("pa"),
            "avg":        season.get("avg"),
            "slg":        season.get("slg"),
            "obp":        season.get("obp"),
            "iso":        season.get("iso"),
            "woba":       season.get("woba"),
            "barrel_pct": season.get("barrel_pct"),
            "exit_velo":  season.get("exit_velo"),
            "hr_fb_pct":  season.get("hr_fb_pct"),
        }
        b["model_track"] = {
            "days_picked": days_picked,
            "model_hits":  model_hits,
            "hit_rate":    round(model_hits / days_picked * 100, 1) if days_picked else None,
        }
        # 7-day HR rolling count (strictly before latest_date). Used by
        # the Lab tab's Hot Streak view. None when batter had zero HRs
        # in the window (most batters); the JS treats None as 0.
        b["recent_hr_7d"] = recent_hr_7d_by_id.get(b.get("batter_id"))
        b["game_time"]  = game.get("game_time")
        b["venue"]      = game.get("venue")
        b["dome"]       = game.get("dome")
        return b

    board = [_augment(b) for b in board]
    picks = [b for b in board if b.get("selected") == 1]

    # Slate summary for the Big Board sidebar — one entry per game today.
    slate_games = []
    for gpk, g in sorted(games_by_pk.items(), key=lambda kv: (kv[1].get("game_time") or "")):
        # How many top-25 board members are in this game?
        top25_count = top25_per_game.get(gpk, 0)
        # Avg Vegas implied total across the two teams (if present)
        vegas_home = vegas_by_team.get(g.get("home_team"))
        vegas_away = vegas_by_team.get(g.get("away_team"))
        vegas_avg  = None
        present = [v for v in (vegas_home, vegas_away) if v is not None]
        if present:
            vegas_avg = round(sum(present) / len(present), 1)
        slate_games.append({
            "game_pk":       gpk,
            "home_team":     g.get("home_team"),
            "away_team":     g.get("away_team"),
            "venue":         g.get("venue"),
            "game_time":     g.get("game_time"),
            "temperature_f": g.get("temperature_f"),
            "wind_mph":      g.get("wind_mph"),
            "wind_dir_deg":  g.get("wind_dir_deg"),
            "dome":          g.get("dome"),
            "home_pitcher":  g.get("home_pitcher"),
            "away_pitcher":  g.get("away_pitcher"),
            "top25_count":   top25_count,
            "vegas_pct_avg": vegas_avg,
        })

    data = {
        "date": latest_date,
        "generated_at": datetime.now().isoformat(),
        "picks":        picks,
        "full_board":   board,
        "board_size":   len(board),
        "slate_games":  slate_games,
    }

    atomic_write_json(out_dir / "picks_latest.json", data)
    print(f"  Exported picks_latest.json ({len(picks)} picks, {len(board)} board, {len(slate_games)} games)")


def export_history(conn, out_dir: Path, days: int = 60):
    """Export daily pick history with outcomes."""

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT
            p.date,
            p.batter_id,
            p.batter_name AS name,
            p.team,
            p.tier,
            p.tier_label,
            p.opp_pitcher,
            p.composite,
            p.power_score,
            p.matchup_score,
            p.park_score,
            p.form_score,
            p.weather_score,
            p.lineup_score,
            p.batting_order,
            COALESCE(o.hr_count, -1) AS hr_count,
            COALESCE(o.ab, 0) AS ab,
            COALESCE(o.hits, 0) AS hits,
            COALESCE(o.rbi, 0) AS rbi,
            COALESCE(o.doubles, 0) AS doubles,
            COALESCE(o.triples, 0) AS triples,
            COALESCE(o.total_bases, 0) AS total_bases,
            CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END AS hit
        FROM daily_picks p
        LEFT JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1 AND p.date >= ?
        ORDER BY p.date DESC, p.composite DESC
    """, (cutoff,)).fetchall()

    # Group by date
    by_date = {}
    for r in rows:
        d = r["date"]
        if d not in by_date:
            by_date[d] = {"date": d, "picks": [], "total": 0, "hits": 0}
        entry = dict(r)
        del entry["date"]
        by_date[d]["picks"].append(entry)
        by_date[d]["total"] += 1
        if r["hr_count"] > 0:
            by_date[d]["hits"] += 1

    # Add hit_rate per day. Drop days where outcomes haven't been ingested
    # yet (hr_count = -1 from the COALESCE means no outcomes row exists).
    history = []
    for d in sorted(by_date.keys(), reverse=True):
        day = by_date[d]
        day["hit_rate"] = round(day["hits"] / max(day["total"], 1) * 100, 1)
        outcomes_available = any(p["hr_count"] >= 0 for p in day["picks"])
        if not outcomes_available:
            continue
        day["outcomes_available"] = outcomes_available
        history.append(day)

    data = {
        "days": days,
        "total_days": len(history),
        "history": history,
        "exported_at": datetime.now().isoformat(),
    }

    atomic_write_json(out_dir / "picks_history.json", data)
    print(f"  Exported picks_history.json ({len(history)} days)")


def export_performance(conn, out_dir: Path):
    """Export aggregate performance stats."""

    # Overall hit rate
    overall = conn.execute("""
        SELECT
            COUNT(*) AS total_picks,
            SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) AS hits
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1
    """).fetchone()

    total = overall["total_picks"] or 0
    hits = overall["hits"] or 0

    # By tier
    tier_rows = conn.execute("""
        SELECT
            p.tier_label,
            COUNT(*) AS total,
            SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) AS hits
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1 AND p.tier_label IS NOT NULL
        GROUP BY p.tier_label
    """).fetchall()

    # By matchup version
    version_rows = conn.execute("""
        SELECT
            COALESCE(p.matchup_version, 'v1') AS version,
            COUNT(*) AS total,
            SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) AS hits
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1
        GROUP BY version
    """).fetchall()

    # Composite score for hits vs misses
    composite_row = conn.execute("""
        SELECT
            ROUND(AVG(CASE WHEN o.hr_count > 0 THEN p.composite END), 1) AS avg_hit,
            ROUND(AVG(CASE WHEN o.hr_count = 0 THEN p.composite END), 1) AS avg_miss
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1
    """).fetchone()

    # Top hitters (most HR hits from our picks)
    top_hitters = conn.execute("""
        SELECT
            p.batter_name AS name,
            p.team,
            COUNT(*) AS times_picked,
            SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) AS times_hit,
            ROUND(AVG(p.composite), 1) AS avg_composite
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1
        GROUP BY p.batter_id
        HAVING times_picked >= 3
        ORDER BY times_hit DESC, avg_composite DESC
        LIMIT 15
    """).fetchall()

    # Current streak (consecutive days with at least 1 hit)
    daily_hits = conn.execute("""
        SELECT p.date,
               SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) AS hits
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1
        GROUP BY p.date
        ORDER BY p.date DESC
    """).fetchall()

    streak = 0
    for r in daily_hits:
        if r["hits"] > 0:
            streak += 1
        else:
            break

    data = {
        "overall": {
            "total_picks": total,
            "hits": hits,
            "hit_rate": round(hits / max(total, 1) * 100, 1),
        },
        "by_tier": [dict(r) for r in tier_rows],
        "by_matchup_version": [dict(r) for r in version_rows],
        "composite_analysis": {
            "avg_hit_composite": composite_row["avg_hit"],
            "avg_miss_composite": composite_row["avg_miss"],
        },
        "top_hitters": [dict(r) for r in top_hitters],
        "current_hit_streak_days": streak,
        "score_distribution": _score_distribution(conn),
        "factor_diagnostic": _factor_diagnostic(conn, days=30),
        "factor_decomp": _factor_decomp(conn, days=30),
        "factor_decomp_by_band": _factor_decomp_by_band(conn, days=30),
        "factor_decomp_hr_split": _factor_decomp_hr_split(conn, days=30),
        "factor_decomp_by_rank": _factor_decomp_by_rank_band(conn, days=30),
        "factor_decomp_hr_rank_split": _factor_decomp_hr_split_by_rank(conn, days=30),
        "input_calibration":     _input_calibration(conn,    days=60),
        "dome_vs_outdoor":       _dome_vs_outdoor(conn,      days=60),
        "pick_composition":      _pick_composition(conn,     days=60),
        "wind_direction_diagnostic": _wind_direction_diagnostic(conn, days=60),
        "temp_humidity_heatmap": _temp_humidity_heatmap(conn, days=60),
        "temp_humidity_heatmap_historical": _temp_humidity_heatmap_historical(conn),
        "archetype_dampening":   _archetype_dampening_diagnostic(conn, days=60),
        "exported_at": datetime.now().isoformat(),
    }

    atomic_write_json(out_dir / "performance.json", data)
    print(f"  Exported performance.json ({total} picks, {hits} hits)")


def _score_distribution(conn) -> list:
    """
    Composite-score histogram for HR hits vs misses across the FULL board
    (not just selected picks). Bins of 5 points from 0-100.
    Returns list of {bin_low, bin_high, hits, misses, hr_rate}.

    hr_rate = hits / (hits + misses) — the actual HR conversion at that
    composite range. The line overlay built from this on the dashboard is
    what reveals the model's signal (climbs from ~3% to ~45% across bins),
    which the absolute-count stacked bar hides.
    """
    rows = conn.execute("""
        SELECT
            CAST(p.composite / 5 AS INTEGER) * 5 AS bin_low,
            SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) AS hits,
            SUM(CASE WHEN COALESCE(o.hr_count, 0) = 0 AND o.batter_id IS NOT NULL THEN 1 ELSE 0 END) AS misses
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.composite IS NOT NULL
        GROUP BY bin_low
        ORDER BY bin_low
    """).fetchall()
    out = []
    for r in rows:
        hits = r["hits"] or 0
        misses = r["misses"] or 0
        total = hits + misses
        out.append({
            "bin_low": r["bin_low"],
            "bin_high": r["bin_low"] + 5,
            "hits": hits,
            "misses": misses,
            "hr_rate": round(hits / total * 100, 1) if total > 0 else 0,
        })
    return out


def _factor_diagnostic(conn, days: int = 30) -> dict:
    """
    For each factor, compute the mean score among batters who hit a HR vs
    those who didn't, over the last N days. Reveals which factors are
    actually surfacing HR hitters and which are flat.

    A factor with hr_mean - miss_mean ~= 0 is providing no signal; one
    with a 15+ point gap is doing real work. Used by the dashboard to
    show "where the model lifts HR hitters" so we can tune what's broken.
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT
            CASE WHEN o.hr_count > 0 THEN 'hr' ELSE 'miss' END AS group_label,
            AVG(p.power_score)   AS power,
            AVG(p.matchup_score) AS matchup,
            AVG(p.park_score)    AS park,
            AVG(p.form_score)    AS form,
            AVG(p.weather_score) AS weather,
            AVG(p.lineup_score)  AS lineup,
            AVG(p.composite)     AS composite,
            COUNT(*)             AS n
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.date >= ?
        GROUP BY group_label
    """, (cutoff,)).fetchall()

    by_group = {r["group_label"]: dict(r) for r in rows}
    hr = by_group.get("hr", {})
    miss = by_group.get("miss", {})

    factors = []
    for f in ("power", "matchup", "park", "form", "weather", "lineup", "composite"):
        h = hr.get(f)
        m = miss.get(f)
        factors.append({
            "factor": f,
            "hr_mean":   round(h, 1) if h is not None else None,
            "miss_mean": round(m, 1) if m is not None else None,
            "gap":       round((h or 0) - (m or 0), 1) if (h is not None and m is not None) else None,
        })

    return {
        "days": days,
        "n_hr": (hr.get("n") or 0),
        "n_miss": (miss.get("n") or 0),
        "factors": factors,
    }


def _factor_decomp(conn, days: int = 30,
                   composite_low: float = None, composite_high: float = None,
                   rank_low: int = None, rank_high: int = None,
                   hr_only: bool = False) -> dict:
    """
    Decompose each factor into its underlying raw inputs and compute the
    mean for HR-hitter rows vs miss rows. Reads from pick_inputs (populated
    by load_picks_to_db). Returns a dict keyed by factor with a list of
    {input, hr_mean, miss_mean, n_hr, n_miss, gap}.

    Filtering knobs (all optional, can be combined):
      - composite_low / composite_high: filter on p.composite (inclusive low,
        exclusive high). Used by the by-band and HR-split decomps.
      - rank_low / rank_high: filter on p.rank_in_board (inclusive low,
        inclusive high — ranks are integer positions). Lets the dashboard
        ask "what do rank 11-30 picks look like?" — controls for slate-
        to-slate variance in absolute composite.
      - hr_only: restrict to o.hr_count > 0. Used to compare HR hitters
        across cohorts.

    Empty for any input where pick_inputs has no data yet (historical days
    pre-date the table). The dashboard's factor decomp section reads this
    to render mini-charts per factor.
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    # Confirm pick_inputs exists (it will after create_tables is called)
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pick_inputs'"
    ).fetchone()
    if not has_table:
        return {"days": days, "factors": {}, "n_hr": 0, "n_miss": 0}

    # Define the inputs grouped by factor
    # 2026-05-03: vegas_implied_total renamed to vegas_team_total_pct.
    factor_inputs = {
        "power":   ["barrel_pct", "exit_velo", "hr_fb_pct", "iso", "xwoba_contact", "pull_fb_pct"],
        "form":    ["recent_hr_14d", "recent_barrel_pct_14d", "ev_trend_14d"],
        "matchup": ["pitcher_hr_per_9", "pitcher_era", "pitcher_hh_pct", "pitcher_k_per_9",
                    "pitcher_fb_pct_allowed", "woba_vs_hand", "archetype_similarity",
                    "vegas_team_total_pct", "platoon_advantage"],
        "park":    ["hr_park_factor"],
        "weather": ["temperature_f", "wind_mph", "humidity_pct", "is_dome"],
        "lineup":  ["batting_order"],
    }

    out = {"days": days, "n_hr": 0, "n_miss": 0, "factors": {}}

    for factor, inputs in factor_inputs.items():
        # Build SELECT clause: AVG(col) for each input, plus group flag
        avg_cols = ", ".join(
            f"AVG(CASE WHEN o.hr_count > 0 THEN i.{c} END) AS {c}_hr, "
            f"AVG(CASE WHEN COALESCE(o.hr_count,0) = 0 THEN i.{c} END) AS {c}_miss, "
            f"COUNT(CASE WHEN o.hr_count > 0 AND i.{c} IS NOT NULL THEN 1 END) AS {c}_n_hr, "
            f"COUNT(CASE WHEN COALESCE(o.hr_count,0) = 0 AND i.{c} IS NOT NULL THEN 1 END) AS {c}_n_miss"
            for c in inputs
        )
        # Build extra filters from band + rank + hr_only flags so the same
        # query body can serve overall, by-composite-band, by-rank-band, and
        # HR-split decomps.
        extra_where = ""
        params = [cutoff]
        if composite_low is not None:
            extra_where += " AND p.composite >= ?"
            params.append(composite_low)
        if composite_high is not None:
            extra_where += " AND p.composite < ?"
            params.append(composite_high)
        if rank_low is not None:
            extra_where += " AND p.rank_in_board >= ?"
            params.append(rank_low)
        if rank_high is not None:
            extra_where += " AND p.rank_in_board <= ?"
            params.append(rank_high)
        if hr_only:
            extra_where += " AND o.hr_count > 0"
        sql = f"""
            SELECT {avg_cols}
            FROM pick_inputs i
            JOIN outcomes o ON i.date = o.date AND i.batter_id = o.batter_id
            JOIN daily_picks p ON p.date = i.date AND p.batter_id = i.batter_id
            WHERE i.date >= ?
            {extra_where}
        """
        try:
            row = conn.execute(sql, params).fetchone()
        except Exception as e:
            row = None

        rows_out = []
        if row is not None:
            for c in inputs:
                hr_mean = row[f"{c}_hr"]
                miss_mean = row[f"{c}_miss"]
                n_hr = row[f"{c}_n_hr"] or 0
                n_miss = row[f"{c}_n_miss"] or 0
                rows_out.append({
                    "input": c,
                    "hr_mean":   round(hr_mean, 3) if hr_mean is not None else None,
                    "miss_mean": round(miss_mean, 3) if miss_mean is not None else None,
                    "gap":       round((hr_mean or 0) - (miss_mean or 0), 3)
                                  if (hr_mean is not None and miss_mean is not None) else None,
                    "n_hr":      n_hr,
                    "n_miss":    n_miss,
                })

        out["factors"][factor] = rows_out

    # Overall n_hr / n_miss (any non-null row counts as a sample)
    extra_where = ""
    params = [cutoff]
    if composite_low is not None:
        extra_where += " AND p.composite >= ?"
        params.append(composite_low)
    if composite_high is not None:
        extra_where += " AND p.composite < ?"
        params.append(composite_high)
    if rank_low is not None:
        extra_where += " AND p.rank_in_board >= ?"
        params.append(rank_low)
    if rank_high is not None:
        extra_where += " AND p.rank_in_board <= ?"
        params.append(rank_high)
    if hr_only:
        extra_where += " AND o.hr_count > 0"
    counts = conn.execute(f"""
        SELECT
            SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) AS n_hr,
            SUM(CASE WHEN COALESCE(o.hr_count,0) = 0 THEN 1 ELSE 0 END) AS n_miss
        FROM pick_inputs i
        JOIN outcomes o ON i.date = o.date AND i.batter_id = o.batter_id
        JOIN daily_picks p ON p.date = i.date AND p.batter_id = i.batter_id
        WHERE i.date >= ?
        {extra_where}
    """, params).fetchone()
    out["n_hr"] = counts["n_hr"] or 0
    out["n_miss"] = counts["n_miss"] or 0

    # Provenance: how many rows came from live runs vs the historical backfill?
    # Helps the dashboard caveat that backfilled rows use current-season-to-date
    # stats as a proxy for as-of-date stats (small forward-looking bias).
    try:
        prov = conn.execute("""
            SELECT COALESCE(source, 'live') AS src, COUNT(*) AS n
            FROM pick_inputs i
            WHERE i.date >= ?
            GROUP BY src
        """, (cutoff,)).fetchall()
        out["provenance"] = {r["src"]: r["n"] for r in prov}
    except Exception:
        out["provenance"] = {}

    return out





def _factor_decomp_by_band(conn, days: int = 30) -> dict:
    """
    Same per-input HR-vs-miss decomposition as _factor_decomp, but split by
    composite-score band. Lets the dashboard show "within composite 40-60,
    here's what HR hitters look like vs misses" — surfacing inputs that are
    flat overall but have signal inside a specific band.
    """
    bands = [(0, 40), (40, 60), (60, 80), (80, 101)]
    out = []
    for low, high in bands:
        d = _factor_decomp(conn, days=days, composite_low=low, composite_high=high)
        d["band_low"] = low
        d["band_high"] = high
        d["label"] = f"{low}-{high if high < 101 else '+'}"
        out.append(d)
    return {"days": days, "bands": out}


def _factor_decomp_hr_split(conn, days: int = 30) -> dict:
    """
    HR hitters only — compare two composite bands (low and high). Reveals
    what raw inputs differentiate HR hitters who scored mid (40-60) from
    HR hitters who scored well (60-80). The factors with the largest gap
    here are signals the model is failing to convert into score for the
    low-band group, which is exactly where weight could be added.
    """
    low_band = _factor_decomp(conn, days=days, composite_low=40, composite_high=60, hr_only=True)
    high_band = _factor_decomp(conn, days=days, composite_low=60, composite_high=80, hr_only=True)
    return {
        "days": days,
        "low":  {"label": "HR hitters scored 40-60", "low": 40,  "high": 60,  "n": low_band.get("n_hr", 0),  "factors": low_band.get("factors", {})},
        "high": {"label": "HR hitters scored 60-80", "low": 60,  "high": 80,  "n": high_band.get("n_hr", 0), "factors": high_band.get("factors", {})},
    }


def _factor_decomp_by_rank_band(conn, days: int = 30) -> dict:
    """
    Same per-input HR-vs-miss decomposition as _factor_decomp, but split by
    DAILY RANK band. Rank controls for slate-to-slate variance: composite 60
    on a strong-pitching slate is a different population than composite 60
    on a weak-pitching slate, but rank #5 is always rank #5.

    Bands chosen to mirror how we use the model:
      - 1-10  → "the picks" (top 8 + just-missed)
      - 11-30 → "fringe"     (the cohort we want to tune toward)
      - 31-100 → "mid-board" (signal still possible)
      - 101-300 → "deep board" (mostly noise)
    """
    bands = [(1, 10), (11, 30), (31, 100), (101, 300)]
    out = []
    for low, high in bands:
        d = _factor_decomp(conn, days=days, rank_low=low, rank_high=high)
        d["rank_low"] = low
        d["rank_high"] = high
        d["label"] = f"#{low}-{high}"
        out.append(d)
    return {"days": days, "bands": out}


def _factor_decomp_hr_split_by_rank(conn, days: int = 30) -> dict:
    """
    HR hitters only — compare top-of-board (rank 1-10) vs fringe (rank
    11-30). Surfaces what raw inputs differentiate HR hitters we picked
    from HR hitters we *almost* picked. Factors with the largest gap are
    where the model is under-weighting signal for the fringe cohort —
    the most actionable tuning lever, since these are the closest to
    breaking into the top-8 selection.
    """
    top   = _factor_decomp(conn, days=days, rank_low=1,  rank_high=10, hr_only=True)
    fringe = _factor_decomp(conn, days=days, rank_low=11, rank_high=30, hr_only=True)
    deep   = _factor_decomp(conn, days=days, rank_low=31, rank_high=100, hr_only=True)
    return {
        "days": days,
        "top":    {"label": "HR hitters ranked #1-10",   "low": 1,  "high": 10,  "n": top.get("n_hr", 0),    "factors": top.get("factors", {})},
        "fringe": {"label": "HR hitters ranked #11-30",  "low": 11, "high": 30,  "n": fringe.get("n_hr", 0), "factors": fringe.get("factors", {})},
        "deep":   {"label": "HR hitters ranked #31-100", "low": 31, "high": 100, "n": deep.get("n_hr", 0),   "factors": deep.get("factors", {})},
    }


# ---------------------------------------------------------------------------
# Input-level diagnostics — 2026-04-30 batch
# These four functions surface what the score functions are rewarding vs
# what's actually happening in the empirical HR rates. Used to diagnose
# whether mismatches (flat scores against climbing HR rates, or vice versa)
# are score-curve issues, weight issues, or genuine no-signal inputs.
# ---------------------------------------------------------------------------

# Maps each pick_inputs raw column to its parent factor sub-score column.
# Lets _input_calibration know which sub-factor score to average per bin.
INPUT_TO_FACTOR = {
    "barrel_pct":              "power",
    "exit_velo":               "power",
    "hr_fb_pct":               "power",
    "iso":                     "power",
    "xwoba_contact":           "power",
    "pull_fb_pct":             "power",
    "recent_hr_14d":           "form",
    "recent_barrel_pct_14d":   "form",
    "ev_trend_14d":            "form",
    "pitcher_hr_per_9":        "matchup",
    "pitcher_era":             "matchup",
    "pitcher_hh_pct":          "matchup",
    "pitcher_k_per_9":         "matchup",
    "pitcher_fb_pct_allowed":  "matchup",
    "woba_vs_hand":            "matchup",
    "archetype_similarity":    "matchup",
    "vegas_team_total_pct":    "matchup",
    "hr_park_factor":          "park",
    "temperature_f":           "weather",
    "wind_mph":                "weather",
    "humidity_pct":            "weather",
}


def _input_calibration(conn, days: int = 60) -> dict:
    """
    For each continuous raw input, bin into 5 quantile bins and compute
    empirical HR rate + average sub-factor score per bin. The mismatch
    between the empirical curve (what should be rewarded) and the score
    curve (what we ARE rewarding) is the tuning signal.

    Mismatch types this surfaces:
      - Score flat where HR rate climbs → score function too compressed
        at the top end (likely culprit for hr_park_factor — top parks not
        getting steep enough boost)
      - Score climbs where HR rate flat → score rewarding noise
      - Curves move opposite directions → score function backwards
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pick_inputs'"
    ).fetchone()
    if not has_table:
        return {"days": days, "inputs": {}}

    # Per-input row filter. archetype_similarity is only consumed by v2
    # matchup scoring; mixing v1 rows (which set matchup_score without using
    # archetype) dilutes the diagnostic into noise. Add v2-only filter for it.
    PER_INPUT_FILTER = {
        "archetype_similarity": "AND COALESCE(p.matchup_version, 'v1') = 'v2'",
    }

    out = {"days": days, "inputs": {}}
    for input_col, factor in INPUT_TO_FACTOR.items():
        score_col = f"{factor}_score"
        extra_filter = PER_INPUT_FILTER.get(input_col, "")
        try:
            rows = conn.execute(f"""
                WITH binned AS (
                    SELECT
                        i.{input_col} AS val,
                        o.hr_count,
                        p.{score_col} AS factor_score,
                        NTILE(5) OVER (ORDER BY i.{input_col}) AS bin
                    FROM pick_inputs i
                    JOIN outcomes o ON i.date = o.date AND i.batter_id = o.batter_id
                    JOIN daily_picks p ON p.date = i.date AND p.batter_id = i.batter_id
                    WHERE i.date >= ? AND i.{input_col} IS NOT NULL
                    {extra_filter}
                )
                SELECT bin, MIN(val) AS bin_low, MAX(val) AS bin_high,
                       COUNT(*) AS n,
                       SUM(CASE WHEN hr_count > 0 THEN 1 ELSE 0 END) AS hits,
                       AVG(factor_score) AS avg_score
                FROM binned
                GROUP BY bin
                ORDER BY bin
            """, (cutoff,)).fetchall()
        except Exception:
            continue

        if not rows or all((r["n"] or 0) == 0 for r in rows):
            continue

        bins_out = []
        for r in rows:
            n = r["n"] or 0
            hits = r["hits"] or 0
            bins_out.append({
                "bin":      r["bin"],
                "bin_low":  round(r["bin_low"], 3) if r["bin_low"]  is not None else None,
                "bin_high": round(r["bin_high"], 3) if r["bin_high"] is not None else None,
                "n":        n,
                "hits":     hits,
                "hr_rate":  round(hits / n * 100, 1) if n else 0,
                "avg_score": round(r["avg_score"], 1) if r["avg_score"] is not None else None,
            })
        # Status flag: classify the input's empirical signal AND the model's
        # response to it independently. This is more diagnostic than just
        # comparing two trends — a "BACKWARDS" flag previously fired when
        # archetype_similarity climbed but matchup_score dropped, but that
        # turned out to be the elite-pitcher dampening dragging the AGGREGATE
        # factor score down, not a sign bug in the archetype scoring itself.
        #
        # New status taxonomy:
        #   ALIGNED          — empirical climbs, score climbs (model captures it)
        #   SIGNAL_NOT_CAPTURED — empirical climbs, score flat or drops
        #                        (model misses real signal; tuning lever)
        #   OVER_WEIGHTED    — empirical flat, score climbs
        #                        (model rewarding noise; weight reduction lever)
        #   NO_SIGNAL        — empirical flat, score flat (input is dead weight)
        #   NOISY            — short series or insufficient n per bin to classify
        #
        # Note: avg_score is the FACTOR-aggregate score (e.g. matchup_score),
        # not isolated input contribution. Correlated inputs in the same factor
        # can drag the aggregate. Treat SIGNAL_NOT_CAPTURED as "investigate"
        # not "definite bug" — it could be aggregate drag from a co-factor.
        def _trend(values, threshold):
            """+1 monotone-ish up, -1 down, 0 flat. Threshold is absolute swing."""
            if not values:
                return 0
            swing = max(values) - min(values)
            if swing < threshold:
                return 0
            up = sum(1 for j in range(len(values)-1) if values[j+1] > values[j])
            dn = sum(1 for j in range(len(values)-1) if values[j+1] < values[j])
            if up >= dn + 2:
                return 1
            if dn >= up + 2:
                return -1
            return 0

        valid = [b for b in bins_out if b["avg_score"] is not None]
        status = "noisy"
        empirical_trend = 0
        score_trend = 0
        if len(valid) >= 4 and all(b["n"] >= 20 for b in valid):
            hr_rates = [b["hr_rate"]  for b in valid]
            scores   = [b["avg_score"] for b in valid]
            empirical_trend = _trend(hr_rates, threshold=2.0)   # 2pp HR-rate swing
            score_trend     = _trend(scores,   threshold=5.0)   # 5pt score swing
            if empirical_trend > 0 and score_trend > 0:
                status = "aligned"
            elif empirical_trend > 0 and score_trend <= 0:
                status = "signal_not_captured"
            elif empirical_trend == 0 and score_trend > 0:
                status = "over_weighted"
            elif empirical_trend == 0 and score_trend == 0:
                status = "no_signal"
            elif empirical_trend < 0 and score_trend > 0:
                status = "over_weighted"  # input goes down with HR rate, score goes up — bad
            elif empirical_trend < 0 and score_trend < 0:
                status = "aligned"  # both decreasing (e.g., pitcher k_per_9: high K → low HR)
            else:
                status = "partial"

        out["inputs"][input_col] = {
            "factor":          factor,
            "bins":            bins_out,
            "status":          status,
            "empirical_trend": empirical_trend,  # +1 up / 0 flat / -1 down
            "score_trend":     score_trend,
            # Backward compat: keep the old field name so existing dashboard JS
            # doesn't break before the renderFunc update lands.
            "mismatch":        status,
        }

    return out


def _dome_vs_outdoor(conn, days: int = 60) -> dict:
    """
    HR rate split by dome status × park-factor band. Tests whether the
    model's preference for dome games is justified by HR yield or whether
    it's a quirk of the weather scoring (dome → fixed 50, neutral, never
    risks a negative wind alignment).
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT
            COALESCE(i.is_dome, 0) AS is_dome,
            CASE
                WHEN i.hr_park_factor IS NULL THEN 'unknown'
                WHEN i.hr_park_factor < 95  THEN '<95 (pitcher)'
                WHEN i.hr_park_factor < 100 THEN '95-100 (mild pitcher)'
                WHEN i.hr_park_factor < 105 THEN '100-105 (mild hitter)'
                WHEN i.hr_park_factor < 110 THEN '105-110 (hitter)'
                ELSE                              '110+ (strong hitter)'
            END AS pf_band,
            COUNT(*) AS n,
            SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) AS hits,
            AVG(p.weather_score) AS avg_weather_score,
            SUM(CASE WHEN p.selected = 1 THEN 1 ELSE 0 END) AS selected_n
        FROM pick_inputs i
        JOIN outcomes o ON i.date = o.date AND i.batter_id = o.batter_id
        JOIN daily_picks p ON p.date = i.date AND p.batter_id = i.batter_id
        WHERE i.date >= ?
        GROUP BY is_dome, pf_band
        ORDER BY is_dome DESC, pf_band
    """, (cutoff,)).fetchall()

    cells = []
    for r in rows:
        n = r["n"] or 0
        hits = r["hits"] or 0
        cells.append({
            "is_dome":  bool(r["is_dome"]),
            "pf_band":  r["pf_band"],
            "n":        n,
            "hits":     hits,
            "hr_rate":  round(hits / n * 100, 1) if n else 0,
            "avg_weather_score": round(r["avg_weather_score"], 1) if r["avg_weather_score"] is not None else None,
            "selected_picks": r["selected_n"] or 0,
        })

    summary = conn.execute("""
        SELECT
            SUM(CASE WHEN COALESCE(i.is_dome,0) = 1 THEN 1 ELSE 0 END) AS dome_picks,
            SUM(CASE WHEN COALESCE(i.is_dome,0) = 0 THEN 1 ELSE 0 END) AS outdoor_picks,
            SUM(CASE WHEN COALESCE(i.is_dome,0) = 1 AND o.hr_count > 0 THEN 1 ELSE 0 END) AS dome_hits,
            SUM(CASE WHEN COALESCE(i.is_dome,0) = 0 AND o.hr_count > 0 THEN 1 ELSE 0 END) AS outdoor_hits
        FROM pick_inputs i
        JOIN outcomes o ON i.date = o.date AND i.batter_id = o.batter_id
        JOIN daily_picks p ON p.date = i.date AND p.batter_id = i.batter_id
        WHERE i.date >= ? AND p.selected = 1
    """, (cutoff,)).fetchone()

    dome_n    = summary["dome_picks"]    or 0
    outdoor_n = summary["outdoor_picks"] or 0
    return {
        "days": days,
        "summary": {
            "dome_picks":      dome_n,
            "outdoor_picks":   outdoor_n,
            "dome_share":      round(dome_n / max(dome_n + outdoor_n, 1) * 100, 1),
            "dome_hr_rate":    round((summary["dome_hits"]    or 0) / max(dome_n, 1)    * 100, 1),
            "outdoor_hr_rate": round((summary["outdoor_hits"] or 0) / max(outdoor_n, 1) * 100, 1),
        },
        "cells": cells,
    }


def _pick_composition(conn, days: int = 60) -> dict:
    """
    Aggregate properties of selected picks. Surfaces systematic biases
    that are invisible per-pick — e.g., 80% dome share, never-pick from
    sub-95 park-factor venues, leadoff hitters absent.

    Joins live tables (daily_slate, park_factors) directly rather than
    relying on pick_inputs columns. The pick_inputs table had ~92% nulls
    for park_factor / batting_order / is_dome on backfilled rows because
    backfill_pick_inputs.py couldn't always join daily_slate on game_pk.
    Joining at query time uses whatever data exists in any source table.
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    # Determine current season for park_factors lookup
    season = datetime.now().year

    # Joined base: daily_picks → daily_slate (for venue + dome) → park_factors (for HR factor).
    # batting_order comes directly from daily_picks (string column, parsed numerically here).
    base_where = """
        FROM daily_picks p
        LEFT JOIN daily_slate s ON s.game_pk = p.game_pk AND s.date = p.date
        LEFT JOIN park_factors pf ON pf.venue = s.venue AND pf.season = ?
        WHERE p.date >= ? AND p.selected = 1
    """
    args = (season, cutoff)

    pf_rows = conn.execute(f"""
        SELECT
            CASE
                WHEN pf.hr_pf_overall IS NULL THEN 'unknown'
                WHEN pf.hr_pf_overall < 95  THEN '<95'
                WHEN pf.hr_pf_overall < 100 THEN '95-100'
                WHEN pf.hr_pf_overall < 105 THEN '100-105'
                WHEN pf.hr_pf_overall < 110 THEN '105-110'
                ELSE                              '110+'
            END AS band,
            COUNT(*) AS n
        {base_where}
        GROUP BY band
    """, args).fetchall()

    bo_rows = conn.execute(f"""
        SELECT
            CASE
                WHEN p.batting_order IS NULL THEN 'unknown'
                WHEN CAST(p.batting_order AS INTEGER) BETWEEN 1 AND 3 THEN '1-3 (top)'
                WHEN CAST(p.batting_order AS INTEGER) BETWEEN 4 AND 6 THEN '4-6 (middle)'
                WHEN CAST(p.batting_order AS INTEGER) BETWEEN 7 AND 9 THEN '7-9 (bottom)'
                ELSE                                                       'other'
            END AS band,
            COUNT(*) AS n
        {base_where}
        GROUP BY band
    """, args).fetchall()

    dome_rows = conn.execute(f"""
        SELECT COALESCE(s.dome, 0) AS is_dome, COUNT(*) AS n
        {base_where}
        GROUP BY is_dome
    """, args).fetchall()

    total = conn.execute(f"SELECT COUNT(*) AS n {base_where}", args).fetchone()
    total_n = total["n"] or 0

    def to_pct(rows, key="band"):
        out = []
        for r in rows:
            n = r["n"] or 0
            out.append({
                "label": r[key],
                "n":     n,
                "pct":   round(n / max(total_n, 1) * 100, 1),
            })
        return out

    dome_dist = []
    for r in dome_rows:
        n = r["n"] or 0
        dome_dist.append({
            "label": "Dome" if r["is_dome"] else "Outdoor",
            "n":     n,
            "pct":   round(n / max(total_n, 1) * 100, 1),
        })

    return {
        "days":          days,
        "total_picks":   total_n,
        "park_factor":   to_pct(pf_rows),
        "batting_order": to_pct(bo_rows),
        "dome":          dome_dist,
    }


def _temp_humidity_heatmap(conn, days: int = 60) -> dict:
    """
    2D heatmap: temperature_band × humidity_band → HR rate. Tests whether
    humidity has an interaction effect with temperature (e.g., hot+humid
    air is less dense than hot+dry, so balls travel further; cold+humid
    is the worst combo).

    Compares each cell's HR rate against the additive-baseline prediction
    (avg by temp_band × avg by humidity_band normalized to overall rate).
    A cell with positive interaction (cell_rate > additive prediction)
    is where the score function should reward the combo extra; negative
    interaction means we should penalize the combo.

    Outdoor games only (humidity is meaningless in domes; dome rows would
    pollute the cells with their fixed 50 weather score).
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT
            CASE
                WHEN i.temperature_f IS NULL THEN 'unknown'
                WHEN i.temperature_f < 60 THEN 'cold (<60)'
                WHEN i.temperature_f < 70 THEN 'cool (60-70)'
                WHEN i.temperature_f < 80 THEN 'warm (70-80)'
                ELSE                          'hot (80+)'
            END AS temp_band,
            CASE
                WHEN i.humidity_pct IS NULL THEN 'unknown'
                WHEN i.humidity_pct < 40 THEN 'dry (<40)'
                WHEN i.humidity_pct < 60 THEN 'mild (40-60)'
                WHEN i.humidity_pct < 80 THEN 'humid (60-80)'
                ELSE                          'very humid (80+)'
            END AS humid_band,
            COUNT(*) AS n,
            SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) AS hits
        FROM pick_inputs i
        JOIN outcomes o ON i.date = o.date AND i.batter_id = o.batter_id
        WHERE i.date >= ?
          AND COALESCE(i.is_dome, 0) = 0
          AND i.temperature_f IS NOT NULL
          AND i.humidity_pct IS NOT NULL
        GROUP BY temp_band, humid_band
    """, (cutoff,)).fetchall()

    temp_order  = ["cold (<60)", "cool (60-70)", "warm (70-80)", "hot (80+)"]
    humid_order = ["dry (<40)", "mild (40-60)", "humid (60-80)", "very humid (80+)"]

    cells = []
    by_cell = {}
    by_temp = {}
    by_humid = {}
    total_n = 0
    total_hits = 0
    for r in rows:
        if r["temp_band"] == "unknown" or r["humid_band"] == "unknown":
            continue
        n = r["n"] or 0
        hits = r["hits"] or 0
        cell_key = (r["temp_band"], r["humid_band"])
        by_cell[cell_key] = (n, hits)
        by_temp[r["temp_band"]]   = (by_temp.get(r["temp_band"], (0,0))[0]   + n, by_temp.get(r["temp_band"], (0,0))[1]   + hits)
        by_humid[r["humid_band"]] = (by_humid.get(r["humid_band"], (0,0))[0] + n, by_humid.get(r["humid_band"], (0,0))[1] + hits)
        total_n    += n
        total_hits += hits

    if total_n == 0:
        return {"days": days, "cells": [], "n_total": 0}

    overall_rate = total_hits / total_n

    # Build cells with interaction delta vs additive baseline.
    # Additive baseline for cell (T, H) = (rate_T / overall) × (rate_H / overall) × overall
    # i.e., baseline_rate(T, H) = rate_T × rate_H / overall_rate.
    for t in temp_order:
        if t not in by_temp:
            continue
        rate_t = by_temp[t][1] / max(by_temp[t][0], 1)
        for h in humid_order:
            if h not in by_humid:
                continue
            rate_h = by_humid[h][1] / max(by_humid[h][0], 1)
            cell = by_cell.get((t, h))
            if cell is None:
                cells.append({
                    "temp_band":  t, "humid_band": h,
                    "n":          0, "hits": 0,
                    "hr_rate":    None,
                    "additive_baseline": None,
                    "interaction": None,
                })
                continue
            n, hits = cell
            rate = hits / n if n else 0
            # Multiplicative interaction model: baseline = rate_T × rate_H / overall_rate
            baseline = (rate_t * rate_h / overall_rate) if overall_rate > 0 else 0
            interaction = rate - baseline
            cells.append({
                "temp_band":  t, "humid_band": h,
                "n":          n, "hits": hits,
                "hr_rate":    round(rate * 100, 1),
                "additive_baseline":  round(baseline * 100, 1),
                "interaction":        round(interaction * 100, 1),
            })

    return {
        "days":              days,
        "n_total":           total_n,
        "overall_hr_rate":   round(overall_rate * 100, 1),
        "temp_bands":        temp_order,
        "humid_bands":       humid_order,
        "cells":             cells,
        "by_temp":           {t: {"n": v[0], "hr_rate": round(v[1]/max(v[0],1)*100, 1)} for t, v in by_temp.items()},
        "by_humid":          {h: {"n": v[0], "hr_rate": round(v[1]/max(v[0],1)*100, 1)} for h, v in by_humid.items()},
        "note":              "Cells with positive interaction (cell rate > additive baseline) suggest "
                             "synergy between temp and humidity; negative = the combo suppresses HRs more "
                             "than either factor alone would predict. Outdoor games only.",
    }


def _archetype_dampening_diagnostic(conn, days: int = 60) -> dict:
    """
    2D heatmap: pitcher_vulnerability_quintile × archetype_similarity_quintile
    → HR rate + avg matchup_score.

    Tests the hypothesis that the elite-pitcher dampening (raw *= 0.7 when
    vulnerability < 25; raw *= 0.85 when vulnerability < 40) is over-aggressive
    for the (low-vulnerability × high-similarity) cell — i.e., we're heavily
    penalizing matchups where the pitcher is elite BUT exactly the archetype
    this hitter feasts on.

    If the bottom-left cell (elite pitcher × high archetype similarity) shows
    a HIGH empirical HR rate but a LOW matchup score, the dampening should
    be conditional on similarity, not just vulnerability.

    NTILE the columns into 5 bins each → 25 cells. Pitcher vulnerability is
    proxied by pitcher_hr_per_9 (lower = more elite); archetype_similarity
    is direct.
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    try:
        # Filter to v2-scored rows only. v1 matchup_score doesn't use archetype
        # at all, so mixing v1 and v2 rows dilutes the diagnostic. The 2026-04-30
        # readout showed score dropping with similarity within every vuln tier —
        # likely an artifact of v1 rows being averaged in.
        rows = conn.execute("""
            WITH binned AS (
                SELECT
                    NTILE(5) OVER (ORDER BY i.pitcher_hr_per_9)     AS vuln_bin,
                    NTILE(5) OVER (ORDER BY i.archetype_similarity) AS sim_bin,
                    i.pitcher_hr_per_9,
                    i.archetype_similarity,
                    o.hr_count,
                    p.matchup_score
                FROM pick_inputs i
                JOIN outcomes o ON i.date = o.date AND i.batter_id = o.batter_id
                JOIN daily_picks p ON p.date = i.date AND p.batter_id = i.batter_id
                WHERE i.date >= ?
                  AND i.pitcher_hr_per_9 IS NOT NULL
                  AND i.archetype_similarity IS NOT NULL
                  AND COALESCE(p.matchup_version, 'v1') = 'v2'
            )
            SELECT
                vuln_bin, sim_bin,
                COUNT(*) AS n,
                SUM(CASE WHEN hr_count > 0 THEN 1 ELSE 0 END) AS hits,
                AVG(matchup_score) AS avg_matchup,
                MIN(pitcher_hr_per_9)     AS vuln_low,
                MAX(pitcher_hr_per_9)     AS vuln_high,
                MIN(archetype_similarity) AS sim_low,
                MAX(archetype_similarity) AS sim_high
            FROM binned
            GROUP BY vuln_bin, sim_bin
            ORDER BY vuln_bin, sim_bin
        """, (cutoff,)).fetchall()
    except Exception:
        rows = []

    cells = []
    for r in rows:
        n = r["n"] or 0
        hits = r["hits"] or 0
        cells.append({
            "vuln_bin": r["vuln_bin"],   # 1=most elite (low HR/9), 5=most vulnerable
            "sim_bin":  r["sim_bin"],    # 1=lowest archetype match, 5=highest
            "n":        n,
            "hits":     hits,
            "hr_rate":  round(hits / n * 100, 1) if n else 0,
            "avg_matchup_score": round(r["avg_matchup"], 1) if r["avg_matchup"] is not None else None,
            "vuln_range": f"{round(r['vuln_low'], 2)}–{round(r['vuln_high'], 2)}" if r["vuln_low"] is not None else "",
            "sim_range":  f"{round(r['sim_low'], 1)}–{round(r['sim_high'], 1)}"   if r["sim_low"]  is not None else "",
        })

    # Surface the most diagnostic cell: elite pitcher (vuln_bin=1) × high similarity (sim_bin=5).
    # If empirical HR rate is materially higher than score implies, dampening is over-aggressive.
    target = next((c for c in cells if c["vuln_bin"] == 1 and c["sim_bin"] == 5), None)
    diagnosis = None
    if target and target["n"] >= 15 and target["avg_matchup_score"] is not None:
        # Compare to overall HR rate to know whether this cell over-/under-converts
        all_n = sum(c["n"] for c in cells)
        all_hits = sum(c["hits"] for c in cells)
        overall_rate = all_hits / max(all_n, 1) * 100
        cell_lift = target["hr_rate"] - overall_rate
        # If cell HR rate is meaningfully higher than overall (5+ pts) AND avg_matchup_score
        # is below median, we're under-rewarding this cell (dampening too aggressive).
        all_scores = [c["avg_matchup_score"] for c in cells if c["avg_matchup_score"] is not None]
        median_score = sorted(all_scores)[len(all_scores)//2] if all_scores else 50
        score_gap = target["avg_matchup_score"] - median_score
        if cell_lift > 5 and score_gap < -3:
            diagnosis = "over_dampened"
        elif cell_lift < -5 and score_gap > 3:
            diagnosis = "under_dampened"
        else:
            diagnosis = "calibrated"

    return {
        "days":      days,
        "n_total":   sum(c["n"] for c in cells),
        "cells":     cells,
        "target":    target,
        "diagnosis": diagnosis,
        "note":      "Rows = pitcher vulnerability quintile (1=most elite, 5=most vulnerable). "
                     "Columns = archetype similarity quintile (1=low match, 5=high match). "
                     "Watch the top-right cell (elite pitcher × high archetype match): if HR rate "
                     "is high but avg matchup score is low, the elite-pitcher dampening is too "
                     "aggressive when archetype signals a real vulnerability.",
    }


def _temp_humidity_heatmap_historical(conn) -> dict:
    """
    Same temp×humidity heatmap as _temp_humidity_heatmap, but reads from
    historical_calibration instead of pick_inputs. Provides a vastly larger
    sample (~170k rows for 2 backfilled seasons vs ~150 from this season's
    outdoor pick rows) so the rare bins (HOT × HUMID, etc.) actually fill in.

    Reads HR rate as hr_count / pa_count (per-PA HR rate). pick_inputs uses
    per-batter-game (hr_count > 0 ? 1 : 0); we adjust here to match — using
    "did this batter HR in this game" rather than per-PA, so the rates are
    comparable to the live-season heatmap.
    """
    # Check the table exists and has rows. If not, return empty.
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='historical_calibration'"
    ).fetchone()
    if not has_table:
        return {"available": False, "n_total": 0, "cells": []}

    n_check = conn.execute(
        "SELECT COUNT(*) AS n FROM historical_calibration"
    ).fetchone()
    if (n_check["n"] or 0) == 0:
        return {"available": False, "n_total": 0, "cells": []}

    # Same band logic as live-season heatmap, just different source table.
    rows = conn.execute("""
        SELECT
            CASE
                WHEN temperature_f IS NULL THEN 'unknown'
                WHEN temperature_f < 60 THEN 'cold (<60)'
                WHEN temperature_f < 70 THEN 'cool (60-70)'
                WHEN temperature_f < 80 THEN 'warm (70-80)'
                ELSE                          'hot (80+)'
            END AS temp_band,
            CASE
                WHEN humidity_pct IS NULL THEN 'unknown'
                WHEN humidity_pct < 40 THEN 'dry (<40)'
                WHEN humidity_pct < 60 THEN 'mild (40-60)'
                WHEN humidity_pct < 80 THEN 'humid (60-80)'
                ELSE                          'very humid (80+)'
            END AS humid_band,
            COUNT(*) AS n,
            SUM(CASE WHEN hr_count > 0 THEN 1 ELSE 0 END) AS hits
        FROM historical_calibration
        WHERE COALESCE(dome, 0) = 0
          AND temperature_f IS NOT NULL
          AND humidity_pct IS NOT NULL
        GROUP BY temp_band, humid_band
    """).fetchall()

    temp_order  = ["cold (<60)", "cool (60-70)", "warm (70-80)", "hot (80+)"]
    humid_order = ["dry (<40)", "mild (40-60)", "humid (60-80)", "very humid (80+)"]

    by_cell = {}
    by_temp = {}
    by_humid = {}
    total_n = 0
    total_hits = 0
    for r in rows:
        if r["temp_band"] == "unknown" or r["humid_band"] == "unknown":
            continue
        n = r["n"] or 0
        hits = r["hits"] or 0
        cell_key = (r["temp_band"], r["humid_band"])
        by_cell[cell_key] = (n, hits)
        by_temp[r["temp_band"]]   = (by_temp.get(r["temp_band"], (0,0))[0]   + n, by_temp.get(r["temp_band"], (0,0))[1]   + hits)
        by_humid[r["humid_band"]] = (by_humid.get(r["humid_band"], (0,0))[0] + n, by_humid.get(r["humid_band"], (0,0))[1] + hits)
        total_n    += n
        total_hits += hits

    if total_n == 0:
        return {"available": True, "n_total": 0, "cells": []}

    overall_rate = total_hits / total_n
    cells = []
    for t in temp_order:
        if t not in by_temp:
            continue
        rate_t = by_temp[t][1] / max(by_temp[t][0], 1)
        for h in humid_order:
            if h not in by_humid:
                continue
            rate_h = by_humid[h][1] / max(by_humid[h][0], 1)
            cell = by_cell.get((t, h))
            if cell is None:
                cells.append({
                    "temp_band":  t, "humid_band": h,
                    "n":          0, "hits": 0,
                    "hr_rate":    None,
                    "additive_baseline": None,
                    "interaction": None,
                })
                continue
            n, hits = cell
            rate = hits / n if n else 0
            baseline = (rate_t * rate_h / overall_rate) if overall_rate > 0 else 0
            interaction = rate - baseline
            cells.append({
                "temp_band":  t, "humid_band": h,
                "n":          n, "hits": hits,
                "hr_rate":    round(rate * 100, 1),
                "additive_baseline":  round(baseline * 100, 1),
                "interaction":        round(interaction * 100, 1),
            })

    seasons = sorted({r["season"] for r in conn.execute(
        "SELECT DISTINCT season FROM historical_calibration"
    ).fetchall()})

    return {
        "available":         True,
        "n_total":           total_n,
        "overall_hr_rate":   round(overall_rate * 100, 1),
        "seasons":           seasons,
        "temp_bands":        temp_order,
        "humid_bands":       humid_order,
        "cells":             cells,
        "by_temp":           {t: {"n": v[0], "hr_rate": round(v[1]/max(v[0],1)*100, 1)} for t, v in by_temp.items()},
        "by_humid":          {h: {"n": v[0], "hr_rate": round(v[1]/max(v[0],1)*100, 1)} for h, v in by_humid.items()},
        "note":              "Backfilled from prior seasons (environmental factors only). "
                             "Player rosters and pitcher matchups vary by year, but the physics "
                             "of HR-vs-weather is stable, so this is a valid sample expansion "
                             "for temperature, humidity, wind, dome diagnostics.",
    }


def _wind_direction_diagnostic(conn, days: int = 60) -> dict:
    """
    HR rate by wind-helping band, outdoor games only. Tests whether the
    wind direction effect we already score is producing the HR-rate
    gradient we'd expect (out-blowing wind → higher HR rate).

    Joins pick_inputs → daily_picks → daily_slate to recover venue +
    raw wind direction (pick_inputs only stores wind_mph and direction
    pre-projection). Computes the helping_factor in Python via wind_utils.
    """
    from etl.wind_utils import wind_helping_factor, helping_band, HELPING_BAND_ORDER, HELPING_BAND_LABELS

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    try:
        rows = conn.execute("""
            SELECT
                s.venue,
                s.wind_mph,
                s.wind_dir_deg,
                COALESCE(s.dome, 0) AS dome,
                o.hr_count,
                p.weather_score,
                p.selected
            FROM pick_inputs i
            JOIN daily_picks p ON p.date = i.date AND p.batter_id = i.batter_id
            JOIN daily_slate s ON s.game_pk = p.game_pk AND s.date = p.date
            JOIN outcomes o ON o.date = i.date AND o.batter_id = i.batter_id
            WHERE i.date >= ?
              AND COALESCE(s.dome, 0) = 0
        """, (cutoff,)).fetchall()
    except Exception:
        rows = []

    by_band = {}
    unknown_venues = set()
    for r in rows:
        helping = wind_helping_factor(r["wind_mph"], r["wind_dir_deg"], r["venue"] or "")
        band = helping_band(helping)
        if band == "unknown":
            if r["venue"]:
                unknown_venues.add(r["venue"])
            continue
        d = by_band.setdefault(band, {"n": 0, "hits": 0, "score_sum": 0.0, "score_n": 0, "helping_sum": 0.0, "helping_n": 0})
        d["n"] += 1
        if r["hr_count"] and r["hr_count"] > 0:
            d["hits"] += 1
        if r["weather_score"] is not None:
            d["score_sum"] += r["weather_score"]
            d["score_n"]   += 1
        if helping is not None:
            d["helping_sum"] += helping
            d["helping_n"]   += 1

    bands_out = []
    for b in HELPING_BAND_ORDER:
        d = by_band.get(b)
        if not d or d["n"] == 0:
            continue
        bands_out.append({
            "band":    b,
            "label":   HELPING_BAND_LABELS[b],
            "n":       d["n"],
            "hits":    d["hits"],
            "hr_rate": round(d["hits"] / d["n"] * 100, 1),
            "avg_weather_score": round(d["score_sum"]   / d["score_n"],   1) if d["score_n"]   else None,
            "avg_helping_mph":   round(d["helping_sum"] / d["helping_n"], 1) if d["helping_n"] else None,
        })

    return {
        "days":           days,
        "n_outdoor":      sum(b["n"] for b in bands_out),
        "bands":          bands_out,
        "unknown_venues": sorted(unknown_venues),
        "note":           "Outdoor games only. helping_factor = wind_mph × cos(angle_to_CF). "
                          "Score column shown is weather_score (model output). "
                          "If HR rate climbs out_strong→in_strong but score is flat, the wind effect isn't being rewarded enough.",
    }


def export_factor_trends(conn, out_dir: Path, days: int = 30):
    """Export per-factor daily averages for trend charts."""

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT
            p.date,
            ROUND(AVG(p.power_score), 1) AS avg_power,
            ROUND(AVG(p.matchup_score), 1) AS avg_matchup,
            ROUND(AVG(p.park_score), 1) AS avg_park,
            ROUND(AVG(p.form_score), 1) AS avg_form,
            ROUND(AVG(p.weather_score), 1) AS avg_weather,
            ROUND(AVG(p.lineup_score), 1) AS avg_lineup,
            ROUND(AVG(p.composite), 1) AS avg_composite,
            COUNT(*) AS n_picks,
            SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) AS hits
        FROM daily_picks p
        LEFT JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1 AND p.date >= ?
        GROUP BY p.date
        ORDER BY p.date
    """, (cutoff,)).fetchall()

    # Tag each day with its model version for the dashboard's boundary
    # marker. Anything <= 2026-04-15 is the legacy fixed-anchor model
    # (raw_data.csv backfill); 2026-04-16 onward is v2 (percentile rerank
    # + new sub-scores). Helps explain the visible discontinuity in the
    # factor trends chart.
    V2_START = "2026-04-16"
    trends = []
    for r in rows:
        d = dict(r)


def export_hr_leaderboard(conn, out_dir, days=14, top_n=40):
    """
    Cross-tab: top HR hitters in last N days × dates × cell shows our model
    rank if they were on our board that day, else just "X" if they HR'd.

    Output shape:
      {
        "days": [date, date, ...],     # most recent first
        "rows": [
          {
            "batter_name": "Aaron Judge",
            "batter_id": 592450,
            "team": "NYY",
            "total_hr": 7,
            "by_date": {date: {"hr_count": int, "rank": int|null}, ...}
          },
          ...
        ],
      }
    """
    cutoff = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")

    # Top batters by total HRs in window. Group on batter_id only — name
    # variants (diacritics, suffix changes, encoding) caused some hitters to
    # get split into multiple rows with low totals and fall off the cut
    # (e.g., Mike Trout had 3 HRs but was missing from the top-40).
    top = conn.execute("""
        SELECT
            o.batter_id,
            MAX(o.batter_name) AS batter_name,
            COALESCE(MAX(p.team), '') AS team,
            SUM(o.hr_count) AS total_hr
        FROM outcomes o
        LEFT JOIN daily_picks p
            ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE o.date >= ? AND o.hr_count > 0
        GROUP BY o.batter_id
        ORDER BY total_hr DESC, batter_name ASC
        LIMIT ?
    """, (cutoff, top_n)).fetchall()

    if not top:
        atomic_write_json(out_dir / "hr_leaderboard.json", {
            "days": [], "rows": [], "exported_at": datetime.now().isoformat(),
        })
        return

    batter_ids = [r["batter_id"] for r in top]
    placeholders = ",".join("?" * len(batter_ids))

    # Per-(batter, date) detail with rank
    detail = conn.execute(f"""
        SELECT
            o.batter_id,
            o.date,
            o.hr_count,
            p.rank_in_board,
            p.composite,
            p.selected
        FROM outcomes o
        LEFT JOIN daily_picks p
            ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE o.batter_id IN ({placeholders})
          AND o.date >= ? AND o.hr_count > 0
    """, (*batter_ids, cutoff)).fetchall()

    # Distinct dates in window, most recent first
    date_rows = conn.execute("""
        SELECT DISTINCT date FROM outcomes
        WHERE date >= ?
        ORDER BY date DESC
    """, (cutoff,)).fetchall()
    dates = [r["date"] for r in date_rows]

    by_key = {}
    for r in detail:
        by_key[(r["batter_id"], r["date"])] = {
            "hr_count": r["hr_count"],
            "rank": r["rank_in_board"],
            "composite": r["composite"],
            "selected": bool(r["selected"]) if r["selected"] is not None else False,
        }

    rows_out = []
    for t in top:
        bid = t["batter_id"]
        by_date = {}
        for d in dates:
            cell = by_key.get((bid, d))
            if cell:
                by_date[d] = cell
        rows_out.append({
            "batter_id": bid,
            "batter_name": t["batter_name"],
            "team": t["team"],
            "total_hr": t["total_hr"],
            "by_date": by_date,
        })

    atomic_write_json(out_dir / "hr_leaderboard.json", {
        "days": dates,
        "window_days": days,
        "rows": rows_out,
        "exported_at": datetime.now().isoformat(),
    })
    print(f"  Exported hr_leaderboard.json ({len(rows_out)} batters, {len(dates)} days)")



def export_hr_recap(conn, out_dir, days=60):
    """Export per-day HR recap: every batter who hit a HR, joined with our
    model's composite/rank AND each HR's Statcast detail (coordX/Y, launch
    speed, etc.) for the diamond SVG in the Topps card modal.

    Per-HR Statcast comes from the `hr_events` table, populated by
    etl_outcomes.fetch_hr_events_for_date and backfill_hr_events.py
    (PR #5a). Backward-compatible: when a date hasn't been backfilled
    yet, each hitter's `hrs` field is an empty list and the front-end
    gracefully degrades to "no spray data" in the diamond SVG.
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT
            o.date, o.batter_name, o.batter_id, o.hr_count, o.ab,
            COALESCE(o.hits, 0) AS hits,
            COALESCE(o.rbi, 0) AS rbi,
            p.team, p.tier_label, p.opp_pitcher, p.composite,
            p.rank_in_board, p.selected,
            p.power_score, p.matchup_score, p.park_score,
            p.form_score, p.weather_score, p.lineup_score
        FROM outcomes o
        LEFT JOIN daily_picks p
            ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE o.hr_count > 0 AND o.date >= ?
        ORDER BY o.date DESC, o.hr_count DESC, p.rank_in_board ASC
    """, (cutoff,)).fetchall()

    # Per-HR Statcast events keyed by (date, batter_id). Build the index
    # once. A 2-HR batter has 2 entries in this list; the front-end
    # renders a stacked diamond SVG per entry.
    #
    # Defensive: the table may not exist on a fresh checkout that hasn't
    # run create_tables yet. Catch and degrade to empty events so the
    # rest of the export still runs.
    events_by_key: dict[tuple[str, int], list[dict]] = {}
    try:
        hr_event_rows = conn.execute("""
            SELECT date, batter_id,
                   game_pk, at_bat_index, inning, half_inning, play_time,
                   pitcher_name, pitching_team,
                   launch_speed, launch_angle, total_distance,
                   coord_x, coord_y, trajectory, location,
                   home_score_after, away_score_after,
                   description, venue
            FROM hr_events
            WHERE date >= ?
            ORDER BY date DESC, play_time ASC
        """, (cutoff,)).fetchall()
        for ev in hr_event_rows:
            key = (ev["date"], ev["batter_id"])
            events_by_key.setdefault(key, []).append({
                "game_pk":      ev["game_pk"],
                "at_bat_index": ev["at_bat_index"],
                "inning":       ev["inning"],
                "halfInning":   ev["half_inning"],
                "time":         ev["play_time"],
                "pitcherName":  ev["pitcher_name"],
                "pitchingTeam": ev["pitching_team"],
                "launchSpeed":  ev["launch_speed"],
                "launchAngle":  ev["launch_angle"],
                "totalDistance": ev["total_distance"],
                "coordX":       ev["coord_x"],
                "coordY":       ev["coord_y"],
                "trajectory":   ev["trajectory"],
                "location":     ev["location"],
                "homeScore":    ev["home_score_after"],
                "awayScore":    ev["away_score_after"],
                "description":  ev["description"],
                "venue":        ev["venue"],
            })
    except sqlite3.OperationalError as e:
        # Table doesn't exist yet (pre-PR #5a checkout). Run create_tables
        # to bring the schema up to date, then continue with empty events.
        print(f"  [hr_recap] hr_events not yet available ({e}) — exporting "
              "without per-HR Statcast. Run create_tables + "
              "backfill_hr_events.py to populate.")

    by_date = {}
    for r in rows:
        d = r["date"]
        if d not in by_date:
            by_date[d] = {"date": d, "hitters": [], "summary": {
                "total_hr_hitters": 0, "we_picked": 0, "best_rank_we_missed": None,
            }}
        entry = dict(r)
        del entry["date"]
        # Attach per-HR Statcast list. Empty list when:
        #  - backfill hasn't reached this date yet
        #  - or the day's playByPlay didn't tag this batter's HR with hitData
        # The front-end reads `entry["hrs"]` and shows the diamond SVG
        # per HR, falling through to "no spray data" when coordX/Y are null.
        entry["hrs"] = events_by_key.get((d, r["batter_id"]), [])
        by_date[d]["hitters"].append(entry)
        s = by_date[d]["summary"]
        s["total_hr_hitters"] += 1
        if r["selected"]:
            s["we_picked"] += 1
        else:
            rk = r["rank_in_board"]
            if rk and (s["best_rank_we_missed"] is None or rk < s["best_rank_we_missed"]):
                s["best_rank_we_missed"] = rk

    recap = [by_date[d] for d in sorted(by_date.keys(), reverse=True)]
    atomic_write_json(out_dir / "hr_recap.json", {
        "days": days,
        "total_days": len(recap),
        "recap": recap,
        "exported_at": datetime.now().isoformat(),
    })
    n_with_events = sum(
        1 for day in recap for h in day["hitters"] if h.get("hrs")
    )
    print(f"  Exported hr_recap.json ({len(recap)} days, "
          f"{sum(len(d['hitters']) for d in recap)} HR hitters, "
          f"{n_with_events} with Statcast events)")



def main():
    parser = argparse.ArgumentParser(description="Export DB data to static JSON for the dashboard")
    parser.add_argument("--out", default=None,
                        help="Output directory (default: <project>/mlb_hr_bet_site/data)")
    parser.add_argument("--days", type=int, default=60, help="Days of history (default: 60)")
    args = parser.parse_args()

    if args.out:
        out_dir = Path(args.out)
    else:
        out_dir = Path(__file__).parent / "mlb_hr_bet_site" / "data"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Exporting site data to {out_dir}/")
    conn = get_db()
    create_tables(conn)
    try:
        export_latest_picks(conn, out_dir)
        export_history(conn, out_dir, days=args.days)
        export_performance(conn, out_dir)
        export_factor_trends(conn, out_dir, days=min(args.days, 30))
        export_hr_recap(conn, out_dir, days=args.days)
        export_hr_leaderboard(conn, out_dir, days=14, top_n=40)
    finally:
        conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
# rank-band decomp wired in 2026-04-30
