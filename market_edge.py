#!/usr/bin/env python3
"""
market_edge.py — B40: does the model beat the book's price? (2026-09-11)

Why. A hit-rate target mirrors the market: the book prices the top HR
hitters at +200-280 and priced our own picks at +285-450 on the first days
of capture, so "8 most likely to homer" converges on the favourites at the
favourites' odds. The only claim worth retaining is model probability above
the de-vigged book probability on the picks we make, scored as ROI at the
captured line and closing-line value. This module turns hr_prop_odds +
daily_picks + outcomes into exactly that, for performance.json.

What it computes (all live rows, dates with outcomes only):

  book_prob   de-vigged probability from the captured Over line. BetRivers
              posts the Over only through the-odds-api, so a one-sided line
              is divided by HR_PROP_OVERROUND (documented assumption; when
              an Under is captured the proportional two-way method is used).
  model_prob  Platt-scaled composite: P(HR) = sigmoid(a * composite + b),
              fit on every live confirmed starter since CALIB_SINCE with an
              outcome (no odds needed, so the sample is ~20k rows).
  card        the published 8 (final selected=1) that had a price: units
              P/L at 1 unit per pick at the noon line (afternoon if noon is
              missing), ROI, hit rate, mean book vs model probability.
  edge card   top-8 by (model_prob - book_prob) among priced confirmed
              starters, same rules as generate_picks (max 2 per game, no
              duplicate names, not likely-out) — what an edge-first
              selection would have bet, scored the same way.
  CLV         mean(book_prob at the afternoon snapshot - at noon) on card
              rows priced at both; positive = the market moved toward us.
  Brier       mean squared error of model_prob and of book_prob vs the
              outcome on every priced row — who is better calibrated.

Nothing here changes scoring or selection. It is a scoreboard.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta

import numpy as np

# One-sided Over lines carry the whole hold. A two-way HR prop at +300/-400
# is 0.25 + 0.80 = 1.05 of probability; the Over's fair share is 0.25/1.05.
# Books vary 1.05-1.10 on these; 1.07 is the middle and is applied only
# when no Under was captured. Revisit once two-way rows exist (see
# market_edge_two_way_share in the export).
HR_PROP_OVERROUND = 1.07
CALIB_SINCE = "2026-06-03"      # A1 weights live -> comparable composites
MAX_PER_GAME = 2
N_PICKS = 8
SNAPSHOT_OPEN = "noon"
SNAPSHOT_CLOSE = "afternoon"


# ---------------------------------------------------------------------------
# Odds arithmetic
# ---------------------------------------------------------------------------

def american_to_implied(price: int | float) -> float:
    price = float(price)
    if price > 0:
        return 100.0 / (price + 100.0)
    return -price / (-price + 100.0)


def devig(over_price, under_price=None, overround: float = HR_PROP_OVERROUND) -> float:
    """Fair P(Over). Two-way proportional when the Under exists, else a
    one-sided overround haircut."""
    po = american_to_implied(over_price)
    if under_price is not None:
        pu = american_to_implied(under_price)
        return po / (po + pu) if (po + pu) > 0 else po
    return po / overround


def unit_pl(price: int | float, hit: int) -> float:
    """P/L of 1 unit on the Over at an American price."""
    price = float(price)
    if not hit:
        return -1.0
    return price / 100.0 if price > 0 else 100.0 / -price


# ---------------------------------------------------------------------------
# Calibration: composite -> P(HR)
# ---------------------------------------------------------------------------

def fit_platt(x, y, iters: int = 30, l2: float = 1e-4) -> tuple[float, float]:
    """Logistic P = sigmoid(a*x + b) by Newton's method. Returns (a, b).
    x is scaled to composite/100 internally; the returned a is per raw
    composite point. Falls back to (0, logit(mean)) on a degenerate fit."""
    x = np.asarray(x, dtype=float) / 100.0
    y = np.asarray(y, dtype=float)
    if len(x) < 50 or y.sum() == 0 or y.sum() == len(y):
        p = min(max(float(y.mean()) if len(y) else 0.1, 1e-3), 1 - 1e-3)
        return 0.0, math.log(p / (1 - p))
    w = np.zeros(2)
    X = np.column_stack([x, np.ones_like(x)])
    for _ in range(iters):
        z = X @ w
        p = 1.0 / (1.0 + np.exp(-z))
        g = X.T @ (p - y) + l2 * w
        s = p * (1 - p)
        H = (X * s[:, None]).T @ X + l2 * np.eye(2)
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            break
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return float(w[0] / 100.0), float(w[1])


def model_prob(composite, a: float, b: float) -> float:
    if composite is None:
        return float("nan")
    z = a * float(composite) + b
    return 1.0 / (1.0 + math.exp(-z))


def calibration_table(x, y, a, b, edges=(0, 30, 40, 50, 55, 60, 65, 70, 75, 101)) -> list[dict]:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (x >= lo) & (x < hi)
        n = int(m.sum())
        if n == 0:
            continue
        pred = float(np.mean([model_prob(v, a, b) for v in x[m]]))
        out.append({"bin": f"{lo}-{hi - 1 if hi < 101 else 100}", "n": n,
                    "actual": round(float(y[m].mean()), 4), "predicted": round(pred, 4)})
    return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_training(conn: sqlite3.Connection, since: str = CALIB_SINCE) -> tuple[list, list]:
    rows = conn.execute(
        """
        SELECT dp.composite, CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END
        FROM daily_picks dp
        JOIN outcomes o ON o.date = dp.date AND o.batter_id = dp.batter_id
                        AND o.game_pk = dp.game_pk
        WHERE dp.date >= ? AND COALESCE(dp.mode, 'live') = 'live'
          AND CAST(dp.batting_order AS INTEGER) BETWEEN 1 AND 9
          AND o.ab > 0 AND dp.composite IS NOT NULL
        """,
        (since,),
    ).fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]


def load_priced_board(conn: sqlite3.Connection, since: str) -> list[dict]:
    """One row per (date, batter, game) that has an Over line, with the best
    (highest) Over price per snapshot across books, plus outcome fields."""
    rows = conn.execute(
        """
        WITH best AS (
            SELECT date, batter_id, snapshot,
                   MAX(CASE WHEN side = 'Over' THEN price_american END) AS over_price,
                   MAX(CASE WHEN side = 'Under' THEN price_american END) AS under_price,
                   COUNT(DISTINCT bookmaker) AS n_books
            FROM hr_prop_odds
            WHERE date >= ? AND market = 'batter_home_runs'
            GROUP BY date, batter_id, snapshot
        )
        SELECT dp.date, dp.batter_id, dp.batter_name, dp.game_pk, dp.composite,
               dp.selected, dp.batting_order, COALESCE(dp.is_likely_out, 0),
               o.hr_count, o.ab,
               op.over_price, op.under_price, cl.over_price, cl.under_price,
               COALESCE(op.n_books, cl.n_books)
        FROM daily_picks dp
        LEFT JOIN best op ON op.date = dp.date AND op.batter_id = dp.batter_id AND op.snapshot = ?
        LEFT JOIN best cl ON cl.date = dp.date AND cl.batter_id = dp.batter_id AND cl.snapshot = ?
        LEFT JOIN outcomes o ON o.date = dp.date AND o.batter_id = dp.batter_id
                             AND o.game_pk = dp.game_pk
        WHERE dp.date >= ? AND COALESCE(dp.mode, 'live') = 'live'
          AND (op.over_price IS NOT NULL OR cl.over_price IS NOT NULL)
        """,
        (since, SNAPSHOT_OPEN, SNAPSHOT_CLOSE, since),
    ).fetchall()
    out = []
    for r in rows:
        try:
            bo = int(r[6])
        except (TypeError, ValueError):
            bo = None
        out.append({
            "date": r[0], "batter_id": int(r[1]), "batter_name": r[2], "game_pk": r[3],
            "composite": r[4], "selected": int(r[5] or 0), "batting_order": bo,
            "is_likely_out": int(r[7] or 0),
            "hit": (1 if (r[8] or 0) > 0 else 0) if r[8] is not None else None,
            "played": 1 if (r[9] or 0) > 0 else 0,
            "open_over": r[10], "open_under": r[11],
            "close_over": r[12], "close_under": r[13],
            "n_books": r[14] or 0,
        })
    return out


# ---------------------------------------------------------------------------
# Pure computation (pinned in tests/smoke.py)
# ---------------------------------------------------------------------------

def _pick_edge_card(rows: list[dict], n_picks: int = N_PICKS, max_per_game: int = MAX_PER_GAME) -> list[dict]:
    """Top-N by edge among priced confirmed starters, production rules."""
    cands = [r for r in rows if r.get("batting_order") and 1 <= r["batting_order"] <= 9
             and not r.get("is_likely_out") and r.get("edge") is not None]
    cands.sort(key=lambda r: -r["edge"])
    out, names, per_game = [], set(), {}
    for r in cands:
        if len(out) >= n_picks:
            break
        if r["batter_name"] in names or per_game.get(r["game_pk"], 0) >= max_per_game:
            continue
        out.append(r)
        names.add(r["batter_name"])
        per_game[r["game_pk"]] = per_game.get(r["game_pk"], 0) + 1
    return out


def _score_card(rows: list[dict]) -> dict:
    """ROI block for a list of priced rows with outcomes."""
    n = len(rows)
    if n == 0:
        return {"n": 0, "hits": 0, "hit_rate": None, "units": 0.0, "roi": None,
                "mean_book_prob": None, "mean_model_prob": None}
    hits = sum(r["hit"] for r in rows)
    units = sum(unit_pl(r["price"], r["hit"]) for r in rows)
    return {
        "n": n, "hits": hits, "hit_rate": round(hits / n, 4),
        "units": round(units, 2), "roi": round(units / n, 4),
        "mean_book_prob": round(sum(r["book_prob"] for r in rows) / n, 4),
        "mean_model_prob": round(sum(r["model_prob"] for r in rows) / n, 4),
    }


def compute_from_rows(train_x, train_y, rows: list[dict], overround: float = HR_PROP_OVERROUND,
                      recent_days: int = 30) -> dict:
    a, b = fit_platt(train_x, train_y)
    # price each row: open (noon) if present, else close; book/model prob; edge
    priced = []
    for r in rows:
        over = r.get("open_over") if r.get("open_over") is not None else r.get("close_over")
        under = r.get("open_under") if r.get("open_over") is not None else r.get("close_under")
        if over is None or r.get("composite") is None:
            continue
        q = dict(r)
        q["price"] = over
        q["price_snapshot"] = SNAPSHOT_OPEN if r.get("open_over") is not None else SNAPSHOT_CLOSE
        q["book_prob"] = devig(over, under, overround)
        q["model_prob"] = model_prob(r["composite"], a, b)
        q["edge"] = q["model_prob"] - q["book_prob"]
        if r.get("open_over") is not None and r.get("close_over") is not None:
            q["clv"] = devig(r["close_over"], r.get("close_under"), overround) - devig(r["open_over"], r.get("open_under"), overround)
        else:
            q["clv"] = None
        priced.append(q)

    scored = [q for q in priced if q.get("hit") is not None]
    by_date: dict[str, list[dict]] = {}
    for q in scored:
        by_date.setdefault(q["date"], []).append(q)

    days = []
    card_all, edge_all = [], []
    for d in sorted(by_date):
        rs = by_date[d]
        card = [q for q in rs if q["selected"] == 1]
        edge = _pick_edge_card(rs)
        card_all.extend(card)
        edge_all.extend(edge)
        clvs = [q["clv"] for q in card if q.get("clv") is not None]
        days.append({
            "date": d,
            "priced_rows": len(rs),
            "card": _score_card(card),
            "edge_card": _score_card(edge),
            "edge_card_names": [q["batter_name"] for q in edge],
            "clv": round(sum(clvs) / len(clvs), 4) if clvs else None,
            "overlap": len({q["batter_id"] for q in card} & {q["batter_id"] for q in edge}),
        })

    clv_all = [q["clv"] for q in card_all if q.get("clv") is not None]
    brier_model = (sum((q["model_prob"] - q["hit"]) ** 2 for q in scored) / len(scored)) if scored else None
    brier_book = (sum((q["book_prob"] - q["hit"]) ** 2 for q in scored) / len(scored)) if scored else None
    two_way = sum(1 for q in priced if (q.get("open_under") is not None or q.get("close_under") is not None))

    return {
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "assumptions": {
            "overround_one_sided": overround,
            "two_way_rows": two_way,
            "calibration": {"since": CALIB_SINCE, "n": len(train_x), "a": round(a, 5), "b": round(b, 4)},
            "price_used": "noon line, afternoon when noon missing; best Over across books",
        },
        "calibration_table": calibration_table(train_x, train_y, a, b) if len(train_x) else [],
        "n_days": len(days),
        "priced_rows_scored": len(scored),
        "card": _score_card(card_all),
        "edge_card": _score_card(edge_all),
        "clv": {"n": len(clv_all), "mean": round(sum(clv_all) / len(clv_all), 4) if clv_all else None},
        "brier": {"model": round(brier_model, 4) if brier_model is not None else None,
                  "book": round(brier_book, 4) if brier_book is not None else None,
                  "n": len(scored)},
        "days": days[-recent_days:],
    }


def compute_market_edge(conn: sqlite3.Connection, days: int = 60) -> dict:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        x, y = load_training(conn)
        rows = load_priced_board(conn, since)
    except sqlite3.OperationalError as e:
        return {"error": f"schema: {e}", "n_days": 0}
    return compute_from_rows(x, y, rows)


if __name__ == "__main__":  # ad-hoc: python market_edge.py
    import json
    from etl.db import get_db
    conn = get_db()
    print(json.dumps(compute_market_edge(conn), indent=1)[:4000])
