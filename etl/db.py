#!/usr/bin/env python3
"""
db.py — SQLite database layer for Daily HR Bet.

Single-file database storing all data needed for scoring:
  - batter_hr_events: every HR a batter has hit (Statcast pitch-level)
  - pitcher_arsenals: pitch mix, velocity, spin per pitcher per season
  - victim_profiles: pre-computed batter victim archetype vectors
  - daily_slate: each day's games, lineups, pitchers, weather
  - daily_lineup: confirmed lineup entries per game
  - daily_picks: model predictions with all factor scores
  - outcomes: what actually happened (HR counts per batter per game)
  - etl_log: tracks what's been fetched and when

Usage:
    from etl.db import get_db, create_tables

    db = get_db()
    create_tables(db)
"""

import sqlite3
from pathlib import Path

# Default database location
DB_DIR = Path(__file__).parent.parent.parent / "data"
DB_PATH = DB_DIR / "hr_bets.db"


def get_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """
    Get a SQLite connection with WAL mode enabled.
    WAL allows concurrent reads while the ETL writes.
    """
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row  # dict-like access on rows
    return conn


def create_tables(conn: sqlite3.Connection):
    """Create all tables if they don't exist. Safe to call repeatedly."""

    conn.executescript("""
    -- ================================================================
    -- Statcast HR events per batter (nightly ETL)
    -- ================================================================
    CREATE TABLE IF NOT EXISTS batter_hr_events (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        batter_id       INTEGER NOT NULL,
        batter_name     TEXT,
        pitcher_id      INTEGER NOT NULL,
        pitcher_name    TEXT,
        game_date       TEXT NOT NULL,
        game_pk         INTEGER,
        p_throws        TEXT,           -- R/L
        pitch_type      TEXT,           -- FF, SL, CH, etc.
        release_speed   REAL,
        release_spin    REAL,
        release_ext     REAL,
        launch_speed    REAL,
        launch_angle    REAL,
        hit_distance    REAL,
        fetched_at      TEXT DEFAULT (datetime('now')),
        UNIQUE(batter_id, game_date, pitcher_id, pitch_type, release_speed)
    );

    CREATE INDEX IF NOT EXISTS idx_hr_events_batter
        ON batter_hr_events(batter_id);
    CREATE INDEX IF NOT EXISTS idx_hr_events_pitcher
        ON batter_hr_events(pitcher_id);
    CREATE INDEX IF NOT EXISTS idx_hr_events_date
        ON batter_hr_events(game_date);

    -- ================================================================
    -- Pitcher arsenal profiles (nightly ETL, 7-day refresh)
    -- ================================================================
    CREATE TABLE IF NOT EXISTS pitcher_arsenals (
        pitcher_id      INTEGER NOT NULL,
        season          INTEGER NOT NULL,
        pitcher_name    TEXT,
        avg_fb_velo     REAL,
        fb_usage_pct    REAL,
        breaking_pct    REAL,
        offspeed_pct    REAL,
        avg_fb_spin     REAL,
        avg_extension   REAL,
        p_throws        TEXT,
        total_pitches   INTEGER,
        source          TEXT,           -- 'statcast' or 'mlb_api_estimate'
        fetched_at      TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (pitcher_id, season)
    );

    -- ================================================================
    -- Pre-computed victim profiles (nightly ETL, derived from above)
    -- ================================================================
    CREATE TABLE IF NOT EXISTS victim_profiles (
        batter_id       INTEGER NOT NULL,
        season          INTEGER NOT NULL,
        batter_name     TEXT,
        avg_fb_velo     REAL,
        fb_usage_pct    REAL,
        breaking_pct    REAL,
        offspeed_pct    REAL,
        hand_r_pct      REAL,
        avg_fb_spin     REAL,
        avg_extension   REAL,
        hr_count        INTEGER,
        n_victim_pitchers INTEGER,
        confidence      REAL,
        computed_at     TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (batter_id, season)
    );

    -- ================================================================
    -- Daily game slate (morning ETL)
    -- ================================================================
    CREATE TABLE IF NOT EXISTS daily_slate (
        game_pk         INTEGER NOT NULL,
        date            TEXT NOT NULL,
        home_team       TEXT,
        away_team       TEXT,
        venue           TEXT,
        home_pitcher_id INTEGER,
        home_pitcher    TEXT,
        away_pitcher_id INTEGER,
        away_pitcher    TEXT,
        game_time       TEXT,
        temperature_f   REAL,
        wind_mph        REAL,
        wind_dir_deg    REAL,
        humidity_pct    REAL,
        dome            INTEGER DEFAULT 0,
        fetched_at      TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (game_pk, date)
    );

    -- ================================================================
    -- Confirmed lineup entries (morning ETL)
    -- ================================================================
    CREATE TABLE IF NOT EXISTS daily_lineup (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        game_pk         INTEGER NOT NULL,
        date            TEXT NOT NULL,
        side            TEXT NOT NULL,       -- 'home' or 'away'
        batting_order   INTEGER,
        player_id       INTEGER NOT NULL,
        player_name     TEXT,
        position        TEXT,
        bats            TEXT,               -- R/L/S
        team            TEXT,
        fetched_at      TEXT DEFAULT (datetime('now')),
        UNIQUE(game_pk, date, player_id)
    );

    CREATE INDEX IF NOT EXISTS idx_lineup_date
        ON daily_lineup(date);
    CREATE INDEX IF NOT EXISTS idx_lineup_player
        ON daily_lineup(player_id);

    -- ================================================================
    -- Season batting stats (nightly ETL)
    -- ================================================================
    CREATE TABLE IF NOT EXISTS season_batting (
        player_id       INTEGER NOT NULL,
        season          INTEGER NOT NULL,
        player_name     TEXT,
        team            TEXT,
        bats            TEXT,
        games           INTEGER,
        pa              INTEGER,
        ab              INTEGER,
        hr              INTEGER,
        hr_per_pa       REAL,
        avg             REAL,
        slg             REAL,
        obp             REAL,
        iso             REAL,
        woba            REAL,
        barrel_pct      REAL,
        exit_velo       REAL,
        hr_fb_pct       REAL,
        tier            INTEGER,            -- 1, 2, 3, or NULL (untiered)
        fetched_at      TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (player_id, season)
    );

    -- ================================================================
    -- Career batting stats (for Bayesian shrinkage prior). Refreshed
    -- quarterly by sync_career_batting.py — distinct from season_batting
    -- (one row per player per season) in that this is one row per
    -- player covering their full MLB career. Used by score_power's
    -- USE_CAREER_PRIOR path to pull current-season stats toward the
    -- player's career rate when sample size is low (e.g., a slow-start
    -- veteran like Marcell Ozuna in early May with only 100 ABs).
    --
    -- Statcast-era columns (barrel_pct / exit_velo / hr_fb_pct) are
    -- only populated for players with 2015+ data. Pre-Statcast careers
    -- have those NULL — shrinkage falls back to current-only for
    -- those metrics.
    -- ================================================================
    CREATE TABLE IF NOT EXISTS career_batting (
        player_id           INTEGER PRIMARY KEY,
        player_name         TEXT,
        career_pa           INTEGER,
        career_ab           INTEGER,
        career_hr           INTEGER,
        career_hits         INTEGER,
        career_avg          REAL,
        career_slg          REAL,
        career_obp          REAL,
        career_iso          REAL,            -- derived: SLG - AVG
        career_woba         REAL,            -- est. (real wOBA needs full event log)
        career_hr_per_pa    REAL,            -- HR / PA (the cleanest career rate)
        career_barrel_pct   REAL,            -- 2015+ only (Statcast)
        career_exit_velo    REAL,            -- 2015+ only
        career_hr_fb_pct    REAL,            -- 2015+ only
        seasons_played      INTEGER,         -- # of MLB seasons (used as shrinkage strength signal)
        first_season        INTEGER,
        last_season         INTEGER,
        fetched_at          TEXT DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_career_batting_pa
        ON career_batting(career_pa DESC);

    -- ================================================================
    -- Season pitching stats (nightly ETL)
    -- ================================================================
    CREATE TABLE IF NOT EXISTS season_pitching (
        pitcher_id      INTEGER NOT NULL,
        season          INTEGER NOT NULL,
        pitcher_name    TEXT,
        team            TEXT,
        p_throws        TEXT,
        ip              REAL,
        era             REAL,
        hr_per_9        REAL,
        k_per_9         REAL,
        whip            REAL,
        hard_hit_pct    REAL,
        fetched_at      TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (pitcher_id, season)
    );

    -- ================================================================
    -- Model picks with all factor scores (generate_picks writes here)
    -- ================================================================
    CREATE TABLE IF NOT EXISTS daily_picks (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        date            TEXT NOT NULL,
        batter_id       INTEGER NOT NULL,
        batter_name     TEXT,
        team            TEXT,
        tier            INTEGER,
        tier_label      TEXT,
        game_pk         INTEGER,
        opp_pitcher     TEXT,
        opp_pitcher_id  INTEGER,
        composite       REAL,
        power_score     REAL,
        matchup_score   REAL,
        matchup_version TEXT,           -- 'v1' or 'v2'
        park_score      REAL,
        form_score      REAL,
        weather_score   REAL,
        lineup_score    REAL,
        batting_order   TEXT,           -- 1-9, 'bench', 'roster_only'
        archetype_sim   REAL,           -- NULL if v1
        vulnerability   REAL,           -- NULL if v1
        weight_config   TEXT,
        selected        INTEGER DEFAULT 0,  -- 1 if in final 8-pick card
        rank_in_board   INTEGER,
        mode            TEXT,               -- 'live' / 'offline_simulation' (added 2026-05-03)
        created_at      TEXT DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_picks_date
        ON daily_picks(date);
    CREATE INDEX IF NOT EXISTS idx_picks_selected
        ON daily_picks(date, selected);

    -- ================================================================
    -- Actual outcomes (overnight ETL after games finish)
    -- ================================================================
    CREATE TABLE IF NOT EXISTS outcomes (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        date            TEXT NOT NULL,
        batter_id       INTEGER NOT NULL,
        batter_name     TEXT,
        game_pk         INTEGER NOT NULL,
        ab              INTEGER,
        hits            INTEGER,
        hr_count        INTEGER DEFAULT 0,
        rbi             INTEGER,
        doubles         INTEGER DEFAULT 0,
        triples         INTEGER DEFAULT 0,
        total_bases     INTEGER DEFAULT 0,
        hit             INTEGER GENERATED ALWAYS AS (hr_count > 0) STORED,
        fetched_at      TEXT DEFAULT (datetime('now')),
        UNIQUE(date, batter_id, game_pk)
    );

    CREATE INDEX IF NOT EXISTS idx_outcomes_date
        ON outcomes(date);

    -- ================================================================
    -- Per-HR Statcast events (overnight ETL, after games finish).
    -- Distinct from `outcomes`, which aggregates to (date, batter, game)
    -- with a single hr_count column. This table stores ONE ROW PER HR
    -- with full Statcast detail — coordinates, launch metrics, trajectory,
    -- pitcher attribution. Powers the diamond SVG + per-HR stats in the
    -- dashboard's Topps card modal.
    --
    -- Populated by etl_outcomes.fetch_hr_events_for_date() which mirrors
    -- the live worker's extractHRs() in workers/live-hr/src/index.js.
    -- The live worker writes today's HRs to KV (36h TTL); this table is
    -- the persistent counterpart for past days. Backfill via
    -- backfill_hr_events.py.
    -- ================================================================
    CREATE TABLE IF NOT EXISTS hr_events (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        date                TEXT NOT NULL,
        game_pk             INTEGER NOT NULL,
        at_bat_index        INTEGER,    -- play.about.atBatIndex; stable per-game
        batter_id           INTEGER NOT NULL,
        batter_name         TEXT,
        batting_team        TEXT,
        pitcher_id          INTEGER,
        pitcher_name        TEXT,
        pitching_team       TEXT,
        inning              INTEGER,
        half_inning         TEXT,        -- 'top' or 'bottom'
        play_time           TEXT,        -- ISO 8601 (about.endTime)
        -- Statcast hitData (any can be NULL when Statcast didn't track)
        launch_speed        REAL,
        launch_angle        REAL,
        total_distance      INTEGER,
        coord_x             REAL,        -- spray-chart X, ~0–250
        coord_y             REAL,        -- spray-chart Y, ~0–250 (low = deep OF)
        trajectory          TEXT,        -- 'fly_ball' / 'line_drive' / 'popup'
        location            TEXT,        -- '7'/'8'/'9'/'78'/'89' zone code
        hardness            TEXT,        -- 'hard' / 'medium' / 'soft'
        -- Game state at the moment of the HR
        home_score_after    INTEGER,
        away_score_after    INTEGER,
        description         TEXT,        -- result.description (broadcast call)
        venue               TEXT,
        fetched_at          TEXT DEFAULT (datetime('now')),
        UNIQUE(game_pk, at_bat_index)
    );

    -- Note: index names use `idx_hrevt_*` (not `idx_hr_events_*`) to
    -- avoid collision with the existing `batter_hr_events` table's
    -- indexes (idx_hr_events_batter / idx_hr_events_pitcher /
    -- idx_hr_events_date) defined above. SQLite's CREATE INDEX IF NOT
    -- EXISTS no-ops on the conflict, so reusing those names would
    -- silently leave this table un-indexed.
    CREATE INDEX IF NOT EXISTS idx_hrevt_date
        ON hr_events(date);
    CREATE INDEX IF NOT EXISTS idx_hrevt_batter
        ON hr_events(batter_id);
    CREATE INDEX IF NOT EXISTS idx_hrevt_game
        ON hr_events(game_pk);

    -- ================================================================
    -- Raw factor inputs per (date, batter) — written by load_picks_to_db
    -- after generate_picks emits them. Lets the dashboard's per-factor
    -- decomposition charts compare HR hitters vs misses on each underlying
    -- input (e.g., did the HR hitter have high xwoba_contact, or low?).
    -- ================================================================
    CREATE TABLE IF NOT EXISTS pick_inputs (
        date            TEXT NOT NULL,
        batter_id       INTEGER NOT NULL,

        -- Power inputs
        barrel_pct              REAL,
        exit_velo               REAL,
        hr_fb_pct               REAL,
        iso                     REAL,
        xwoba_contact           REAL,
        pull_fb_pct             REAL,

        -- Form inputs
        recent_hr_14d           REAL,    -- legacy proxies (pre-2026-05-19),
        recent_barrel_pct_14d   REAL,    -- retained for historical rows only
        ev_trend_14d            REAL,
        -- Form rebuild 2026-05-19: split game-count windows, honest names.
        -- recent_barrel_pct_14d was min(25, recent_ISO*100); ev_trend_14d was
        -- (recent_SLG - season_SLG)*30 -- these new columns replace them.
        recent_hr_10g           REAL,    -- HR over the last ~10 games
        recent_iso_30g          REAL,    -- ISO over the last ~30 games
        recent_avg_30g          REAL,    -- AVG over the last ~30 games
        recent_window_days      INTEGER, -- calendar span of the 30-game window
        ev_trend                REAL,    -- real EV trend vs season (Phase 2)

        -- Power rebuild B6a (2026-05-21): rolling 14-day quality-contact
        -- inputs from bulk Statcast pitch-level data. Fed into score_power
        -- when USE_RECENT_STATCAST_BLEND is on; otherwise stored only for
        -- backfill / refit observation. *_real_* suffix distinguishes from
        -- the legacy synthetic recent_barrel_pct_14d above (which was
        -- min(25, recent_ISO*100), kept for historical rows only).
        recent_barrel_real_14d   REAL,   -- real Statcast barrel% (events / batted balls)
        recent_xwoba_contact_14d REAL,   -- mean est_woba_using_speedangle on contact
        recent_iso_14d           REAL,   -- (TB - H) / AB in window

        -- Matchup: pitcher inputs
        pitcher_hr_per_9        REAL,
        pitcher_era             REAL,
        pitcher_hh_pct          REAL,
        pitcher_k_per_9         REAL,
        pitcher_fb_pct_allowed  REAL,
        -- Pitcher recency (added 2026-05-13): rolling 21-day HR/9 + start
        -- count from MLB API gameLog. Blended 60/40 with pitcher_hr_per_9
        -- inside score_pitcher_vulnerability when starts_21d >= 2; persisted
        -- here so the next refit can learn its own coefficient instead of
        -- inheriting season HR/9's standardized weight (form 0.496,
        -- matchup 0.468 from the 2026-05-01 refit baseline).
        --
        -- B4 (2026-05-21): the _21d suffix is retained for backward compat
        -- with the prior schema. The value reflects the *configured*
        -- recency window (pitcher_profile.PITCHER_RECENT_WINDOW_TYPE +
        -- PITCHER_RECENT_WINDOW_N), which may be last-N-starts post-
        -- backtest. recent_era + recent_k9 added — same gameLog payload,
        -- aligns the whole pitcher pipeline on consistent recent windows.
        pitcher_recent_hr9_21d    REAL,
        pitcher_recent_starts_21d INTEGER,
        pitcher_recent_era_21d    REAL,
        pitcher_recent_k9_21d     REAL,

        -- Matchup: batter + game inputs
        woba_vs_hand            REAL,
        archetype_similarity    REAL,
        -- Vegas team total (renamed 2026-05-03; see migration block below)
        --   *_pct = slate-relative percentile rank 0-100 (this is what feeds
        --           the matchup score; the column was misleadingly named
        --           `vegas_implied_total` for months because the field was
        --           originally a raw run total)
        --   *_raw = the actual Vegas implied team total in runs (4.5, 5.2,
        --           etc.); was JSON-only previously, now persisted
        vegas_team_total_pct    REAL,
        vegas_team_total_raw    REAL,
        platoon_advantage       INTEGER,        -- 0/1

        -- Park
        hr_park_factor          REAL,

        -- Weather
        temperature_f           REAL,
        wind_mph                REAL,
        wind_direction_deg      INTEGER,
        humidity_pct            REAL,
        is_dome                 INTEGER,        -- 0/1

        -- Lineup (already in daily_picks but mirrored here for self-contained queries)
        batting_order           INTEGER,        -- 1-9 only; null if not a starter

        -- Handedness (added 2026-05-03 — diagnostic value for platoon analysis)
        bats                    TEXT,           -- 'L' / 'R' / 'S'
        throws                  TEXT,           -- 'L' / 'R' (the opposing pitcher)

        -- Provenance (added 2026-05-03)
        weather_source          TEXT,           -- 'open_meteo' / 'dome_default' /
                                                -- 'coords_missing_default' / 'api_failed_default'
        barrel_pct_source       TEXT,           -- 'statcast' / 'synthetic_hr_per_pa' /
                                                -- 'season_batting_fallback' / 'career_shrunk'

        -- Lineup provenance (added 2026-05-04, after PR #33's recent-lineup
        -- fallback shipped). Tells us where the batter's batting_order
        -- came from so the dashboard can flag stale rows:
        --   'posted'            — statsapi posted lineup for today (canonical)
        --   'recent:YYYY-MM-DD' — team's last posted lineup before today
        --                        (PR #33 fallback when today's not yet posted)
        --   'roster_fallback'   — bdfed alphabetical roster (last resort,
        --                        batting_order will be NULL)
        lineup_source           TEXT,

        fetched_at              TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (date, batter_id)
    );

    CREATE INDEX IF NOT EXISTS idx_pick_inputs_date
        ON pick_inputs(date);

    -- ================================================================
    -- Park HR factors with L/R handedness splits (nightly ETL, weekly refresh)
    -- ================================================================
    CREATE TABLE IF NOT EXISTS park_factors (
        venue           TEXT NOT NULL,
        season          INTEGER NOT NULL,
        hr_pf_overall   REAL NOT NULL,      -- Overall HR factor (100 = neutral)
        hr_pf_lhb       REAL NOT NULL,      -- HR factor for left-handed batters
        hr_pf_rhb       REAL NOT NULL,      -- HR factor for right-handed batters
        source          TEXT,               -- 'seed', 'savant', 'fangraphs'
        notes           TEXT,
        fetched_at      TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (venue, season)
    );

    -- ================================================================
    -- Historical calibration (prior-season backfill for environmental
    -- factor diagnostics). Populated by etl/historical_calibration.py.
    -- ================================================================
    CREATE TABLE IF NOT EXISTS historical_batter_games (
        date         TEXT NOT NULL,
        game_pk      INTEGER NOT NULL,
        batter_id    INTEGER NOT NULL,
        home_team    TEXT,
        season       INTEGER NOT NULL,
        hr_count     INTEGER DEFAULT 0,
        pa_count     INTEGER DEFAULT 0,
        PRIMARY KEY (date, game_pk, batter_id)
    );

    CREATE INDEX IF NOT EXISTS idx_historical_bg_season
        ON historical_batter_games(season);

    CREATE TABLE IF NOT EXISTS historical_game_weather (
        date            TEXT NOT NULL,
        venue           TEXT NOT NULL,
        home_team       TEXT,
        season          INTEGER NOT NULL,
        temperature_f   REAL,
        wind_mph        REAL,
        wind_dir_deg    REAL,
        humidity_pct    REAL,
        dome            INTEGER DEFAULT 0,
        fetched_at      TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (date, venue)
    );

    CREATE INDEX IF NOT EXISTS idx_historical_gw_season
        ON historical_game_weather(season);

    -- Materialized join (built by build_historical_calibration_table).
    -- Read by export_site_data._temp_humidity_heatmap_historical and
    -- related diagnostics.
    CREATE TABLE IF NOT EXISTS historical_calibration (
        date            TEXT NOT NULL,
        game_pk         INTEGER NOT NULL,
        batter_id       INTEGER NOT NULL,
        home_team       TEXT,
        venue           TEXT,
        season          INTEGER NOT NULL,
        temperature_f   REAL,
        wind_mph        REAL,
        wind_dir_deg    REAL,
        humidity_pct    REAL,
        dome            INTEGER,
        hr_count        INTEGER DEFAULT 0,
        pa_count        INTEGER DEFAULT 0,
        PRIMARY KEY (date, game_pk, batter_id)
    );

    CREATE INDEX IF NOT EXISTS idx_historical_cal_season
        ON historical_calibration(season);

    -- ================================================================
    -- ETL run log (tracks freshness)
    -- ================================================================
    CREATE TABLE IF NOT EXISTS etl_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        job             TEXT NOT NULL,       -- 'nightly', 'morning', 'outcomes'
        date            TEXT NOT NULL,       -- date the ETL ran for
        status          TEXT NOT NULL,       -- 'started', 'completed', 'failed'
        detail          TEXT,
        started_at      TEXT DEFAULT (datetime('now')),
        completed_at    TEXT,
        rows_affected   INTEGER DEFAULT 0,
        error           TEXT
    );
    """)

    # Idempotent migrations for existing DBs that pre-date a column add.
    # SQLite ALTER TABLE doesn't support IF NOT EXISTS, so PRAGMA-check first.
    existing_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(outcomes)").fetchall()
    }
    for col, ddl in [
        ("doubles",     "ALTER TABLE outcomes ADD COLUMN doubles INTEGER DEFAULT 0"),
        ("triples",     "ALTER TABLE outcomes ADD COLUMN triples INTEGER DEFAULT 0"),
        ("total_bases", "ALTER TABLE outcomes ADD COLUMN total_bases INTEGER DEFAULT 0"),
    ]:
        if col not in existing_cols:
            conn.execute(ddl)

    # Migration for existing DBs: add etl_log.error column if missing
    existing_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(etl_log)").fetchall()
    }
    if 'error' not in existing_cols:
        try:
            conn.execute("ALTER TABLE etl_log ADD COLUMN error TEXT")
        except Exception:
            pass

    # 2026-05-03 schema-cleanup PR: mode column on daily_picks
    existing_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(daily_picks)").fetchall()
    }
    if 'mode' not in existing_cols:
        try:
            conn.execute("ALTER TABLE daily_picks ADD COLUMN mode TEXT")
        except Exception:
            pass

    # 2026-05-03 schema-cleanup PR: bats/throws + weather_source/barrel_pct_source
    # on pick_inputs. All four are NULL-safe additive columns; older rows
    # stay NULL until they're regenerated by a future load_picks_to_db run.
    existing_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(pick_inputs)").fetchall()
    }
    for col, ddl in [
        ("bats",              "ALTER TABLE pick_inputs ADD COLUMN bats TEXT"),
        ("throws",            "ALTER TABLE pick_inputs ADD COLUMN throws TEXT"),
        ("weather_source",    "ALTER TABLE pick_inputs ADD COLUMN weather_source TEXT"),
        ("barrel_pct_source", "ALTER TABLE pick_inputs ADD COLUMN barrel_pct_source TEXT"),
        # 2026-05-04: lineup_source — flags where batting_order came from
        # (posted / recent:DATE / roster_fallback). See pick_inputs CREATE
        # block for the column comment.
        ("lineup_source",     "ALTER TABLE pick_inputs ADD COLUMN lineup_source TEXT"),
    ]:
        if col not in existing_cols:
            try:
                conn.execute(ddl)
            except Exception:
                pass

    # 2026-05-03 schema-cleanup PR: rename pick_inputs.vegas_implied_total ->
    # vegas_team_total_pct (the column was misnamed for months — it stores a
    # 0-100 percentile, not a Vegas run total) + add vegas_team_total_raw to
    # persist the actual run total which had been JSON-only. SQLite's
    # ALTER TABLE RENAME COLUMN is supported in 3.25+ (Sep 2018).
    existing_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(pick_inputs)").fetchall()
    }
    if ('vegas_implied_total' in existing_cols
            and 'vegas_team_total_pct' not in existing_cols):
        try:
            conn.execute(
                "ALTER TABLE pick_inputs RENAME COLUMN "
                "vegas_implied_total TO vegas_team_total_pct"
            )
        except Exception:
            pass
        existing_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(pick_inputs)").fetchall()
        }
    if 'vegas_team_total_raw' not in existing_cols:
        try:
            conn.execute(
                "ALTER TABLE pick_inputs ADD COLUMN vegas_team_total_raw REAL"
            )
        except Exception:
            pass

    # 2026-05-13: pitcher recency columns — rolling 21-day HR/9 and start
    # count from MLB API gameLog. NULL-safe additive; older rows stay NULL
    # until rerun against the new pipeline. See pick_inputs CREATE block
    # comment for the blend semantics.
    #
    # B4 (2026-05-21): added recent_era + recent_k9. The _21d suffix is
    # retained for backward compat; the actual window is configurable via
    # pitcher_profile.PITCHER_RECENT_WINDOW_TYPE + PITCHER_RECENT_WINDOW_N.
    existing_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(pick_inputs)").fetchall()
    }
    for col, ddl in [
        ("pitcher_recent_hr9_21d",
         "ALTER TABLE pick_inputs ADD COLUMN pitcher_recent_hr9_21d REAL"),
        ("pitcher_recent_starts_21d",
         "ALTER TABLE pick_inputs ADD COLUMN pitcher_recent_starts_21d INTEGER"),
        ("pitcher_recent_era_21d",
         "ALTER TABLE pick_inputs ADD COLUMN pitcher_recent_era_21d REAL"),
        ("pitcher_recent_k9_21d",
         "ALTER TABLE pick_inputs ADD COLUMN pitcher_recent_k9_21d REAL"),
    ]:
        if col not in existing_cols:
            try:
                conn.execute(ddl)
            except Exception:
                pass

    # 2026-05-19: Form factor rebuild -- split game-count windows. The old
    # recent_*_14d columns were mislabeled proxies (recent_barrel_pct_14d was
    # min(25, recent_ISO*100); ev_trend_14d a SLG delta). New honest columns;
    # old ones retained for historical rows. NULL-safe additive.
    existing_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(pick_inputs)").fetchall()
    }
    for col, ddl in [
        ("recent_hr_10g",
         "ALTER TABLE pick_inputs ADD COLUMN recent_hr_10g REAL"),
        ("recent_iso_30g",
         "ALTER TABLE pick_inputs ADD COLUMN recent_iso_30g REAL"),
        ("recent_avg_30g",
         "ALTER TABLE pick_inputs ADD COLUMN recent_avg_30g REAL"),
        ("recent_window_days",
         "ALTER TABLE pick_inputs ADD COLUMN recent_window_days INTEGER"),
        ("ev_trend",
         "ALTER TABLE pick_inputs ADD COLUMN ev_trend REAL"),
    ]:
        if col not in existing_cols:
            try:
                conn.execute(ddl)
            except Exception:
                pass

    # 2026-05-20: B8 -- season-HR floor decoupled from MLB-API lag.
    # The /api/v1/stats?byDateRange endpoint that _fetch_season_batting_splits
    # uses lags HR aggregation by ~3 days while updating games count
    # immediately. Producing wrong floor tiers (Burger 8 HR scored
    # power_score=50 instead of 60 on 2026-05-20). Source the floor from
    # outcomes-cumulative HR instead, and persist into pick_inputs so
    # backtest_factors.rescore_row can apply the floor consistently.
    existing_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(pick_inputs)").fetchall()
    }
    for col, ddl in [
        ("season_hr",
         "ALTER TABLE pick_inputs ADD COLUMN season_hr INTEGER"),
    ]:
        if col not in existing_cols:
            try:
                conn.execute(ddl)
            except Exception:
                pass

    # 2026-05-21: B6a -- recent quality-contact blend for score_power.
    # Three rolling 14-day inputs sourced from bulk Statcast pitch-level
    # data (no per-player Statcast calls -- those hung the noon run
    # 2026-04-29). Populated by features_v2.fetch_batter_recent_statcast_14d,
    # which is as_of_date-aware so the 2025-season backfill can target
    # historical dates without look-ahead bias. NULL-safe additive;
    # score_power skips on None so flag-off behavior is unchanged.
    #
    # *recent_barrel_real_14d* has the _real_ suffix to distinguish from
    # the dead legacy *recent_barrel_pct_14d* column above, which was
    # min(25, recent_ISO*100) -- a synthetic proxy retained only for
    # historical rows.
    existing_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(pick_inputs)").fetchall()
    }
    for col, ddl in [
        ("recent_barrel_real_14d",
         "ALTER TABLE pick_inputs ADD COLUMN recent_barrel_real_14d REAL"),
        ("recent_xwoba_contact_14d",
         "ALTER TABLE pick_inputs ADD COLUMN recent_xwoba_contact_14d REAL"),
        ("recent_iso_14d",
         "ALTER TABLE pick_inputs ADD COLUMN recent_iso_14d REAL"),
        # B12 (2026-05-25): wider real-Statcast windows. Backtest-only for
        # now -- nightly ETL still populates only the _14d columns. If the
        # 21d/28d variant wins the backtest, the nightly fetcher gets wired
        # to populate these too.
        ("recent_barrel_real_21d",
         "ALTER TABLE pick_inputs ADD COLUMN recent_barrel_real_21d REAL"),
        ("recent_xwoba_contact_21d",
         "ALTER TABLE pick_inputs ADD COLUMN recent_xwoba_contact_21d REAL"),
        ("recent_iso_21d",
         "ALTER TABLE pick_inputs ADD COLUMN recent_iso_21d REAL"),
        ("recent_barrel_real_28d",
         "ALTER TABLE pick_inputs ADD COLUMN recent_barrel_real_28d REAL"),
        ("recent_xwoba_contact_28d",
         "ALTER TABLE pick_inputs ADD COLUMN recent_xwoba_contact_28d REAL"),
        ("recent_iso_28d",
         "ALTER TABLE pick_inputs ADD COLUMN recent_iso_28d REAL"),
    ]:
        if col not in existing_cols:
            try:
                conn.execute(ddl)
            except Exception:
                pass

    conn.commit()


