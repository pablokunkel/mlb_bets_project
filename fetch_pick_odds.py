#!/usr/bin/env python3
"""
fetch_pick_odds.py — Capture the sportsbook HR-prop price on each published pick.

B34. Snapshots DraftKings' `batter_home_runs` Over/Under price for every pick
on the day's card, at pick time, into `hr_prop_odds`. The data is
unrecoverable after the fact — the-odds-api's free tier has no usable
historical player-prop endpoint — so this runs every day the pipeline runs.

Two things this unlocks later:
  - real break-even per leg instead of assuming a flat +300
  - "did we beat the de-vigged market?" measured against the two-way price
    (which is why we store BOTH sides, not just the Over)

WRITE-ONLY for now. Nothing in scoring, selection, or the site export reads
`hr_prop_odds`. Do not wire it into generate_picks.py.

FAIL-SOFT BY CONSTRUCTION. This runs *after* picks are generated, loaded, and
pushed. An odds outage, an expired key, a rate limit, a dead network — none of
them may block or delay pick publishing. main() therefore catches everything
and exits 0 unless --strict is passed (which exists only so tests can assert
on real failures).

API cost (free tier: 500 credits/month):
  - GET /v4/sports/baseball_mlb/events            -> 0 credits
  - GET /v4/sports/.../events/{id}/odds           -> 1 credit per event
    (markets x regions = 1 x 1)
  We only hit the events that actually contain a pick, so ~4-8 credits/day.
  features_v2.fetch_vegas_implied_totals already spends ~2/day. Comfortable.

Usage:
    python fetch_pick_odds.py                        # today, canonical DB
    python fetch_pick_odds.py --date 2026-08-21
    python fetch_pick_odds.py --json path/to/picks.json
    python fetch_pick_odds.py --db custom.db
    python fetch_pick_odds.py --dry-run              # fetch + log, no writes
    python fetch_pick_odds.py --strict               # exit non-zero on failure
"""

import argparse
import json
import os
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from etl.db import get_db, create_tables, RESULTS_DIR, SITE_DATA_DIR
# features_v2 also runs the project's .env auto-loader at import, which is how
# VEGAS_ODDS_API_KEY gets picked up on a local run. Its heavy deps (pybaseball,
# pandas) are imported lazily inside functions, so this import stays cheap.
from features_v2 import ODDS_API_BASE, _team_to_abbrev

MARKET = "batter_home_runs"
DEFAULT_BOOKMAKER = "draftkings"
DEFAULT_SNAPSHOT = "noon"
REQUEST_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

