#!/usr/bin/env python3
"""
batter_ab_heatmap.py -- Season-long batter x game heatmap for model diagnostics.

Builds a single self-contained HTML file. Every batter who has appeared in an
`outcomes` row, sorted by year-to-date home runs. Each row is that batter's
season laid out one game per column; each cell is heat-shaded by what the model
thought of him that day (composite, board rank, rank within his own game, or any
single factor) and carries a glyph for what actually happened (HR / extra-base
hit / single / hitless / DNP).

The point: spot where the model was cold on a batter who went deep, or hot on a
batter who did nothing -- i.e. where the scoring is leaving signal on the table.

Interactive controls in the page:
  - Heat        -- what colours the cells (composite / rank / rank-in-game / factor)
  - Group by    -- HR season-to-date, HR last 7 days, or heat-metric decile;
                   each group header shows that group's HR-per-game rate
  - Dates       -- restrict the column range; headline + heat rescale to it
  - Show / Find -- min-HR filter and batter search

Click any cell for the full per-(batter, date) detail; click a batter name for
the season summary + HR log.

Usage:
    python diagnostics/batter_ab_heatmap.py
    python diagnostics/batter_ab_heatmap.py --db /path/to/hr_bets.db --out foo.html
    python diagnostics/batter_ab_heatmap.py --open      # open in browser when done

Data source: hr_bets.db (read-only). No network calls, no writes to the DB.
"""

import argparse
import json
import sqlite3
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SEASON = 2026


# ---------------------------------------------------------------------------
# DB location
# ---------------------------------------------------------------------------

