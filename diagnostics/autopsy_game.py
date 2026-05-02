"""
autopsy_game.py
---------------
Game-level autopsy: pulls every scored batter in a single game on a given
date, joins their pick_inputs + daily_picks scores + outcomes, and prints
a full decomposition so you can see which factor flagged the game wrong.

Usage (from project root):
    python autopsy_game.py 2026-05-01 SEA KC        -> SEA vs KC on 5/1
    python autopsy_game.py 2026-05-01 SEA           -> any game with SEA
    python autopsy_game.py 2026-05-01                -> lists teams that day
    python autopsy_game.py                            -> shows usage

Reads only. Safe to run any time, including during nightly ETL.
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(r"C:\Users\pablo\OneDrive\Documents\Claude\Projects\data\hr_bets.db")


def fmt(val, width=6, prec=1):
    if val is None:
        return f"{'-':>{width}}"
    if isinstance(val, float):
        return f"{val:>{width}.{prec}f}"
    return f"{val:>{width}}"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    date = sys.argv[1]
    team1 = sys.argv[2] if len(sys.argv) > 2 else None
    team2 = sys.argv[3] if len(sys.argv) > 3 else None

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # Resolve which game_pks to autopsy
    # ------------------------------------------------------------------
    if not team1:
        teams = conn.execute(
            "SELECT DISTINCT team FROM daily_picks WHERE date = ? ORDER BY team",
            (date,),
        ).fetchall()
        if not teams:
            print(f"No daily_picks rows for {date}.")
            sys.exit(1)
        print(f"Teams on {date}:")
        for t in teams:
            print(f"  {t['team']}")
        print("\nRerun with one or two team args, e.g.:")
        print(f"  python autopsy_game.py {date} SEA KC")
        sys.exit(0)

    if team2:
        games = conn.execute(
            """
            SELECT DISTINCT game_pk FROM daily_picks
            WHERE date = ?
              AND game_pk IN (SELECT game_pk FROM daily_picks WHERE date = ? AND team LIKE ?)
              AND game_pk IN (SELECT game_pk FROM daily_picks WHERE date = ? AND team LIKE ?)
            """,
            (date, date, f"%{team1}%", date, f"%{team2}%"),
        ).fetchall()
    else:
        games = conn.execute(
            "SELECT DISTINCT game_pk FROM daily_picks WHERE date = ? AND team LIKE ?",
            (date, f"%{team1}%"),
        ).fetchall()

    if not games:
        print(f"No games on {date} matching {team1}" + (f" / {team2}" if team2 else ""))
        teams = conn.execute(
            "SELECT DISTINCT team FROM daily_picks WHERE date = ? ORDER BY team",
            (date,),
        ).fetchall()
        print(f"Available teams: {[t['team'] for t in teams]}")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Slate-wide ranking lookup (composite desc, dense rank in Python)
    # ------------------------------------------------------------------
    slate = conn.execute(
        "SELECT batter_id, composite FROM daily_picks WHERE date = ? ORDER BY composite DESC",
        (date,),
    ).fetchall()
    slate_n = len(slate)
    slate_ranks = {row["batter_id"]: i + 1 for i, row in enumerate(slate)}

    print(f"Slate on {date}: {slate_n} scored batters")
    print(f"Autopsying {len(games)} game(s)\n")

    # ------------------------------------------------------------------
    # Per-game breakdown
    # ------------------------------------------------------------------
    for game_row in games:
        game_pk = game_row["game_pk"]

        batters = conn.execute(
            """
            SELECT dp.batter_id, dp.batter_name, dp.team, dp.opp_pitcher,
                   dp.composite, dp.power_score, dp.matchup_score,
                   dp.park_score, dp.form_score, dp.weather_score,
                   dp.lineup_score, dp.batting_order, dp.selected,
                   pi.barrel_pct, pi.exit_velo, pi.hr_fb_pct, pi.iso,
                   pi.xwoba_contact, pi.pull_fb_pct,
                   pi.recent_hr_14d, pi.recent_barrel_pct_14d, pi.ev_trend_14d,
                   pi.pitcher_hr_per_9, pi.pitcher_era, pi.pitcher_k_per_9,
                   pi.pitcher_fb_pct_allowed,
                   pi.woba_vs_hand, pi.vegas_implied_total,
                   pi.platoon_advantage, pi.hr_park_factor,
                   pi.temperature_f, pi.wind_mph, pi.humidity_pct, pi.is_dome,
                   COALESCE(o.hr_count, 0) AS hr_count,
                   COALESCE(o.ab, 0)        AS ab,
                   COALESCE(o.hits, 0)      AS hits
            FROM daily_picks dp
            LEFT JOIN pick_inputs pi
              ON pi.date = dp.date AND pi.batter_id = dp.batter_id
            LEFT JOIN outcomes o
              ON o.date = dp.date AND o.batter_id = dp.batter_id
            WHERE dp.date = ? AND dp.game_pk = ?
            ORDER BY dp.composite DESC
            """,
            (date, game_pk),
        ).fetchall()

        teams_in_game = sorted(set(b["team"] for b in batters))
        pitchers_in_game = sorted(set(b["opp_pitcher"] for b in batters if b["opp_pitcher"]))
        hr_hitters = [b for b in batters if b["hr_count"] > 0]
        total_hrs = sum(b["hr_count"] for b in hr_hitters)

        print("=" * 100)
        print(f"GAME AUTOPSY: {' vs '.join(teams_in_game)}  ({date}, game_pk={game_pk})")
        print(f"Pitchers in game: {' / '.join(pitchers_in_game) or '(unknown)'}")
        print(f"Outcome: {total_hrs} HR across {len(hr_hitters)} hitter(s)")
        print("=" * 100)

        # --------------- Per-HR-hitter detailed breakdown ---------------
        if hr_hitters:
            print("\n--- HR HITTERS: full score + input decomposition ---")
            for b in hr_hitters:
                rank = slate_ranks.get(b["batter_id"], "?")
                sel = "PICKED IN TOP-8" if b["selected"] else "not picked"
                print(f"\n  {b['batter_name']} ({b['team']}) - {b['hr_count']} HR, {b['hits']}/{b['ab']}")
                print(f"    Composite {b['composite']:.1f} | slate rank {rank}/{slate_n} | {sel}")
                print(f"    SCORES   power={fmt(b['power_score'])} matchup={fmt(b['matchup_score'])} "
                      f"form={fmt(b['form_score'])} weather={fmt(b['weather_score'])} "
                      f"lineup={fmt(b['lineup_score'])} park={fmt(b['park_score'])}")
                print(f"    POWER    barrel={fmt(b['barrel_pct'])} ev={fmt(b['exit_velo'])} "
                      f"hr_fb={fmt(b['hr_fb_pct'])} iso={fmt(b['iso'],prec=3)} "
                      f"xwoba={fmt(b['xwoba_contact'],prec=3)} pull_fb={fmt(b['pull_fb_pct'])}")
                print(f"    FORM     14d_hr={fmt(b['recent_hr_14d'],prec=0)} "
                      f"14d_barrel={fmt(b['recent_barrel_pct_14d'])} "
                      f"ev_trend={fmt(b['ev_trend_14d'])}")
                print(f"    MATCHUP  pit_hr/9={fmt(b['pitcher_hr_per_9'],prec=2)} "
                      f"pit_era={fmt(b['pitcher_era'],prec=2)} pit_k/9={fmt(b['pitcher_k_per_9'],prec=2)} "
                      f"pit_fb%={fmt(b['pitcher_fb_pct_allowed'])} "
                      f"woba_v_hand={fmt(b['woba_vs_hand'],prec=3)} "
                      f"vegas_tot={fmt(b['vegas_implied_total'],prec=1)} "
                      f"platoon={b['platoon_advantage']}")
                print(f"    ENV      park_pf={fmt(b['hr_park_factor'])} "
                      f"temp={fmt(b['temperature_f'],prec=0)} wind={fmt(b['wind_mph'])} "
                      f"humid={fmt(b['humidity_pct'],prec=0)} dome={b['is_dome']}")
                print(f"    LINEUP   batting_order={b['batting_order']}")
        else:
            print("\n(No HRs in this game.)")

        # --------------- Full roster sorted by composite ---------------
        print("\n--- FULL GAME ROSTER (sorted by composite; H = homered, * = top-8 pick) ---")
        print(f"{'SlateRk':<8}{'Sel':<4}{'HR':<3}{'Name':<22}{'Team':<6}"
              f"{'Comp':>6}{'Pwr':>6}{'Mch':>6}{'Frm':>6}{'Wth':>6}{'Lnp':>6}{'BO':>4}")
        print("-" * 99)
        for b in batters:
            rank = slate_ranks.get(b["batter_id"], "?")
            sel = "*" if b["selected"] else " "
            hr = "H" if b["hr_count"] > 0 else " "
            print(f"{str(rank):<8}{sel:<4}{hr:<3}{(b['batter_name'] or '?')[:20]:<22}"
                  f"{(b['team'] or '?'):<6}"
                  f"{fmt(b['composite'])}{fmt(b['power_score'])}{fmt(b['matchup_score'])}"
                  f"{fmt(b['form_score'])}{fmt(b['weather_score'])}{fmt(b['lineup_score'])}"
                  f"{fmt(b['batting_order'],width=4,prec=0)}")

        # --------------- Quick summary stats ---------------
        non_hr = [b for b in batters if b["hr_count"] == 0]
        if hr_hitters and non_hr:
            avg_hr = sum(b["composite"] for b in hr_hitters) / len(hr_hitters)
            avg_non = sum(b["composite"] for b in non_hr) / len(non_hr)
            top = max(batters, key=lambda x: x["composite"] or 0)
            print(f"\nAvg composite: HR hitters {avg_hr:.1f}  vs  non-HR in game {avg_non:.1f}")
            print(f"Top composite in game: {top['batter_name']} ({top['composite']:.1f}) — "
                  f"{'HOMERED' if top['hr_count'] > 0 else 'no HR'}")

        print()  # blank line between games

    conn.close()


if __name__ == "__main__":
    main()
