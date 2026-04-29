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
        hit             INTEGER GENERATED ALWAYS AS (hr_count > 0) STORED,
        fetched_at      TEXT DEFAULT (datetime('now')),
        UNIQUE(date, batter_id, game_pk)
    );

    CREATE INDEX IF NOT EXISTS idx_outcomes_date
        ON outcomes(date);

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
        rows_affected   INTEGER DEFAULT 0
    );
    """)

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


def get_park_factors_age_days(conn: sqlite3.Connection, season: int) -> int | None:
    """How many days old is the park_factors data for this season? None if missing."""
    row = conn.execute(
        "SELECT CAST(julianday('now') - julianday(MIN(fetched_at)) AS INTEGER) "
        "FROM park_factors WHERE season = ?",
        (season,)
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def get_arsenal_age_days(conn: sqlite3.Connection, pitcher_id: int, season: int) -> int | None:
    """How many days old is this pitcher's arsenal data? None if missing."""
    row = conn.execute(
        "SELECT CAST(julianday('now') - julianday(fetched_at) AS INTEGER) "
        "FROM pitcher_arsenals WHERE pitcher_id = ? AND season = ?",
        (pitcher_id, season)
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def log_etl_start(conn: sqlite3.Connection, job: str, date: str) -> int:
    """Log ETL job start. Returns the log row id."""
    cur = conn.execute(
        "INSERT INTO etl_log (job, date, status) VALUES (?, ?, 'started')",
        (job, date)
    )
    conn.commit()
    return cur.lastrowid


def log_etl_complete(conn: sqlite3.Connection, log_id: int, rows: int = 0, detail: str = ""):
    """Mark ETL job as completed."""
    conn.execute(
        "UPDATE etl_log SET status='completed', completed_at=datetime('now'), "
        "rows_affected=?, detail=? WHERE id=?",
        (rows, detail, log_id)
    )
    conn.commit()


def log_etl_fail(conn: sqlite3.Connection, log_id: int, detail: str = ""):
    """Mark ETL job as failed."""
    conn.execute(
        "UPDATE etl_log SET status='failed', completed_at=datetime('now'), "
        "detail=? WHERE id=?",
        (detail, log_id)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# CLI — create / inspect the database
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Manage the HR Bets database")
    parser.add_argument("--create", action="store_true", help="Create all tables")
    parser.add_argument("--stats", action="store_true", help="Show table row counts")
    parser.add_argument("--db", default=None, help="Custom DB path")
    args = parser.parse_args()

    db = get_db(args.db)

    if args.create:
        create_tables(db)
        print(f"Database created at {DB_PATH}")

    if args.stats or args.create:
        tables = [
            "batter_hr_events", "pitcher_arsenals", "victim_profiles",
            "daily_slate", "daily_lineup", "season_batting", "season_pitching",
            "park_factors", "daily_picks", "outcomes", "etl_log",
        ]
        print(f"\n  DATABASE: {DB_PATH}")
        print(f"  {'Table':<24} {'Rows':>8}")
        print(f"  {'-' * 34}")
        for t in tables:
            try:
                count = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                print(f"  {t:<24} {count:>8}")
            except sqlite3.OperationalError:
                print(f"  {t:<24} {'(missing)':>8}")
        print()

    db.close()