def _outcome_rows(path: Path) -> int:
    """Row count of `outcomes`, or -1 if the DB is unreadable / has no table.
    Opened read-only so a bad path never creates an empty file."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        n = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
        conn.close()
        return n
    except sqlite3.Error:
        return -1


def find_db(explicit: str | None) -> Path:
    """Resolve hr_bets.db. Walks up from this file collecting every
    data/hr_bets.db, then picks the one with the most `outcomes` rows -- this
    skips the empty schema-only DB that git worktrees can leave behind."""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            sys.exit(f"[batter_ab_heatmap] --db not found: {p}")
        return p
    here = Path(__file__).resolve()
    candidates = []
    for parent in here.parents:
        cand = parent / "data" / "hr_bets.db"
        if cand.exists():
            candidates.append(cand)
    best, best_rows = None, 0
    for c in candidates:
        rows = _outcome_rows(c)
        if rows > best_rows:
            best, best_rows = c, rows
    if best is None:
        sys.exit("[batter_ab_heatmap] could not locate a populated hr_bets.db "
                 "-- pass --db /path/to/hr_bets.db")
    return best


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def rnd(x, n=1):
    """Round, passing None / non-numbers straight through as None."""
    if x is None:
        return None
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return None


def nz(x):
    """None -> 0 for summing."""
    return x if isinstance(x, (int, float)) else 0


def drop_nulls(d: dict) -> dict:
    """Strip None values so the embedded JSON stays small."""
    return {k: v for k, v in d.items() if v is not None}


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------

def build_dataset(db_path: Path) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    def q(sql):
        return [dict(r) for r in conn.execute(sql).fetchall()]

    outcomes = q("SELECT date, batter_id, batter_name, game_pk, ab, hits, hr_count, "
                 "rbi, doubles, triples, total_bases FROM outcomes")
    picks = q("SELECT * FROM daily_picks")
    inputs = q("SELECT * FROM pick_inputs")
    hrev = q("SELECT date, batter_id, batter_name, inning, half_inning, pitcher_name, "
             "launch_speed, launch_angle, total_distance, trajectory, hardness, "
             "description, venue FROM hr_events")
    slate = q("SELECT game_pk, date, home_team, away_team, venue, dome FROM daily_slate")
    lineup = q("SELECT game_pk, player_id, side FROM daily_lineup")
    season = q(f"SELECT * FROM season_batting WHERE season = {SEASON}")

    conn.close()

    # --- season stats by player -------------------------------------------
    season_by_id = {r["player_id"]: r for r in season}

    # --- slate lookup: game_pk -> venue / teams ---------------------------
    slate_by_pk = {r["game_pk"]: r for r in slate}

    # --- lineup side: (game_pk, batter_id) -> 'home' / 'away' -------------
    # daily_slate stores full team names; this resolves which side a batter
    # was on without an abbreviation<->full-name map.
    lineup_side = {(r["game_pk"], r["player_id"]): r["side"] for r in lineup}

    # --- outcomes aggregated to (batter, date) ----------------------------
    # Doubleheaders produce two rows same date; fold them into one cell.
    out_cell = defaultdict(lambda: {
        "ab": [], "hits": [], "hr": 0, "rbi": [], "d2": 0, "d3": 0, "tb": 0,
        "pks": [],
    })
    name_seen = defaultdict(lambda: defaultdict(int))
    for r in outcomes:
        key = (r["batter_id"], r["date"])
        c = out_cell[key]
        c["ab"].append(r["ab"])
        c["hits"].append(r["hits"])
        c["rbi"].append(r["rbi"])
        c["hr"] += nz(r["hr_count"])
        c["d2"] += nz(r["doubles"])
        c["d3"] += nz(r["triples"])
        c["tb"] += nz(r["total_bases"])
        if r["game_pk"] is not None:
            c["pks"].append(r["game_pk"])
        if r["batter_name"]:
            name_seen[r["batter_id"]][r["batter_name"]] += 1

    def fold(vals):
        keep = [v for v in vals if v is not None]
        return sum(keep) if keep else None

    # --- daily_picks deduped to (date, batter): keep best (lowest) rank ---
    pick_cell = {}
    for r in picks:
        key = (r["batter_id"], r["date"])
        prev = pick_cell.get(key)
        if prev is None:
            pick_cell[key] = r
            continue
        pr, cr = prev.get("rank_in_board"), r.get("rank_in_board")
        if cr is not None and (pr is None or cr < pr):
            pick_cell[key] = r

    # --- pick_inputs keyed (date, batter) (PK, unique) --------------------
    input_cell = {(r["batter_id"], r["date"]): r for r in inputs}

    # --- hr_events grouped (date, batter) ---------------------------------
    hr_by_cell = defaultdict(list)
    for r in hrev:
        hr_by_cell[(r["batter_id"], r["date"])].append(r)

    # --- date axis: dates with real outcomes only -------------------------
    # A model-only date (scores, results pending) can't be evaluated, so it is
    # not a column. Outcome dates with no model scores ARE kept (the gap shows).
    all_dates = sorted({r["date"] for r in outcomes})
    model_dates = sorted({r["date"] for r in picks})
    model_date_set = set(model_dates)

    # --- batter universe: anyone with an outcomes row ---------------------
    batter_ids = {bid for (bid, _d) in out_cell}

    def batter_name(bid):
        sb = season_by_id.get(bid)
        if sb and sb.get("player_name"):
            return sb["player_name"]
        seen = name_seen.get(bid, {})
        if seen:
            return max(seen, key=seen.get)
        return f"#{bid}"

    def batter_team(bid):
        sb = season_by_id.get(bid)
        if sb and sb.get("team") and sb["team"] != "???":
            return sb["team"]
        # fall back to most recent non-placeholder daily_picks team
        latest = None
        for r in picks:
            t = r.get("team")
            if r["batter_id"] == bid and t and t != "???":
                if latest is None or r["date"] > latest[0]:
                    latest = (r["date"], t)
        if latest:
            return latest[1]
        return sb.get("team") if sb else None

    def batter_bats(bid):
        sb = season_by_id.get(bid)
        if sb and sb.get("bats"):
            return sb["bats"]
        for d in reversed(all_dates):
            ic = input_cell.get((bid, d))
            if ic and ic.get("bats"):
                return ic["bats"]
        return None

    # --- assemble cells ---------------------------------------------------
    cells = {}

    for bid in batter_ids:
        bcells = {}
        for d in all_dates:
            oc = out_cell.get((bid, d))
            pc = pick_cell.get((bid, d))
            ic = input_cell.get((bid, d))
            hrs = hr_by_cell.get((bid, d), [])
            if oc is None:
                continue  # batter did not play this date -- leave blank (DNP)

            cell = {}

            # ---- outcome ----
            cell["ab"] = fold(oc["ab"])
            cell["h"] = fold(oc["hits"])
            cell["hr"] = oc["hr"]
            cell["rbi"] = fold(oc["rbi"])
            cell["d2"] = oc["d2"] or None
            cell["d3"] = oc["d3"] or None
            cell["tb"] = oc["tb"] or None
            cell["dh"] = 1 if len(oc["pks"]) > 1 else None

            # ---- model scores ----
            if pc is not None:
                cell["c"] = rnd(pc.get("composite"))
                cell["rk"] = pc.get("rank_in_board")
                cell["sel"] = pc.get("selected") or None
                cell["tl"] = pc.get("tier_label")
                cell["mv"] = pc.get("matchup_version")
                cell["opp"] = pc.get("opp_pitcher")
                cell["f"] = drop_nulls({
                    "pw": rnd(pc.get("power_score")),
                    "mu": rnd(pc.get("matchup_score")),
                    "pk": rnd(pc.get("park_score")),
                    "fm": rnd(pc.get("form_score")),
                    "wx": rnd(pc.get("weather_score")),
                    "ln": rnd(pc.get("lineup_score")),
                })
                cell["vuln"] = rnd(pc.get("vulnerability"))
                cell["arch"] = rnd(pc.get("archetype_sim"))
                gpk = pc.get("game_pk")
                cell["gpk"] = gpk          # used to compute rank-within-game
                sl = slate_by_pk.get(gpk)
                if sl:
                    cell["ven"] = sl.get("venue")
                    side = lineup_side.get((gpk, bid))
                    if side == "home":
                        cell["oppTeam"] = sl.get("away_team")
                        cell["home"] = 1
                    elif side == "away":
                        cell["oppTeam"] = sl.get("home_team")
                        cell["home"] = 0

            # ---- raw inputs ----
            if ic is not None:
                cell["in"] = drop_nulls({
                    "barrel": rnd(ic.get("barrel_pct")),
                    "ev": rnd(ic.get("exit_velo")),
                    "hrfb": rnd(ic.get("hr_fb_pct")),
                    "iso": rnd(ic.get("iso"), 3),
                    "xwoba": rnd(ic.get("xwoba_contact"), 3),
                    "pullfb": rnd(ic.get("pull_fb_pct")),
                    "rhr14": rnd(ic.get("recent_hr_14d")),
                    "rbar14": rnd(ic.get("recent_barrel_pct_14d")),
                    "evtr14": rnd(ic.get("ev_trend_14d")),
                    # Rebuilt Form columns (PR #56, live from 2026-05-19).
                    # Both old and new render in the modal so the transition
                    # is visible -- old populated for historical rows, new for
                    # post-fix rows; the dates that flip side are easy to spot.
                    "rhr10g": rnd(ic.get("recent_hr_10g")),
                    "riso30g": rnd(ic.get("recent_iso_30g"), 3),
                    "ravg30g": rnd(ic.get("recent_avg_30g"), 3),
                    "rwd": ic.get("recent_window_days"),
                    "evtrend": rnd(ic.get("ev_trend"), 2),
                    "phr9": rnd(ic.get("pitcher_hr_per_9"), 2),
                    "pera": rnd(ic.get("pitcher_era"), 2),
                    "phh": rnd(ic.get("pitcher_hh_pct")),
                    "pk9": rnd(ic.get("pitcher_k_per_9"), 2),
                    "pfb": rnd(ic.get("pitcher_fb_pct_allowed")),
                    "prhr9": rnd(ic.get("pitcher_recent_hr9_21d"), 2),
                    "prst": ic.get("pitcher_recent_starts_21d"),
                    "wobah": rnd(ic.get("woba_vs_hand"), 3),
                    "archsim": rnd(ic.get("archetype_similarity")),
                    "vegasr": rnd(ic.get("vegas_team_total_raw"), 2),
                    "vegasp": rnd(ic.get("vegas_team_total_pct")),
                    "pf": rnd(ic.get("hr_park_factor")),
                    "temp": rnd(ic.get("temperature_f")),
                    "wind": rnd(ic.get("wind_mph")),
                    "winddir": ic.get("wind_direction_deg"),
                    "hum": rnd(ic.get("humidity_pct")),
                    "dome": ic.get("is_dome"),
                    "bo": ic.get("batting_order"),
                    "throws": ic.get("throws"),
                    "lsrc": ic.get("lineup_source"),
                    "bsrc": ic.get("barrel_pct_source"),
                    "wsrc": ic.get("weather_source"),
                })

            # ---- HR statcast events ----
            if hrs:
                cell["hrs"] = [drop_nulls({
                    "inn": h.get("inning"),
                    "half": h.get("half_inning"),
                    "pit": h.get("pitcher_name"),
                    "ev": rnd(h.get("launch_speed")),
                    "la": rnd(h.get("launch_angle")),
                    "dist": h.get("total_distance"),
                    "traj": h.get("trajectory"),
                    "hard": h.get("hardness"),
                    "ven": h.get("venue") or None,
                    "desc": h.get("description"),
                }) for h in hrs]
                if cell.get("ven") is None:
                    for e in cell["hrs"]:
                        if e.get("ven"):
                            cell["ven"] = e["ven"]
                            break

            bcells[d] = drop_nulls(cell)
        if bcells:
            cells[str(bid)] = bcells

    # --- rank within game: among cells sharing (date, game_pk), order by ---
    # composite. rig = 1 is the model's top bat in that game; gn = field size.
    by_game = defaultdict(list)
    for bc in cells.values():
        for d, cell in bc.items():
            g = cell.get("gpk")
            if g is not None and cell.get("c") is not None:
                by_game[(d, g)].append(cell)
    for lst in by_game.values():
        lst.sort(key=lambda c: c["c"], reverse=True)
        for i, cell in enumerate(lst):
            cell["rig"] = i + 1
            cell["gn"] = len(lst)
    for bc in cells.values():
        for cell in bc.values():
            cell.pop("gpk", None)   # only needed for the rank pass above

    # --- batter list, sorted by YTD HR (from the cells we built) ----------
    batters = []
    for bid in batter_ids:
        bc = cells.get(str(bid), {})
        hr = sum((c.get("hr") or 0) for c in bc.values())
        games = sum(1 for c in bc.values() if "ab" in c or "h" in c or "hr" in c)
        ab = sum(nz(c.get("ab")) for c in bc.values())
        h = sum(nz(c.get("h")) for c in bc.values())
        sb = season_by_id.get(bid)
        season_line = None
        if sb:
            season_line = drop_nulls({
                "g": sb.get("games"), "pa": sb.get("pa"), "ab": sb.get("ab"),
                "hr": sb.get("hr"), "avg": rnd(sb.get("avg"), 3),
                "slg": rnd(sb.get("slg"), 3), "obp": rnd(sb.get("obp"), 3),
                "iso": rnd(sb.get("iso"), 3), "woba": rnd(sb.get("woba"), 3),
                "barrel": rnd(sb.get("barrel_pct")), "ev": rnd(sb.get("exit_velo")),
                "hrfb": rnd(sb.get("hr_fb_pct")), "tier": sb.get("tier"),
            })
        batters.append({
            "id": bid,
            "name": batter_name(bid),
            "team": batter_team(bid),
            "bats": batter_bats(bid),
            "hr": hr,
            "g": games,
            "ab": ab,
            "h": h,
            "season": season_line,
        })
    batters.sort(key=lambda b: (-b["hr"], b["name"]))

    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "through": {"outcomes": all_dates[-1] if all_dates else None,
                    "model": model_dates[-1] if model_dates else None,
                    "start": all_dates[0] if all_dates else None},
        "dates": all_dates,
        "modelDates": sorted(model_date_set),
        "batters": batters,
        "cells": cells,
    }


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Batter x Game Heatmap -- HR Model Diagnostics</title>
<style>
  :root { --bg:#0e1116; --panel:#161b22; --panel2:#1c2230; --line:#2a313c;
          --txt:#e6e9ef; --dim:#8b95a5; --gold:#ffd23f; --cy:#4fd1ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font:13px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
  h1 { font-size:18px; margin:0 0 2px; }
  a { color:var(--cy); }
  .wrap { padding:16px 18px 60px; }
  .sub { color:var(--dim); font-size:12px; }
  .warn { color:#ffb454; }

  /* headline cards */
  .cards { display:flex; flex-wrap:wrap; gap:10px; margin:14px 0 6px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:8px;
          padding:9px 13px; min-width:120px; }
  .card .big { font-size:21px; font-weight:700; }
  .card .lbl { color:var(--dim); font-size:11px; text-transform:uppercase;
               letter-spacing:.4px; }
  .card.good .big { color:#46d17a; } .card.mid .big { color:var(--gold); }
  .card.bad .big { color:#ff6b5e; }

  /* controls */
  .ctl { display:flex; flex-wrap:wrap; gap:12px 14px; align-items:center;
         background:var(--panel); border:1px solid var(--line); border-radius:8px;
         padding:9px 13px; margin:10px 0; position:sticky; top:0; z-index:30; }
  .ctl label { color:var(--dim); font-size:11px; text-transform:uppercase;
               letter-spacing:.4px; margin-right:5px; }
  select, input[type=text] { background:var(--panel2); color:var(--txt);
         border:1px solid var(--line); border-radius:5px; padding:4px 7px;
         font-size:12px; }
  input[type=text] { width:160px; }
  .seg { display:inline-flex; border:1px solid var(--line); border-radius:5px;
         overflow:hidden; }
  .seg button { background:var(--panel2); color:var(--dim); border:0;
         padding:4px 9px; cursor:pointer; font-size:12px; }
  .seg button.on { background:var(--cy); color:#06121a; font-weight:700; }
  .tg { cursor:pointer; user-select:none; display:flex; align-items:center; gap:5px; }
  .tg input { accent-color:var(--cy); }

  /* grid */
  .scroll { overflow:auto; border:1px solid var(--line); border-radius:8px;
            max-height:78vh; }
  table { border-collapse:separate; border-spacing:0; }
  th,td { padding:0; margin:0; }
  thead th { position:sticky; top:0; z-index:20; background:var(--panel2);
             color:var(--dim); font-weight:600; font-size:10px;
             border-bottom:1px solid var(--line); border-right:1px solid #20262f;
             height:34px; min-width:30px; width:30px; }
  thead th.mgap { background:#12161c; }
  thead th.corner { left:0; z-index:25; min-width:248px; width:248px;
             text-align:left; }
  tbody th.name { position:sticky; left:0; z-index:10; background:var(--panel);
             border-right:1px solid var(--line); border-bottom:1px solid #20262f;
             min-width:248px; width:248px; text-align:left; padding:0 9px;
             height:30px; cursor:pointer; }
  tbody th.name:hover { background:var(--panel2); }
  tbody tr:hover th.name { background:var(--panel2); }
  .nm { display:flex; align-items:baseline; gap:7px; }
  .nm .rk { color:var(--dim); font-size:10px; width:26px; flex:none; }
  .nm .who { font-weight:600; overflow:hidden; text-overflow:ellipsis;
             white-space:nowrap; }
  .nm .tm { color:var(--dim); font-size:10px; }
  .nm .hrn { margin-left:auto; color:var(--gold); font-weight:700; font-size:13px;
             flex:none; }
  .nm .hrn small { color:var(--dim); font-weight:400; }

  td.cell { width:30px; height:30px; text-align:center; vertical-align:middle;
            border-right:1px solid #1a1f27; border-bottom:1px solid #1a1f27;
            cursor:pointer; position:relative; font-size:11px; }
  td.cell:hover { outline:2px solid var(--cy); outline-offset:-2px; z-index:5; }
  td.dnp { background:#0d1015; cursor:default; }
  td.nomodel { background-image:repeating-linear-gradient(45deg,
            #181c23 0 5px,#13161c 5px 10px); }
  .g { font-weight:700; text-shadow:0 0 3px #000,0 1px 2px #000; }
  .g.hr { color:var(--gold); font-size:13px; }
  .g.xb { color:#dfe6f0; }
  .g.s1 { color:#aab3c2; font-weight:600; }
  .g.out { color:#5c6675; font-weight:400; }
  td.sel { box-shadow:inset 0 0 0 2px var(--cy); }
  td.miss { box-shadow:inset 0 0 0 2px #ff5b4d; }
  td.sel.miss { box-shadow:inset 0 0 0 2px #ff5b4d,inset 0 0 0 4px var(--cy); }
  body.focushr td.cell:not(.has-hr) .g { opacity:.18; }
  body.focushr td.nomodel:not(.has-hr) { opacity:.5; }

  /* group header rows */
  tbody tr.ghdr th.ghn { position:sticky; left:0; z-index:11;
            background:#102433; color:var(--cy); font-weight:700; font-size:11px;
            text-transform:uppercase; letter-spacing:.4px; text-align:left;
            padding:5px 10px; border-top:2px solid #2d6f8c;
            border-bottom:1px solid var(--line); }
  .ghs { color:var(--dim); font-weight:400; text-transform:none;
         letter-spacing:0; margin-left:10px; }

  .legend { display:flex; flex-wrap:wrap; gap:7px 16px; margin:9px 2px;
            color:var(--dim); font-size:11px; align-items:center; }
  .legend b { color:var(--txt); }
  .chip { display:inline-block; width:13px; height:13px; border-radius:3px;
          vertical-align:-2px; margin-right:3px; }
  .ramp { display:inline-block; width:120px; height:11px; border-radius:3px;
          vertical-align:-1px; }

  /* modal */
  .ovl { position:fixed; inset:0; background:rgba(4,7,11,.78); z-index:100;
         display:none; align-items:flex-start; justify-content:center;
         padding:34px 16px; overflow:auto; }
  .ovl.on { display:flex; }
  .modal { background:var(--panel); border:1px solid var(--line);
           border-radius:11px; width:min(880px,100%); }
  .mhd { display:flex; align-items:flex-start; gap:12px; padding:14px 17px;
         border-bottom:1px solid var(--line); }
  .mhd h2 { margin:0; font-size:16px; }
  .mhd .x { margin-left:auto; cursor:pointer; color:var(--dim); font-size:22px;
            line-height:1; border:0; background:none; }
  .mbody { padding:14px 17px; }
  .sec { margin:0 0 16px; }
  .sec h3 { margin:0 0 7px; font-size:11px; text-transform:uppercase;
            letter-spacing:.6px; color:var(--cy); }
  .kv { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
        gap:6px 14px; }
  .kv div { display:flex; justify-content:space-between; gap:8px;
            border-bottom:1px dotted #2a313c; padding:2px 0; }
  .kv .k { color:var(--dim); } .kv .v { font-weight:600; text-align:right; }
  .kv .v.na { color:#4d5666; font-weight:400; }
  .fbars { display:flex; flex-direction:column; gap:5px; }
  .fb { display:flex; align-items:center; gap:9px; }
  .fb .fl { width:74px; color:var(--dim); font-size:11px; }
  .fb .ft { flex:1; background:var(--panel2); border-radius:4px; height:15px;
            position:relative; overflow:hidden; }
  .fb .ff { height:100%; border-radius:4px; }
  .fb .fv { width:64px; font-size:11px; font-weight:600; }
  .hrcard { background:var(--panel2); border:1px solid var(--line);
            border-left:3px solid var(--gold); border-radius:7px;
            padding:8px 11px; margin-bottom:7px; }
  .hrcard .d1 { font-weight:600; margin-bottom:3px; }
  .hrcard .d2 { color:var(--dim); font-size:11px; }
  .hrcard .stat { display:inline-block; margin-right:13px; }
  .hrcard .stat b { color:var(--gold); }
  .pill { display:inline-block; padding:1px 7px; border-radius:9px; font-size:10px;
          font-weight:700; }
  .pill.sel { background:var(--cy); color:#06121a; }
  .pill.miss { background:#ff5b4d; color:#1a0604; }
  .bigc { font-size:30px; font-weight:800; }
  .hrlog { max-height:330px; overflow:auto; }
  .hrlog .row { display:flex; gap:9px; padding:4px 0;
                border-bottom:1px dotted #2a313c; font-size:12px; }
  .hrlog .row .dt { width:74px; color:var(--dim); }
  .hrlog .row .cc { width:54px; font-weight:700; }
  .empty { color:var(--dim); padding:30px; text-align:center; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Batter &times; Game Heatmap <span class="sub" id="rangeNote"></span></h1>
  <div class="sub" id="genNote"></div>

  <div class="cards" id="cards"></div>

  <div class="ctl">
    <span><label>Heat</label>
      <select id="mode">
        <option value="c">Composite score</option>
        <option value="rk">Board rank</option>
        <option value="rg">Rank within game</option>
        <option value="pw">Power factor</option>
        <option value="mu">Matchup factor</option>
        <option value="fm">Form factor</option>
        <option value="wx">Weather factor</option>
        <option value="pk">Park factor</option>
        <option value="ln">Lineup factor</option>
      </select></span>
    <span><label>Group by</label>
      <select id="groupby">
        <option value="none">none</option>
        <option value="hrWindow">HR games — in view window</option>
        <option value="hr7">HR games — last 7 days of view</option>
        <option value="decile">Heat-metric decile</option>
      </select></span>
    <span><label>Dates</label>
      <select id="dfrom"></select>
      <span class="sub">&rarr;</span>
      <select id="dto"></select></span>
    <span><label>Show</label>
      <span class="seg" id="minhr">
        <button data-v="1" class="on">1+ HR</button>
        <button data-v="3">3+</button>
        <button data-v="5">5+</button>
        <button data-v="10">10+</button>
        <button data-v="0">All</button>
      </span></span>
    <span><label>Find</label>
      <input type="text" id="search" placeholder="batter or team..."></span>
    <label class="tg"><input type="checkbox" id="focushr">Focus HRs</label>
    <label class="tg"><input type="checkbox" id="flagmiss" checked>Flag misses</label>
    <span class="sub" id="count"></span>
  </div>

  <div class="legend" id="legend"></div>

  <div class="scroll"><table id="grid"></table></div>
  <div id="emptymsg"></div>
</div>

<div class="ovl" id="ovl"><div class="modal" id="modal"></div></div>

<script id="payload" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('payload').textContent);
const C = D.cells, B = D.batters, DATES = D.dates;
const MODELSET = new Set(D.modelDates);

/* ---- factor / mode metadata ---- */
const FACT = {pw:'Power',mu:'Matchup',pk:'Park',fm:'Form',wx:'Weather',ln:'Lineup'};
const MODE_LABEL = {c:'Composite',rk:'Board rank',rg:'Rank within game',
  pw:'Power',mu:'Matchup',fm:'Form',wx:'Weather',pk:'Park',ln:'Lineup'};
const INVERT = new Set(['rk','rg']);   /* lower value = hotter */

/* value a cell contributes for a given heat mode */
function metric(cell, mode){
  if(!cell) return null;
  if(mode==='c')  return cell.c ?? null;
  if(mode==='rk') return cell.rk ?? null;
  if(mode==='rg') return cell.rig ?? null;
  if(cell.f && cell.f[mode]!=null) return cell.f[mode];
  return null;
}

/* ---- heat ramp: cold(0) -> hot(1) ---- */
const RAMP=[[16,36,63],[38,65,95],[59,63,74],[122,90,44],[196,99,31],[232,51,31]];
function ramp(t){
  t=Math.max(0,Math.min(1,t));
  const s=t*(RAMP.length-1), i=Math.min(Math.floor(s),RAMP.length-2), f=s-i;
  const a=RAMP[i], b=RAMP[i+1];
  return `rgb(${Math.round(a[0]+(b[0]-a[0])*f)},`+
            `${Math.round(a[1]+(b[1]-a[1])*f)},`+
            `${Math.round(a[2]+(b[2]-a[2])*f)})`;
}

/* per-mode domain, recomputed over the visible date range so contrast adapts */
let DOMAIN={};
function computeDomains(visSet){
  const modes=['c','rk','rg','pw','mu','pk','fm','wx','ln'];
  modes.forEach(m=>{
    let lo=Infinity, hi=-Infinity;
    for(const bid in C) for(const d in C[bid]){
      if(!visSet.has(d)) continue;
      const v=metric(C[bid][d],m);
      if(v==null) continue;
      if(v<lo)lo=v; if(v>hi)hi=v;
    }
    if(lo===Infinity){lo=0;hi=1;}
    DOMAIN[m]=[lo,hi];
  });
}
function heatColor(cell, mode){
  const v=metric(cell,mode);
  if(v==null) return null;
  const [lo,hi]=DOMAIN[mode];
  let t=hi>lo ? (v-lo)/(hi-lo) : .5;
  if(INVERT.has(mode)) t=1-t;          /* rank 1 = hot */
  return ramp(t);
}

/* ---- result glyph ---- */
function glyph(cell){
  if(!cell) return {cls:'',txt:''};
  const hr=cell.hr||0;
  if(hr>=2) return {cls:'g hr',txt:'★'+hr};
  if(hr===1) return {cls:'g hr',txt:'★'};
  if(cell.h!=null){
    if(cell.d3) return {cls:'g xb',txt:'3B'};
    if(cell.d2) return {cls:'g xb',txt:'2B'};
    if(cell.h>0) return {cls:'g s1',txt:'1B'};
    if(cell.ab>0) return {cls:'g out',txt:'·'};
    return {cls:'g out',txt:'∘'};
  }
  return {cls:'g out',txt:'·'};   /* played, hits unknown (early rows) */
}
function isMiss(cell){
  if(!cell || !(cell.hr>0)) return false;
  return (cell.rk==null) || (cell.rk>30);
}

/* ---- state ---- */
const S={mode:'c',minhr:1,search:'',focushr:false,flagmiss:true,
  from:0,to:DATES.length-1,group:'none'};

function visibleDates(){ return DATES.slice(S.from,S.to+1); }

/* ---- headline (recomputed over the visible window) ---- */
function computeHeadline(visSet){
  // Counts HR GAMES (a game with >=1 HR = 1), not total HR -- a multi-HR
  // day is still one betting win, so it must not inflate the rates.
  const h={total:0,scored:0,nomodel:0,b10:0,b30:0,b100:0,b101:0,picked:0};
  for(const bid in C) for(const d in C[bid]){
    if(!visSet.has(d)) continue;
    const c=C[bid][d];
    if((c.hr||0)<=0) continue;
    h.total++;
    if(c.rk==null){ h.nomodel++; }
    else{
      h.scored++;
      if(c.rk<=10)h.b10++; else if(c.rk<=30)h.b30++;
      else if(c.rk<=100)h.b100++; else h.b101++;
      if(c.sel)h.picked++;
    }
  }
  return h;
}
function renderCards(h){
  const sc=h.scored||0;
  const pct=n=> sc? Math.round(100*n/sc)+'%' : '--';
  const cards=[
    {lbl:'HR games (window)',big:h.total,cls:''},
    {lbl:'HR games — model days',big:h.scored,cls:''},
    {lbl:'…from board top 10',big:pct(h.b10),cls:'good'},
    {lbl:'…from rank 11-30',big:pct(h.b30),cls:'mid'},
    {lbl:'…from rank 31-100',big:pct(h.b100),cls:'bad'},
    {lbl:'…from rank 101+',big:pct(h.b101),cls:'bad'},
    {lbl:'HR games by an 8-pick',big:h.picked,cls:'good'},
  ];
  document.getElementById('cards').innerHTML = cards.map(c=>
    `<div class="card ${c.cls}"><div class="big">${c.big}</div>`+
    `<div class="lbl">${c.lbl}</div></div>`).join('');
}

/* ---- legend ---- */
function renderLegend(){
  document.getElementById('legend').innerHTML=
    `<span><b>Heat</b> = <span id="lgmode"></span>: `+
      `<span class="ramp" style="background:linear-gradient(90deg,`+
      RAMP.map((c,i)=>`${ramp(i/(RAMP.length-1))} ${i/(RAMP.length-1)*100}%`)
      .join(',')+`)"></span> cold&rarr;hot</span>`+
    `<span><span class="g hr">★</span> <b>HR</b> (★N = multi)</span>`+
    `<span><span class="g xb">2B</span>/<span class="g xb">3B</span> extra-base</span>`+
    `<span><span class="g s1">1B</span> single</span>`+
    `<span><span class="g out">·</span> played, no hit</span>`+
    `<span><span class="chip" style="background:#0d1015"></span>DNP</span>`+
    `<span><span class="chip" style="background-image:repeating-linear-gradient(`+
      `45deg,#181c23 0 4px,#13161c 4px 8px)"></span>no model score</span>`+
    `<span style="box-shadow:inset 0 0 0 2px var(--cy);padding:0 6px">`+
      `cyan = was an 8-pick</span>`+
    `<span style="box-shadow:inset 0 0 0 2px #ff5b4d;padding:0 6px">`+
      `red = flagged miss (HR, board rank &gt;30)</span>`;
  document.getElementById('lgmode').textContent=MODE_LABEL[S.mode];
}

function fmtDay(d){ const p=d.split('-'); return (+p[1])+'/'+(+p[2]); }

/* ---- batter filtering ---- */
function visibleBatters(visSet){
  const q=S.search.trim().toLowerCase();
  return B.filter(b=>{
    if(b.hr < S.minhr) return false;
    if(q){
      const hay=(b.name+' '+(b.team||'')).toLowerCase();
      if(!hay.includes(q)) return false;
    }
    const row=C[String(b.id)];
    if(!row) return false;
    for(const d in row){ if(visSet.has(d)) return true; }
    return false;   /* no games in the visible window */
  });
}

/* ---- grouping ---- */
function avgMetric(b, visSet){
  const row=C[String(b.id)]||{};
  let s=0,n=0;
  for(const d in row){
    if(!visSet.has(d)) continue;
    const v=metric(row[d],S.mode);
    if(v!=null){ s+=v; n++; }
  }
  return n? s/n : null;
}
function groupStat(batters, visSet){
  let games=0, hr=0, ghr=0;
  batters.forEach(b=>{
    const row=C[String(b.id)]||{};
    for(const d in row){
      if(!visSet.has(d)) continue;
      games++;
      const n=row[d].hr||0;
      hr+=n; if(n>0) ghr++;
    }
  });
  return {games,hr,ghr, rate: games? (100*ghr/games).toFixed(1) : '0.0'};
}
function groupBatters(bs, visList, visSet){
  const G=S.group, buckets={};
  let order=[];
  const put=(k,b)=>{ (buckets[k]=buckets[k]||[]).push(b); };

  if(G==='hrWindow'){
    // HR GAMES the batter had inside the visible date window (rolls with the
    // date filter): are recent HRs coming from one-off "rogue" hitters or
    // from repeat hitters the model should have caught?
    order=['8+ HR games','5-7 HR games','3-4 HR games','2 HR games',
           '1 HR game','0 HR games'];
    bs.forEach(b=>{ const row=C[String(b.id)]||{}; let n=0;
      for(const d in row){ if(visSet.has(d) && (row[d].hr||0)>0) n++; }
      put(n>=8?'8+ HR games':n>=5?'5-7 HR games':n>=3?'3-4 HR games':
          n===2?'2 HR games':n===1?'1 HR game':'0 HR games', b); });
  } else if(G==='hr7'){
    order=['4+ HR games','3 HR games','2 HR games','1 HR game','0 HR games'];
    const last7=visList.slice(-7);
    bs.forEach(b=>{ const row=C[String(b.id)]||{}; let n=0;
      last7.forEach(d=>{ if(row[d] && (row[d].hr||0)>0) n++; });
      put(n>=4?'4+ HR games':n===1?'1 HR game':n+' HR games', b); });
  } else if(G==='decile'){
    const scored=[];
    bs.forEach(b=>{ const v=avgMetric(b,visSet);
      if(v==null) put('no model score',b); else scored.push({b,v}); });
    scored.sort((a,b)=>a.v-b.v);
    const m=scored.length;
    scored.forEach((x,i)=>{ put('D'+Math.min(10,Math.floor(i*10/m)+1), x.b); });
    for(let d=10;d>=1;d--) order.push('D'+d);
    order.push('no model score');
  }
  const label=k=>{
    if(G!=='decile' || k[0]!=='D') return k;
    const d=k.slice(1);
    return MODE_LABEL[S.mode]+' decile '+d+
      (d==='10'?' (highest avg)':d==='1'?' (lowest avg)':'');
  };
  return order.filter(k=>buckets[k] && buckets[k].length)
              .map(k=>({label:label(k), batters:buckets[k]}));
}

/* ---- one batter row ---- */
function rowHTML(b, idx, visList){
  let s=`<tr><th class="name" data-b="${b.id}">`+
    `<div class="nm"><span class="rk">${idx}</span>`+
    `<span class="who">${esc(b.name)}</span>`+
    `<span class="tm">${esc(b.team||'')}</span>`+
    `<span class="hrn">${b.hr}<small> hr</small></span></div></th>`;
  const row=C[String(b.id)]||{};
  visList.forEach(d=>{
    const cell=row[d];
    if(!cell){ s+='<td class="dnp"></td>'; return; }
    const gl=glyph(cell), hc=heatColor(cell,S.mode);
    let cls='cell';
    if(hc===null) cls+=' nomodel';
    if(cell.sel) cls+=' sel';
    if(cell.hr>0) cls+=' has-hr';
    if(S.flagmiss && isMiss(cell)) cls+=' miss';
    const style=hc?` style="background:${hc}"`:'';
    s+=`<td class="${cls}" data-b="${b.id}" data-d="${d}"${style} `+
       `title="${tooltip(b,d,cell)}"><span class="${gl.cls}">${gl.txt}</span></td>`;
  });
  return s+'</tr>';
}

/* ---- grid ---- */
function render(){
  const visList=visibleDates(), visSet=new Set(visList);
  computeDomains(visSet);
  renderCards(computeHeadline(visSet));
  document.body.classList.toggle('focushr',S.focushr);
  document.getElementById('lgmode').textContent=MODE_LABEL[S.mode];

  const bs=visibleBatters(visSet);
  const groups = (S.group==='none')
    ? [{label:null,batters:bs}]
    : groupBatters(bs,visList,visSet);

  // header
  let head='<thead><tr><th class="corner">'+
    '<div class="nm"><span class="rk">#</span><span class="who">Batter</span>'+
    '<span class="hrn">HR</span></div></th>';
  visList.forEach(d=>{
    head+=`<th class="${MODELSET.has(d)?'':'mgap'}" title="${d}">`+
      fmtDay(d).replace('/','/<br>')+`</th>`;
  });
  head+='</tr></thead>';

  // body
  const span=visList.length+1;
  let body='<tbody>', idx=0;
  groups.forEach(g=>{
    if(g.label!=null){
      const st=groupStat(g.batters,visSet);
      body+=`<tr class="ghdr"><th class="ghn" colspan="${span}">${esc(g.label)}`+
        `<span class="ghs">${g.batters.length} batters · ${st.games} games · `+
        `${st.ghr} HR games · ${st.rate}% of games went yard</span></th></tr>`;
    }
    g.batters.forEach(b=>{ idx++; body+=rowHTML(b,idx,visList); });
  });
  body+='</tbody>';

  document.getElementById('grid').innerHTML=head+body;
  document.getElementById('count').textContent=
    bs.length+' batters · '+visList.length+'/'+DATES.length+' days'+
    (S.group==='none'?'':' · '+groups.length+' groups');
  document.getElementById('emptymsg').innerHTML = bs.length? '' :
    '<div class="empty">No batters match.</div>';
}

function tooltip(b,d,cell){
  let t=b.name+'  '+d;
  const hr=cell.hr||0;
  if(hr) t+='  — '+hr+' HR';
  else if(cell.h!=null) t+='  — '+(cell.h)+'-for-'+(cell.ab??'?');
  if(cell.c!=null){
    t+='  — comp '+cell.c+' (board rank '+(cell.rk??'?');
    if(cell.rig) t+=', game rank '+cell.rig+'/'+cell.gn;
    t+=')';
  } else t+='  — no model score';
  return esc(t);
}

function esc(s){ return String(s==null?'':s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;'); }

/* ---- modal: cell detail ---- */
function num(v,na='--'){ return v==null? `<span class="v na">${na}</span>`
  : `<span class="v">${v}</span>`; }
function kv(k,v,na){ return `<div><span class="k">${k}</span>${num(v,na)}</div>`; }

function openCell(bid,d){
  const b=B.find(x=>x.id==bid), cell=(C[String(bid)]||{})[d];
  if(!cell) return;
  const hr=cell.hr||0;
  let h='';

  // header
  let tags='';
  if(cell.sel) tags+=' <span class="pill sel">8-PICK</span>';
  if(isMiss(cell)) tags+=' <span class="pill miss">FLAGGED MISS</span>';
  h+=`<div class="mhd"><div><h2>${esc(b.name)} <span class="sub">`+
     `${esc(b.team||'')}${b.bats?' · bats '+b.bats:''}</span>${tags}</h2>`+
     `<div class="sub">${d}${cell.dh?' · doubleheader (folded)':''}`+
     `${cell.tl?' · '+esc(cell.tl):''}</div></div>`+
     `<button class="x" onclick="closeModal()">&times;</button></div>`;
  h+='<div class="mbody">';

  // outcome
  h+='<div class="sec"><h3>What happened</h3><div class="kv">';
  h+=kv('Home runs',hr);
  h+=kv('Hits',cell.h); h+=kv('At-bats',cell.ab);
  h+=kv('Doubles',cell.d2||0); h+=kv('Triples',cell.d3||0);
  h+=kv('RBI',cell.rbi); h+=kv('Total bases',cell.tb||0);
  h+='</div></div>';

  // HR statcast
  if(cell.hrs && cell.hrs.length){
    h+='<div class="sec"><h3>Home run detail (Statcast)</h3>';
    cell.hrs.forEach(e=>{
      h+='<div class="hrcard"><div class="d1">'+
        (e.inn?('Inn '+e.inn+(e.half?' ('+e.half+')':'')):'HR')+
        (e.pit?' — off '+esc(e.pit):'')+'</div>';
      h+='<div class="d2">';
      if(e.ev!=null)  h+='<span class="stat">EV <b>'+e.ev+'</b> mph</span>';
      if(e.la!=null)  h+='<span class="stat">LA <b>'+e.la+'</b>&deg;</span>';
      if(e.dist!=null)h+='<span class="stat">Dist <b>'+e.dist+'</b> ft</span>';
      if(e.traj)      h+='<span class="stat">'+esc(e.traj)+'</span>';
      if(e.hard)      h+='<span class="stat">'+esc(e.hard)+' contact</span>';
      h+='</div>';
      if(e.desc) h+='<div class="d2" style="margin-top:4px">'+esc(e.desc)+'</div>';
      h+='</div>';
    });
    h+='</div>';
  }

  // model
  if(cell.c!=null){
    h+='<div class="sec"><h3>Model read</h3>';
    h+='<div style="display:flex;gap:18px;align-items:center;margin-bottom:9px;'+
       'flex-wrap:wrap">'+
       '<div><div class="bigc" style="color:'+(heatColor(cell,'c')||'#888')+'">'+
       cell.c+'</div><div class="sub">composite</div></div>'+
       '<div><div class="bigc">'+(cell.rk??'--')+'</div>'+
       '<div class="sub">board rank that day</div></div>'+
       (cell.rig?'<div><div class="bigc">'+cell.rig+
       '<span style="font-size:14px;color:var(--dim)">/'+cell.gn+'</span></div>'+
       '<div class="sub">rank within its game</div></div>':'')+
       (cell.mv?'<div><div class="bigc" style="font-size:15px">'+esc(cell.mv)+
       '</div><div class="sub">matchup path</div></div>':'')+'</div>';
    h+='<div class="fbars">';
    Object.keys(FACT).forEach(k=>{
      const v=cell.f? cell.f[k] : null;
      const w=v==null?0:Math.max(0,Math.min(100,v));
      h+='<div class="fb"><span class="fl">'+FACT[k]+'</span>'+
         '<span class="ft"><span class="ff" style="width:'+w+'%;background:'+
         (v==null?'#333':ramp(w/100))+'"></span></span>'+
         '<span class="fv">'+(v==null?'<span class="na">n/a</span>':v)+'</span>'+
         '</div>';
    });
    h+='</div></div>';
  } else {
    h+='<div class="sec"><h3>Model read</h3><div class="sub">'+
       'No model score for this date — the scoring pipeline did not run '+
       '(or this batter was not in a scored slate).</div></div>';
  }

  // matchup / context
  const I=cell.in||{};
  h+='<div class="sec"><h3>Matchup &amp; context</h3><div class="kv">';
  h+=kv('Opp. pitcher',cell.opp?esc(cell.opp):null);
  h+=kv('Pitcher throws',I.throws);
  h+=kv('Opponent',cell.oppTeam?esc(cell.oppTeam):null);
  h+=kv('Venue',cell.ven?esc(cell.ven):null);
  h+=kv('Home/Away',cell.home==null?null:(cell.home?'Home':'Away'));
  h+=kv('Batting order',I.bo);
  h+=kv('Lineup source',I.lsrc?esc(I.lsrc):null);
  h+='</div></div>';

  // raw inputs
  if(cell.in){
    h+='<div class="sec"><h3>Model inputs &mdash; power &amp; form</h3>'+
       '<div class="kv">';
    h+=kv('Barrel %',I.barrel); h+=kv('Exit velo',I.ev);
    h+=kv('HR/FB %',I.hrfb); h+=kv('ISO',I.iso);
    h+=kv('xwOBA contact',I.xwoba); h+=kv('Pull-FB %',I.pullfb);
    h+=kv('HR last 14d (old)',I.rhr14); h+=kv('Barrel% 14d (proxy)',I.rbar14);
    h+=kv('EV trend 14d (proxy)',I.evtr14);
    h+=kv('HR last 10g (new)',I.rhr10g); h+=kv('ISO 30g (new)',I.riso30g);
    h+=kv('AVG 30g (new)',I.ravg30g); h+=kv('Window (d)',I.rwd);
    h+=kv('EV trend (Phase 2)',I.evtrend);
    h+=kv('Barrel source',I.bsrc?esc(I.bsrc):null);
    h+='</div></div>';
    h+='<div class="sec"><h3>Model inputs &mdash; pitcher, park, weather, Vegas</h3>'+
       '<div class="kv">';
    h+=kv('Pitcher HR/9',I.phr9); h+=kv('Pitcher ERA',I.pera);
    h+=kv('Pitcher hard-hit%',I.phh); h+=kv('Pitcher K/9',I.pk9);
    h+=kv('Pitcher FB% allowed',I.pfb); h+=kv('Pitcher HR/9 (21d)',I.prhr9);
    h+=kv('Pitcher starts (21d)',I.prst);
    h+=kv('wOBA vs hand',I.wobah); h+=kv('Archetype sim',I.archsim);
    h+=kv('Vulnerability',cell.vuln);
    h+=kv('Vegas team total',I.vegasr); h+=kv('Vegas total %ile',I.vegasp);
    h+=kv('Park HR factor',I.pf);
    h+=kv('Temp (F)',I.temp); h+=kv('Wind (mph)',I.wind);
    h+=kv('Wind dir (deg)',I.winddir); h+=kv('Humidity %',I.hum);
    h+=kv('Dome',I.dome==null?null:(I.dome?'Yes':'No'));
    h+=kv('Weather source',I.wsrc?esc(I.wsrc):null);
    h+='</div></div>';
  }

  h+='</div>';
  document.getElementById('modal').innerHTML=h;
  document.getElementById('ovl').classList.add('on');
}

/* ---- modal: batter season summary ---- */
function openBatter(bid){
  const b=B.find(x=>x.id==bid);
  const row=C[String(bid)]||{};
  let h='';
  h+=`<div class="mhd"><div><h2>${esc(b.name)} <span class="sub">`+
     `${esc(b.team||'')}${b.bats?' · bats '+b.bats:''}</span></h2>`+
     `<div class="sub">${b.hr} HR · ${b.g} games in window `+
     `(${D.through.start} → ${D.through.outcomes})</div></div>`+
     `<button class="x" onclick="closeModal()">&times;</button></div>`;
  h+='<div class="mbody">';

  const s=b.season;
  if(s){
    h+='<div class="sec"><h3>2026 season line</h3><div class="kv">';
    h+=kv('Games',s.g); h+=kv('PA',s.pa); h+=kv('AB',s.ab); h+=kv('HR',s.hr);
    h+=kv('AVG',s.avg); h+=kv('OBP',s.obp); h+=kv('SLG',s.slg);
    h+=kv('ISO',s.iso); h+=kv('wOBA',s.woba);
    h+=kv('Barrel %',s.barrel); h+=kv('Exit velo',s.ev); h+=kv('HR/FB %',s.hrfb);
    h+=kv('Tier',s.tier);
    h+='</div></div>';
  }

  // model averages on days he was scored
  let n=0,sc=0,sumc=0,sumr=0;
  const fsum={pw:0,mu:0,fm:0,wx:0,pk:0,ln:0},fn={pw:0,mu:0,fm:0,wx:0,pk:0,ln:0};
  for(const d in row){ const c=row[d];
    if(c.c!=null){ n++; sumc+=c.c; if(c.rk!=null){sc++;sumr+=c.rk;}
      if(c.f) for(const k in fsum) if(c.f[k]!=null){fsum[k]+=c.f[k];fn[k]++;} } }
  if(n){
    h+='<div class="sec"><h3>Model averages ('+n+' scored days)</h3><div class="kv">';
    h+=kv('Avg composite',(sumc/n).toFixed(1));
    h+=kv('Avg board rank',sc?(sumr/sc).toFixed(0):null);
    for(const k in fsum) h+=kv('Avg '+FACT[k],fn[k]?(fsum[k]/fn[k]).toFixed(1):null);
    h+='</div></div>';
  }

  // HR log
  const hrdays=Object.keys(row).filter(d=>row[d].hr>0).sort();
  h+='<div class="sec"><h3>Home run log ('+hrdays.length+
     ' games with a HR)</h3>';
  if(hrdays.length){
    h+='<div class="hrlog">';
    hrdays.forEach(d=>{ const c=row[d];
      const miss=isMiss(c);
      h+='<div class="row"><span class="dt">'+d+'</span>'+
         '<span class="cc" style="color:var(--gold)">'+'★'.repeat(
           Math.min(c.hr,3))+(c.hr>1?' '+c.hr:'')+'</span>'+
         '<span style="flex:1">'+(c.opp?'off '+esc(c.opp):'')+
           (c.ven?' · '+esc(c.ven):'')+'</span>'+
         '<span style="width:160px;text-align:right">'+
           (c.c!=null?('comp '+c.c+' · rank '+(c.rk??'?')):
             '<span class="na">no model score</span>')+
           (miss?' <span class="pill miss">MISS</span>':'')+'</span>'+
         '</div>';
    });
    h+='</div>';
  } else h+='<div class="sub">No home runs in this window.</div>';
  h+='<div class="sub" style="margin-top:8px">Tip: click any cell in the grid '+
     'for that game’s full input detail.</div>';
  h+='</div></div>';

  document.getElementById('modal').innerHTML=h;
  document.getElementById('ovl').classList.add('on');
}

function closeModal(){ document.getElementById('ovl').classList.remove('on'); }

/* ---- wiring ---- */
document.getElementById('grid').addEventListener('click',e=>{
  const cell=e.target.closest('td.cell');
  if(cell){ openCell(cell.dataset.b,cell.dataset.d); return; }
  const nm=e.target.closest('th.name');
  if(nm){ openBatter(nm.dataset.b); }
});
document.getElementById('ovl').addEventListener('click',e=>{
  if(e.target.id==='ovl') closeModal();
});
document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeModal(); });
document.getElementById('mode').addEventListener('change',e=>{
  S.mode=e.target.value; render();
});
document.getElementById('groupby').addEventListener('change',e=>{
  S.group=e.target.value; render();
});
const DFROM=document.getElementById('dfrom'), DTO=document.getElementById('dto');
DFROM.addEventListener('change',e=>{
  S.from=+e.target.value;
  if(S.from>S.to){ S.to=S.from; DTO.value=S.to; }
  render();
});
DTO.addEventListener('change',e=>{
  S.to=+e.target.value;
  if(S.to<S.from){ S.from=S.to; DFROM.value=S.from; }
  render();
});
document.getElementById('minhr').addEventListener('click',e=>{
  const btn=e.target.closest('button'); if(!btn) return;
  S.minhr=+btn.dataset.v;
  [...e.currentTarget.children].forEach(x=>x.classList.toggle('on',x===btn));
  render();
});
let stmr;
document.getElementById('search').addEventListener('input',e=>{
  clearTimeout(stmr); stmr=setTimeout(()=>{ S.search=e.target.value; render(); },140);
});
document.getElementById('focushr').addEventListener('change',e=>{
  S.focushr=e.target.checked; render();
});
document.getElementById('flagmiss').addEventListener('change',e=>{
  S.flagmiss=e.target.checked; render();
});

/* ---- boot ---- */
DATES.forEach((d,i)=>{
  DFROM.add(new Option(d,i));
  DTO.add(new Option(d,i));
});
DFROM.value=String(S.from);
DTO.value=String(S.to);
document.getElementById('rangeNote').textContent=
  '— '+D.through.start+' through '+D.through.outcomes;
document.getElementById('genNote').innerHTML=
  'Generated '+D.generated+' — outcomes through <b>'+D.through.outcomes+
  '</b>, model scores through <b>'+D.through.model+'</b>. '+
  '<span class="sub">Re-pull hr_bets.db from R2 and rerun to extend.</span>';
renderLegend();
render();
</script>
</body>
</html>
"""


