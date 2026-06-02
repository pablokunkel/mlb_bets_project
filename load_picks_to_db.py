#!/usr/bin/env python3
"""
load_picks_to_db.py — Persist a day's picks JSON to the SQLite DB.

generate_picks.py produces a JSON file (default: results/picks_<DATE>.json)
containing both the 8-pick card and the full scored board. This script reads
that JSON and inserts every board row into daily_picks, with selected=1 set
on the rows that match the 8 picks in the card.

Idempotent: deletes any existing daily_picks rows for the date before
re-inserting, so it's safe to re-run.

Usage:
    python load_picks_to_db.py                       # today, default JSON path
    python load_picks_to_db.py --date 2026-04-29
    python load_picks_to_db.py --json path/to/picks.json
    python load_picks_to_db.py --db custom.db
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from etl.db import get_db, create_tables, RESULTS_DIR


def resolve_json_path(date_str: str, explicit: str | None) -> Path:
    """Find the picks JSON for the given date.

    generate_picks.py writes to `<project>/../results/picks_<DATE>.json` by
    default (note: parent of the project dir). Fall back to a few sensible
    alternates so we work whether run_daily.bat passes --output or not.
    """
    if explicit:
        return Path(explicit)

    candidates = [
        RESULTS_DIR / f"picks_{date_str}.json",
        Path(__file__).parent / "results" / f"picks_{date_str}.json",
        Path.home() / "Desktop" / "HR-Picks" / f"picks_{date_str}.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        f"No picks JSON found for {date_str}. Tried:\n  "
        + "\n  ".join(str(c) for c in candidates)
    )


def load_picks(json_path: Path, db_path: Path | None = None) -> tuple[int, int]:
    """Insert the day's full board into daily_picks. Returns (n_inserted, n_selected)."""

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    date_str = data["date"]
    card = data.get("picks", [])
    board = data.get("full_board", [])
    weight_config = data.get("scoring_config", "default")
    mode = data.get("mode")  # 'live' / 'offline_simulation' (added 2026-05-03)

    if not board:
        # Older JSON without full_board — treat the 8-pick card as the entire board.
        board = card
        print(f"  [warn] No full_board in JSON; loading card-only ({len(card)} rows).")

    # Build a set of (player_id, game_pk) keys identifying the selected card.
    # Falls back to name+game_pk if player_id is missing in older JSONs.
    selected_keys: set[tuple] = set()
    for p in card:
        pid = p.get("player_id") or 0
        gpk = p.get("game_pk")
        if pid:
            selected_keys.add(("id", pid, gpk))
        else:
            selected_keys.add(("name", p.get("name", ""), gpk))

    def is_selected(row: dict) -> bool:
        pid = row.get("player_id") or 0
        gpk = row.get("game_pk")
        if pid and ("id", pid, gpk) in selected_keys:
            return True
        if ("name", row.get("name", ""), gpk) in selected_keys:
            return True
        # Some generators flag selected=true on the board row itself.
        return bool(row.get("selected", False))

    conn = get_db(db_path)
    create_tables(conn)

    # Idempotent: clear and re-insert this date's picks.
    conn.execute("DELETE FROM daily_picks WHERE date = ?", (date_str,))
    conn.commit()

    # B7 (2026-05-25): is_likely_out + status_description + promoted_due_to
    # are NULL-safe additive columns. Older pick JSONs (pre-B7) will set
    # them to 0 / NULL / NULL.
    pick_sql = """
        INSERT INTO daily_picks (
            date, batter_id, batter_name, team, tier, tier_label,
            game_pk, opp_pitcher, opp_pitcher_id,
            composite, power_score, matchup_score, matchup_version,
            park_score, form_score, weather_score, lineup_score,
            batting_order, weight_config, selected, rank_in_board,
            mode,
            is_likely_out, status_description, promoted_due_to
        ) VALUES (?, ?, ?, ?, ?, ?,  ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?, ?, ?,  ?, ?, ?)
    """

    # pick_inputs row insertion — captures the raw signals fed into each
    # factor, so the dashboard can decompose what drove each score.
    # Idempotent (INSERT OR REPLACE on PRIMARY KEY (date, batter_id)).
    # 2026-05-03 (PR #20): added bats/throws + weather_source/barrel_pct_source
    # 2026-05-03 (PR #21): vegas_implied_total -> vegas_team_total_pct rename,
    #                       vegas_team_total_raw added (was previously JSON-only)
    # 2026-05-04 (PR #34): lineup_source — flags posted / recent:DATE / roster_fallback
    # 2026-05-20 (B8): season_hr added — outcomes-cumulative HR count
    # entering the date, used by score_power's HR-floor lookup and by
    # backtest_factors.rescore_row so backtests can apply the same floor
    # that production does.
    # B16 (2026-05-27): + slate_park_pct, slate_weather_pct,
    # slate_pitcher_vulnerability_pct -- the within-slate percentile values
    # that fed score_park / score_weather / score_matchup. Persisting them
    # lets backtest_factors.rescore_row and refit_weights.rescore_row
    # replay production scoring byte-for-byte instead of falling through to
    # the v1 anchored fallbacks (which mis-scored 3 of 6 factors). NULL on
    # rows where the slate path wasn't active (offline sim, domes, pitchers
    # with <2 signals); rescore code handles those via the legacy path.
    pick_inputs_sql = """
        INSERT OR REPLACE INTO pick_inputs (
            date, batter_id,
            barrel_pct, exit_velo, hr_fb_pct, iso, xwoba_contact, pull_fb_pct,
            recent_hr_14d, recent_barrel_pct_14d, ev_trend_14d,
            recent_hr_10g, recent_iso_30g, recent_avg_30g, recent_window_days, ev_trend,
            recent_barrel_real_14d, recent_xwoba_contact_14d, recent_iso_14d,
            pitcher_hr_per_9, pitcher_era, pitcher_hh_pct, pitcher_k_per_9, pitcher_fb_pct_allowed,
            pitcher_recent_hr9_21d, pitcher_recent_starts_21d,
            pitcher_recent_era_21d, pitcher_recent_k9_21d,
            woba_vs_hand, archetype_similarity,
            vegas_team_total_pct, vegas_team_total_raw,
            platoon_advantage,
            hr_park_factor,
            temperature_f, wind_mph, wind_direction_deg, humidity_pct, is_dome,
            batting_order,
            bats, throws, weather_source, barrel_pct_source, lineup_source,
            season_hr,
            fb_slg, fb_pa, br_slg, br_pa, os_slg, os_pa,
            form_archetype_centroid_json, form_archetype_window, form_archetype_n_hrs,
            park_archetype_centroid_json, park_archetype_n_hrs,
            slate_park_pct, slate_weather_pct, slate_pitcher_vulnerability_pct
        ) VALUES (?, ?,  ?, ?, ?, ?, ?, ?,  ?, ?, ?,  ?, ?, ?, ?, ?,  ?, ?, ?,  ?, ?, ?, ?, ?,  ?, ?,  ?, ?,  ?, ?,  ?, ?,  ?,  ?,  ?, ?, ?, ?, ?,  ?,  ?, ?, ?, ?, ?,  ?,  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,  ?, ?, ?)
    """

    # Clear pick_inputs for the date too — re-runs should start clean.
    try:
        conn.execute("DELETE FROM pick_inputs WHERE date = ?", (date_str,))
    except Exception:
        # Table may not exist yet on older DBs that haven't been migrated.
        # create_tables() above should have added it; if it failed, skip.
        pass

    # 2026-05-25 (park archetype Phase 2): pre-fetch the snapshot from
    # batter_park_archetype for date=date_str. This decorates pick_inputs
    # with the centroid + n_hrs so backtest_park_archetype can read them
    # without re-joining the snapshot table on every query. The snapshot
    # honors honest as-of-date: row keyed (player_id, date_through=date_str)
    # was built from HRs strictly before date_str.
    #
    # When the snapshot table is empty for this date (e.g. Phase 2 hasn't
    # run for this date yet, or running against a fresh DB), the columns
    # stay NULL and score_park (USE_PARK_ARCHETYPE=False default) doesn't
    # care.
    archetype_lookup: dict[int, tuple[str | None, int]] = {}
    try:
        rows = conn.execute(
            """
            SELECT player_id, feature_centroid_json, COALESCE(n_hrs_used, 0)
            FROM batter_park_archetype
            WHERE date_through = ?
            """,
            (date_str,),
        ).fetchall()
        for r in rows:
            archetype_lookup[int(r[0])] = (r[1], int(r[2] or 0))
    except Exception:
        # Table may not exist on older DBs that haven't been migrated.
        # create_tables() above should have added it; if it didn't, skip.
        pass

    n_inserted = 0
    n_selected = 0
    n_inputs = 0
    for i, row in enumerate(board, start=1):
        sel = 1 if is_selected(row) else 0
        if sel:
            n_selected += 1

        card_match = None
        for p in card:
            same_id = row.get("player_id") and p.get("player_id") == row.get("player_id")
            same_name = (not row.get("player_id")) and p.get("name") == row.get("name")
            if same_id or same_name:
                card_match = p
                break

        def field(key, default=None):
            if card_match and card_match.get(key) not in (None, ""):
                return card_match.get(key, default)
            return row.get(key, default)

        conn.execute(pick_sql, (
            date_str,
            row.get("player_id") or 0,
            row.get("name"),
            row.get("team"),
            row.get("tier"),
            row.get("tier_label"),
            row.get("game_pk") or field("game_pk"),
            row.get("opp_pitcher") or field("opp_pitcher"),
            row.get("opp_pitcher_id") or field("opp_pitcher_id") or 0,
            row.get("composite"),
            row.get("power_score"),
            row.get("matchup_score"),
            field("matchup_version", "v1"),
            row.get("park_score"),
            row.get("form_score"),
            row.get("weather_score"),
            row.get("lineup_score"),
            (str(row.get("batting_order")) if row.get("batting_order") is not None else None),
            weight_config,
            sel,
            i,
            mode,
            # B7 (2026-05-25): IL/scratch flags. Default to 0 / NULL on
            # rows from older pick JSONs that pre-date the field.
            int(row.get("is_likely_out") or field("is_likely_out") or 0),
            row.get("status_description") or field("status_description"),
            row.get("promoted_due_to") or field("promoted_due_to"),
        ))
        n_inserted += 1

        # Persist raw factor inputs if generate_picks emitted them.
        inputs = row.get("inputs") or field("inputs", {}) or {}
        pid = row.get("player_id") or 0
        # Skip bench-pick artifacts: pid==0 OR all four power inputs missing.
        # These rows poison the per-factor decomposition and refit weights
        # by bringing the average barrel%/EV/HR-FB/ISO down to ~0.
        power_fields = (
            inputs.get("barrel_pct"),
            inputs.get("exit_velo"),
            inputs.get("hr_fb_pct"),
            inputs.get("iso"),
        )
        all_power_missing = all(v is None or v == 0 for v in power_fields)
        if inputs and pid and not all_power_missing:
            try:
                bo = inputs.get("batting_order")
                bo_int = bo if isinstance(bo, int) else None
                conn.execute(pick_inputs_sql, (
                    date_str, pid,
                    inputs.get("barrel_pct"),
                    inputs.get("exit_velo"),
                    inputs.get("hr_fb_pct"),
                    inputs.get("iso"),
                    inputs.get("xwoba_contact"),
                    inputs.get("pull_fb_pct"),
                    inputs.get("recent_hr_14d"),
                    inputs.get("recent_barrel_pct_14d"),
                    inputs.get("ev_trend_14d"),
                    inputs.get("recent_hr_10g"),
                    inputs.get("recent_iso_30g"),
                    inputs.get("recent_avg_30g"),
                    inputs.get("recent_window_days"),
                    inputs.get("ev_trend"),
                    # B6a (2026-05-21): rolling 14d quality-contact.
                    # Defaults to None for picks JSON files pre-B6a; the
                    # column stays NULL and score_power skips through.
                    inputs.get("recent_barrel_real_14d"),
                    inputs.get("recent_xwoba_contact_14d"),
                    inputs.get("recent_iso_14d"),
                    inputs.get("pitcher_hr_per_9"),
                    inputs.get("pitcher_era"),
                    inputs.get("pitcher_hh_pct"),
                    inputs.get("pitcher_k_per_9"),
                    inputs.get("pitcher_fb_pct_allowed"),
                    # 2026-05-13: pitcher recency — rolling 21d HR/9 + start count
                    # B4 (2026-05-21): + recent ERA + recent K/9 (same payload).
                    inputs.get("pitcher_recent_hr9_21d"),
                    inputs.get("pitcher_recent_starts_21d"),
                    inputs.get("pitcher_recent_era_21d"),
                    inputs.get("pitcher_recent_k9_21d"),
                    inputs.get("woba_vs_hand"),
                    inputs.get("archetype_similarity"),
                    # Backward compat: read new name first, fall back to old
                    # name so older picks JSONs (pre-rename) still load.
                    inputs.get("vegas_team_total_pct",
                               inputs.get("vegas_implied_total")),
                    inputs.get("vegas_team_total_raw",
                               inputs.get("vegas_implied_total_raw")),
                    inputs.get("platoon_advantage"),
                    inputs.get("hr_park_factor"),
                    inputs.get("temperature_f"),
                    inputs.get("wind_mph"),
                    inputs.get("wind_direction_deg"),
                    inputs.get("humidity_pct"),
                    inputs.get("is_dome"),
                    bo_int,
                    inputs.get("bats"),
                    inputs.get("throws"),
                    inputs.get("weather_source"),
                    inputs.get("barrel_pct_source"),
                    inputs.get("lineup_source"),
                    # B8 (2026-05-20): outcomes-cumulative HR. Defaults to
                    # None for older row payloads (pre-B8 picks_history JSON
                    # files); load is still successful, column stays NULL,
                    # and backtest_factors.rescore_row falls through to its
                    # legacy behavior for those rows.
                    inputs.get("season_hr"),
                    # Phase 2 (2026-05-25): pitch-type archetype matchup
                    # sub-signal inputs. NULL on rows where
                    # fetch_batter_pitch_type_splits returned nothing for
                    # the batter. _compute_xslg_vs_arsenal's None+skip
                    # absorbs NULLs.
                    inputs.get("fb_slg"),
                    inputs.get("fb_pa"),
                    inputs.get("br_slg"),
                    inputs.get("br_pa"),
                    inputs.get("os_slg"),
                    inputs.get("os_pa"),
                    # Phase 2 form-archetype (2026-05-26). Centroid persisted
                    # so backtest_factors.rescore_row can replay archetype-match
                    # scores without re-pulling Statcast. None for batters with
                    # <FORM_ARCHETYPE_MIN_HRS HRs (None+skip).
                    inputs.get("form_archetype_centroid_json"),
                    inputs.get("form_archetype_window"),
                    inputs.get("form_archetype_n_hrs"),
                    # 2026-05-25 (park archetype Phase 2): centroid + n_hrs
                    # from batter_park_archetype (populated by
                    # etl/backfill_park_archetype.py). Missing snapshot row
                    # -> both NULL; harness treats NULL centroid as
                    # below-threshold (None+skip).
                    inputs.get("park_archetype_centroid_json"),
                    inputs.get("park_archetype_n_hrs"),
                    # B16 (2026-05-27): slate-relative percentiles that fed
                    # score_park / score_weather / score_matchup at scoring
                    # time. NULL when the slate-relative path wasn't active
                    # for this row (offline sim, dome weather, pitcher with
                    # <2 signals). Pre-B16 picks JSONs don't carry these
                    # keys -- .get() returns None and the column stays NULL.
                    inputs.get("slate_park_pct"),
                    inputs.get("slate_weather_pct"),
                    inputs.get("slate_pitcher_vulnerability_pct"),

                ))
                n_inputs += 1
            except Exception:
                pass

    conn.commit()
    conn.close()
    if n_inputs > 0:
        print(f"  Persisted {n_inputs} pick_inputs rows")
    return n_inserted, n_selected


def main():
    ap = argparse.ArgumentParser(description="Load a day's picks JSON into daily_picks")
    ap.add_argument("--date", default="today", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--json", default=None, help="Explicit path to picks JSON")
    ap.add_argument("--db", default=None, help="Custom DB path")
    args = ap.parse_args()

    date_str = (
        datetime.now().strftime("%Y-%m-%d")
        if args.date == "today"
        else args.date
    )

    json_path = resolve_json_path(date_str, args.json)
    print(f"Loading picks for {date_str} from {json_path} ...")
    n_ins, n_sel = load_picks(json_path, Path(args.db) if args.db else None)
    print(f"  Inserted {n_ins} board rows ({n_sel} flagged selected=1)")


if __name__ == "__main__":
    main()