# ---------------------------------------------------------------------------
# Helper queries used by multiple ETL scripts
# ---------------------------------------------------------------------------

def get_latest_hr_date(conn: sqlite3.Connection, batter_id: int) -> str | None:
    """Get the most recent game_date for a batter's HR events."""
    row = conn.execute(
        "SELECT MAX(game_date) FROM batter_hr_events WHERE batter_id = ?",
        (batter_id,)
    ).fetchone()
    return row[0] if row and row[0] else None


def get_park_factors_age_days(conn: sqlite3.Connection, season: int):
    """How many days old is the park_factors data for this season? None if missing."""
    row = conn.execute(
        "SELECT CAST(julianday('now') - julianday(MIN(fetched_at)) AS INTEGER) "
        "FROM park_factors WHERE season = ?",
        (season,)
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def get_arsenal_age_days(conn, pitcher_id, season):
    """How many days old is the pitcher_arsenals data for one pitcher+season? None if missing."""
    row = conn.execute(
        "SELECT CAST(julianday('now') - julianday(fetched_at) AS INTEGER) "
        "FROM pitcher_arsenals WHERE pitcher_id = ? AND season = ?",
        (pitcher_id, season),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def log_etl_start(conn, job, date_str):
    """Insert a row into etl_log marking the start of a job. Returns the row id."""
    cur = conn.execute(
        "INSERT INTO etl_log (job, date, status, started_at) "
        "VALUES (?, ?, 'running', datetime('now'))",
        (job, date_str),
    )
    conn.commit()
    return cur.lastrowid


def log_etl_complete(conn, log_id, rows=0, detail=""):
    """Mark a previously-started etl_log row as completed."""
    conn.execute(
        "UPDATE etl_log SET status='completed', completed_at=datetime('now'), "
        "rows_affected=?, error=? WHERE id=?",
        (rows, detail, log_id),
    )
    conn.commit()


def log_etl_fail(conn, log_id, error_msg):
    """Mark a previously-started etl_log row as failed."""
    conn.execute(
        "UPDATE etl_log SET status='failed', completed_at=datetime('now'), "
        "error=? WHERE id=?",
        (str(error_msg)[:500], log_id),
    )
    conn.commit()