def render_html(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")  # safe inside <script>
    return PAGE.replace("__DATA__", payload)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Batter x game HR-model heatmap")
    ap.add_argument("--db", help="path to hr_bets.db (default: auto-locate)")
    ap.add_argument("--out", help="output HTML path "
                    "(default: diagnostics/batter_ab_heatmap.html)")
    ap.add_argument("--open", action="store_true",
                    help="open the HTML in a browser when done")
    args = ap.parse_args()

    db_path = find_db(args.db)
    out_path = Path(args.out) if args.out else \
        Path(__file__).resolve().parent / "batter_ab_heatmap.html"

    print(f"[batter_ab_heatmap] db   : {db_path}")
    data = build_dataset(db_path)
    html = render_html(data)
    out_path.write_text(html, encoding="utf-8")

    ncells = sum(len(v) for v in data["cells"].values())
    print(f"[batter_ab_heatmap] out  : {out_path}  ({len(html)/1024:.0f} KB)")
    print(f"[batter_ab_heatmap] range: {data['through']['start']} "
          f"-> {data['through']['outcomes']}  "
          f"({len(data['dates'])} game days, {len(data['batters'])} batters, "
          f"{ncells} cells)")
    if args.open:
        webbrowser.open(out_path.as_uri())


if __name__ == "__main__":
    main()