# Suffixes the book and MLB's own feed disagree about ("Ronald Acuna Jr." vs
# "Ronald Acuna"). Stripped from both sides before comparison.
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize_name(name: str) -> str:
    """
    Fold a player name to a comparison key.

    Accents out (Julio Rodriguez), generational suffixes out (Ronald Acuna
    Jr. -> ronald acuna), whitespace collapsed.

    Periods and apostrophes are DELETED, hyphens become spaces. That split is
    deliberate: deleting the period folds "J.T. Realmuto" onto "JT Realmuto",
    while a hyphen has to become a space so "Ha-Seong Kim" folds onto
    "Ha Seong Kim". Turning periods into spaces instead would break the first
    pair; deleting hyphens would break the second.
    """
    if not name:
        return ""
    # NFKD splits an accented letter into base + combining mark; drop the marks.
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    cleaned = []
    for ch in folded.lower():
        if ch.isalnum():
            cleaned.append(ch)
        elif ch in " -":
            cleaned.append(" ")
        # periods, apostrophes, commas, quotes are dropped outright
    tokens = "".join(cleaned).split()
    while len(tokens) > 1 and tokens[-1] in _NAME_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _tight_key(normalized: str) -> str:
    """
    Space-free variant of a normalized name, used only as a second-chance
    lookup. Catches the residual initials disagreement ("J. T. Realmuto" ->
    "j t realmuto" vs "JT Realmuto" -> "jt realmuto"). Collision risk is nil:
    we only ever compare within one game's set of picks.
    """
    return normalized.replace(" ", "")


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _event_date_et(commence_time: str) -> str:
    """
    Map an event's UTC commence_time to its ET calendar date.

    Fixed UTC-4 (EDT). MLB's regular season runs April-October, which is
    entirely inside EDT — the same assumption .github/workflows/daily-picks.yml
    documents for its cron. First pitches span ~12:00-22:10 ET (16:00 UTC to
    02:10 UTC the next day), so a flat -4h assigns every one of them to the
    right calendar day.

    Returns "" if the timestamp is unparseable.
    """
    if not commence_time:
        return ""
    try:
        # the-odds-api returns "2026-08-21T23:10:00Z"; fromisoformat wants +00:00
        ts = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return (ts - timedelta(hours=4)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Pick loading
# ---------------------------------------------------------------------------

def load_picks_from_db(conn, date_str: str) -> list[dict]:
    """The selected picks for the date, straight out of daily_picks."""
    rows = conn.execute(
        """
        SELECT batter_id, batter_name, team, game_pk
        FROM daily_picks
        WHERE date = ? AND selected = 1
        ORDER BY rank_in_board
        """,
        (date_str,),
    ).fetchall()
    return [
        {
            "batter_id": int(r[0]),
            "batter_name": r[1] or "",
            "team": r[2] or "",
            "game_pk": r[3],
        }
        for r in rows
        if r[0]
    ]


def load_picks_from_json(path: Path, date_str: str | None = None) -> list[dict]:
    """
    The card out of a picks JSON. Handles both key spellings we produce:
    generate_picks.py writes player_id/name, export_site_data.py writes
    batter_id/batter_name.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if date_str and data.get("date") and data["date"] != date_str:
        raise ValueError(f"{path.name} is for {data['date']}, not {date_str}")
    out = []
    for p in data.get("picks", []):
        pid = p.get("batter_id") or p.get("player_id")
        if not pid:
            continue
        out.append({
            "batter_id": int(pid),
            "batter_name": p.get("batter_name") or p.get("name") or "",
            "team": p.get("team") or "",
            "game_pk": p.get("game_pk"),
        })
    return out


def resolve_picks(conn, date_str: str, explicit_json: str | None) -> list[dict]:
    """
    daily_picks first (that is the published card of record), picks JSON as a
    fallback so this stays runnable ad-hoc from a checkout whose DB is behind.
    """
    if explicit_json:
        return load_picks_from_json(Path(explicit_json), date_str)

    picks = load_picks_from_db(conn, date_str)
    if picks:
        return picks

    print(f"  [odds] no selected daily_picks rows for {date_str}; trying picks JSON")
    for candidate in (
        RESULTS_DIR / f"picks_{date_str}.json",
        SITE_DATA_DIR / "picks_latest.json",
    ):
        if not candidate.exists():
            continue
        try:
            picks = load_picks_from_json(candidate, date_str)
        except ValueError as e:
            print(f"  [odds] skipping {candidate.name}: {e}")
            continue
        if picks:
            print(f"  [odds] loaded {len(picks)} picks from {candidate}")
            return picks
    return []


def load_slate_matchups(conn, date_str: str) -> dict[int, tuple[str, str]]:
    """{game_pk: (home_abbrev, away_abbrev)} for the date. Empty on a stale DB."""
    out: dict[int, tuple[str, str]] = {}
    try:
        rows = conn.execute(
            "SELECT game_pk, home_team, away_team FROM daily_slate WHERE date = ?",
            (date_str,),
        ).fetchall()
    except Exception:
        return out
    for gpk, home, away in rows:
        h, a = _team_to_abbrev(home or ""), _team_to_abbrev(away or "")
        if h and a:
            out[int(gpk)] = (h, a)
    return out


# ---------------------------------------------------------------------------
# the-odds-api
# ---------------------------------------------------------------------------

def _log_quota(resp: requests.Response, label: str) -> None:
    """the-odds-api reports spend on every response via x-requests-* headers."""
    used = resp.headers.get("x-requests-used")
    remaining = resp.headers.get("x-requests-remaining")
    last = resp.headers.get("x-requests-last")
    print(
        f"  [odds] quota after {label}: used={used} remaining={remaining} "
        f"cost_of_this_call={last}"
    )


def fetch_events(api_key: str, date_str: str) -> list[dict]:
    """
    The listed MLB events, filtered to date_str (ET). Free — the events
    endpoint is not metered, which is why we list first and only pay for the
    events that actually contain a pick.
    """
    resp = requests.get(
        f"{ODDS_API_BASE}/sports/baseball_mlb/events",
        params={"apiKey": api_key},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    _log_quota(resp, "events list")
    events = resp.json()
    same_day = [
        e for e in events
        if _event_date_et(e.get("commence_time", "")) == date_str
    ]
    print(f"  [odds] {len(events)} MLB events listed, {len(same_day)} on {date_str} (ET)")
    return same_day


def fetch_event_odds(api_key: str, event_id: str, bookmaker: str) -> dict:
    """One event's HR props. Costs 1 credit (1 market x 1 region)."""
    resp = requests.get(
        f"{ODDS_API_BASE}/sports/baseball_mlb/events/{event_id}/odds",
        params={
            "apiKey": api_key,
            "regions": "us",
            "markets": MARKET,
            "bookmakers": bookmaker,
            "oddsFormat": "american",
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    _log_quota(resp, f"event {event_id}")
    return resp.json()


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match_events_to_picks(
    picks: list[dict],
    events: list[dict],
    slate: dict[int, tuple[str, str]],
) -> tuple[dict[str, list[dict]], list[dict]]:
    """
    Group picks by the-odds-api event id.

    Match on the (home, away) abbreviation pair when daily_slate knows the
    matchup, otherwise on the pick's own team abbreviation. Returns
    ({event_id: [picks]}, [picks with no event]).
    """
    by_pair: dict[tuple[str, str], dict] = {}
    by_team: dict[str, dict] = {}
    for ev in events:
        home = _team_to_abbrev(ev.get("home_team", ""))
        away = _team_to_abbrev(ev.get("away_team", ""))
        if not home or not away:
            print(
                f"  [odds] could not abbreviate event teams: "
                f"{ev.get('away_team')} @ {ev.get('home_team')}"
            )
            continue
        by_pair[(home, away)] = ev
        # Doubleheaders aside, a team appears once per slate. First listed wins.
        by_team.setdefault(home, ev)
        by_team.setdefault(away, ev)

    grouped: dict[str, list[dict]] = {}
    unmatched: list[dict] = []
    for pick in picks:
        gpk = pick.get("game_pk")
        ev = None
        if gpk is not None and int(gpk) in slate:
            ev = by_pair.get(slate[int(gpk)])
        if ev is None:
            ev = by_team.get(pick.get("team", ""))
        if ev is None:
            unmatched.append(pick)
            continue
        grouped.setdefault(ev["id"], []).append(pick)
    return grouped, unmatched


def extract_prices(
    payload: dict,
    picks: list[dict],
    bookmaker: str,
) -> tuple[list[dict], list[dict]]:
    """
    Pull each pick's Over/Under out of one event's odds payload.

    Returns (rows, picks_without_a_two_way_line). A pick with no posted line is
    normal at noon — books do not price every batter — so it is logged, never
    fatal.
    """
    by_key = {normalize_name(p["batter_name"]): p for p in picks}
    by_tight = {_tight_key(k): p for k, p in by_key.items()}
    found: dict[int, set[str]] = {}
    rows: list[dict] = []

    for bk in payload.get("bookmakers", []):
        bk_key = bk.get("key", "")
        if bk_key != bookmaker:
            continue
        for market in bk.get("markets", []):
            if market.get("key") != MARKET:
                continue
            for oc in market.get("outcomes", []):
                # In player-prop markets the-odds-api puts the player in
                # `description` and Over/Under in `name`.
                key = normalize_name(oc.get("description", ""))
                pick = by_key.get(key) or by_tight.get(_tight_key(key))
                if pick is None:
                    continue
                side = (oc.get("name") or "").strip().title()
                if side not in ("Over", "Under"):
                    continue
                price = oc.get("price")
                rows.append({
                    "batter_id": pick["batter_id"],
                    "batter_name": pick["batter_name"],
                    "game_pk": pick.get("game_pk"),
                    "event_id": payload.get("id"),
                    "bookmaker": bk_key,
                    "side": side,
                    "price_american": int(price) if price is not None else None,
                    "point": float(oc["point"]) if oc.get("point") is not None else None,
                })
                found.setdefault(pick["batter_id"], set()).add(side)

    missing = [p for p in picks if len(found.get(p["batter_id"], set())) < 2]
    return rows, missing


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def store_rows(conn, date_str: str, rows: list[dict], snapshot: str) -> int:
    """INSERT OR REPLACE so a re-run of the same snapshot is idempotent."""
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.executemany(
        """
        INSERT OR REPLACE INTO hr_prop_odds (
            date, batter_id, batter_name, game_pk, event_id,
            bookmaker, market, side, price_american, point,
            fetched_at, snapshot
        ) VALUES (?, ?, ?, ?, ?,  ?, ?, ?, ?, ?,  ?, ?)
        """,
        [
            (
                date_str, r["batter_id"], r["batter_name"], r["game_pk"],
                r["event_id"], r["bookmaker"], MARKET, r["side"],
                r["price_american"], r["point"], fetched_at, snapshot,
            )
            for r in rows
        ],
    )
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(
    date_str: str,
    db_path: str | None = None,
    explicit_json: str | None = None,
    bookmaker: str = DEFAULT_BOOKMAKER,
    snapshot: str = DEFAULT_SNAPSHOT,
    dry_run: bool = False,
) -> int:
    """Capture the day's prop odds. Returns the number of rows stored."""
    api_key = os.environ.get("VEGAS_ODDS_API_KEY")
    if not api_key:
        raise RuntimeError("VEGAS_ODDS_API_KEY not set - cannot fetch prop odds")

    conn = get_db(db_path)
    create_tables(conn)

    picks = resolve_picks(conn, date_str, explicit_json)
    if not picks:
        print(f"  [odds] no picks found for {date_str} - nothing to price")
        return 0
    print(f"  [odds] pricing {len(picks)} picks for {date_str} ({bookmaker}, {snapshot})")

    events = fetch_events(api_key, date_str)
    slate = load_slate_matchups(conn, date_str)
    grouped, no_event = match_events_to_picks(picks, events, slate)
    for p in no_event:
        print(
            f"  [odds] UNMATCHED (no event): {p['batter_name']} "
            f"({p['team']}, game_pk={p['game_pk']})"
        )
    print(f"  [odds] {len(grouped)} events contain picks - {len(grouped)} credits to spend")

    all_rows: list[dict] = []
    no_line: list[dict] = []
    for event_id, event_picks in grouped.items():
        try:
            payload = fetch_event_odds(api_key, event_id, bookmaker)
        except Exception as e:
            # One dead event must not cost us the other seven.
            print(f"  [odds] event {event_id} odds fetch failed: {e}")
            no_line.extend(event_picks)
            continue
        rows, missing = extract_prices(payload, event_picks, bookmaker)
        all_rows.extend(rows)
        no_line.extend(missing)

    for p in no_line:
        print(
            f"  [odds] UNMATCHED (no {bookmaker} {MARKET} line): "
            f"{p['batter_name']} ({p['team']})"
        )

    n_priced = len({r["batter_id"] for r in all_rows})
    if dry_run:
        print(f"  [odds] DRY RUN - would store {len(all_rows)} rows")
    elif all_rows:
        store_rows(conn, date_str, all_rows, snapshot)
        print(f"  [odds] stored {len(all_rows)} rows into hr_prop_odds")
    else:
        print("  [odds] no rows to store")

    print(
        f"  [odds] SUMMARY {date_str}: {n_priced}/{len(picks)} picks priced, "
        f"{len(all_rows)} rows, {len(no_event) + len(no_line)} unmatched"
    )
    return len(all_rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture DraftKings HR-prop odds for today's published picks."
    )
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--db", default=None, help="Path to hr_bets.db (default: canonical)")
    parser.add_argument("--json", default=None,
                        help="Read picks from this JSON instead of daily_picks")
    parser.add_argument("--bookmaker", default=DEFAULT_BOOKMAKER)
    parser.add_argument("--snapshot", default=DEFAULT_SNAPSHOT,
                        help="Snapshot label (default: noon; B34b will add a close snapshot)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and log, but do not write")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero on failure. Default is exit 0 always, so a "
                             "broken odds fetch can never fail the daily pipeline.")
    args = parser.parse_args()

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    try:
        run(
            date_str=date_str,
            db_path=args.db,
            explicit_json=args.json,
            bookmaker=args.bookmaker,
            snapshot=args.snapshot,
            dry_run=args.dry_run,
        )
    except Exception as e:
        # The whole point of this script is that it cannot break the pipeline.
        print(f"  [odds] FAILED (non-fatal): {type(e).__name__}: {e}")
        if args.strict:
            return 1
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
