#!/usr/bin/env python3
"""
run_backfill_local.py — Run the 2025-season backfill locally with R2 bookends.

What this does
--------------
1. Loads R2 credentials from .env (if present), then verifies they're set.
2. Pulls hr_bets.db from R2 → local data/ (overwriting any stale local copy).
3. Runs the etl.backfill_2025 orchestrator with whatever args you pass.
4. Pushes the updated DB back to R2 — ALWAYS, including after Ctrl+C or
   exceptions, so partial progress survives a graceful interrupt.

Why this is needed
------------------
Since the 2026-05-12 GH Actions cutover, R2 is the source of truth for the
production DB. A raw `python -m etl.backfill_2025` writes to the local
data/ copy, but the next scheduled job (daily-picks, outcomes-refresh,
nightly-refresh) pulls R2's copy and overwrites the local file. Backfill
work that lives only in the local DB disappears.

This wrapper ensures every local backfill session is bookended:
    R2 -> local  (pull)
    backfill runs (any duration, any number of chunks)
    local -> R2  (push)

So you can run a 4-hour chunk locally, kill it with Ctrl+C if needed, and
the partial progress lands in R2 before the wrapper exits.

Usage
-----
    # Same args as `python -m etl.backfill_2025`, just wrapped:
    python run_backfill_local.py --max-runtime 4h
    python run_backfill_local.py --start 2025-04-01 --end 2025-04-30 --max-dates 30

    # Outcomes prereq only (fast path; doesn't run the slate loop):
    python run_backfill_local.py --outcomes-only

Convention: matches run_daily.bat / run_outcomes.bat / run_nightly.bat —
which all bookend their local work with appropriate sync. This is the
backfill analog.

Required env vars (from .env or shell):
    R2_ACCOUNT_ID
    R2_ACCESS_KEY_ID
    R2_SECRET_ACCESS_KEY
    R2_BUCKET
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
PYTHON = sys.executable


# ---------------------------------------------------------------------------
# UTF-8 I/O hardening
# ---------------------------------------------------------------------------

def _force_utf8_io() -> None:
    """Make this wrapper and every child subprocess emit UTF-8.

    On Windows, when stdout is a pipe or a redirected file — you run
    `run_backfill_2025.bat > backfill.log 2>&1` — Python falls back to the
    legacy cp1252 console codec. The first non-ASCII char in any log line
    (an em-dash, an arrow) then crashes the run with UnicodeEncodeError. A
    6-12h backfill is exactly the job you want to tee to a file, so harden
    it at the root: PYTHONUTF8 / PYTHONIOENCODING go into os.environ (the
    r2_sync.py and etl.backfill_2025 subprocesses inherit them), and this
    wrapper's own streams are reconfigured directly. setdefault() respects
    any value the user already exported. No-op on a real console / Linux.
    """
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# .env loader (no python-dotenv dep)
# ---------------------------------------------------------------------------
# IMPORTANT — this DELIBERATELY differs from features_v2._load_dotenv().
#
# features_v2's loader uses the standard dotenv convention "existing env
# wins" (`if key not in os.environ`). That's correct for the daily
# pipeline on GH Actions, where R2_* / VEGAS_* are injected as real env
# vars from repo secrets and must beat any committed .env.
#
# This wrapper is laptop-only. Here, .env is the single source of truth
# for R2 credentials. The "env wins" rule is an active hazard: a stale
# `set R2_ACCESS_KEY_ID=...` left in a cmd session silently shadows .env,
# and the only symptom is a 403 that looks exactly like a dead token.
# That cost a debugging session on 2026-05-22.
#
# So: .env is AUTHORITATIVE. Every key in .env overrides whatever the
# shell handed us — and when an override actually changes a value, we
# say so out loud. A shadowed credential should never be silent again.

def _load_dotenv() -> None:
    """Load KEY=VALUE lines from .env into os.environ. .env always wins."""
    env_path = REPO / ".env"
    if not env_path.exists():
        print(f"  [WARN] no .env found at {env_path}", file=sys.stderr)
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if not key:
                continue
            prior = os.environ.get(key)
            if prior is not None and prior != val:
                # The exact failure mode we're guarding against — announce it.
                print(f"  [.env] {key}: .env value overrides a DIFFERENT value "
                      f"already in the shell environment (.env is authoritative)")
            os.environ[key] = val
    except Exception as e:
        print(f"  [WARN] .env parse failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Subprocess helpers — keep this wrapper itself crash-free by wrapping each
# subprocess in try/except. The push step is the load-bearing one; if it
# fails we want the user to know exactly what to retry.
# ---------------------------------------------------------------------------

def _r2(verb: str) -> int:
    """Run `python infra/r2_sync.py <verb>`. Returns exit code."""
    return subprocess.run(
        [PYTHON, str(REPO / "infra" / "r2_sync.py"), verb],
        cwd=str(REPO),
    ).returncode


def pull() -> None:
    """Pull DB from R2. Hard-fail on error — we don't want to run a
    backfill against a stale or missing local DB."""
    print("\n=== [1/3] Pulling hr_bets.db from R2 ===")
    rc = _r2("pull")
    if rc != 0:
        print(f"\nR2 pull failed (exit {rc}). Aborting before any local work "
              "to avoid local-vs-remote drift.", file=sys.stderr)
        sys.exit(rc)


def push() -> int:
    """Push local DB to R2. Soft-fail (return non-zero) so the caller can
    print a clear retry message. Local DB still has the work either way."""
    print("\n=== [3/3] Pushing hr_bets.db back to R2 ===")
    rc = _r2("push")
    if rc != 0:
        print(
            f"\n*** R2 PUSH FAILED (exit {rc}) ***\n"
            "Your local DB has the backfill work, but R2 does not.\n"
            "The next scheduled GH Actions job (daily-picks at 13:07 UTC,\n"
            "outcomes-refresh at 06:00 UTC, nightly-refresh at 08:00 UTC)\n"
            "WILL OVERWRITE your local DB with R2's old copy.\n"
            "\n"
            "Retry the push immediately:\n"
            f"    python infra/r2_sync.py push\n",
            file=sys.stderr,
        )
    return rc


def run_orchestrator(args: list[str]) -> int:
    """Run `python -m etl.backfill_2025 <args>`. Ctrl+C is propagated to
    the child; the orchestrator's own KeyboardInterrupt handler prints a
    resume hint before returning. Either way we want to push next."""
    print("\n=== [2/3] Running etl.backfill_2025 ===")
    print(f"  args: {' '.join(args) if args else '(none — full season default)'}")
    try:
        return subprocess.run(
            [PYTHON, "-m", "etl.backfill_2025", *args],
            cwd=str(REPO),
        ).returncode
    except KeyboardInterrupt:
        # SIGINT was sent to this wrapper; child already saw it too and
        # exited (probably with 130). Continue to the finally block to
        # push partial progress.
        return 130


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _force_utf8_io()
    _load_dotenv()

    required = (
        "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY", "R2_BUCKET",
    )
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(
            f"\nERROR: required R2 env vars not set: {', '.join(missing)}\n"
            "\n"
            "Add them to .env at the repo root, or export them in your shell:\n"
            "    R2_ACCOUNT_ID=<32-hex Cloudflare account ID>\n"
            "    R2_ACCESS_KEY_ID=<R2 token access key>\n"
            "    R2_SECRET_ACCESS_KEY=<R2 token secret>\n"
            "    R2_BUCKET=mlb-hr-bets-db\n"
            "\n"
            "See docs/HOSTING.md, section 2 for token setup.\n",
            file=sys.stderr,
        )
        sys.exit(2)

    # Masked R2 identity — print exactly which account / key / bucket this
    # run will use, so a wrong credential is obvious at a glance instead of
    # surfacing 30 lines deep in a boto3 traceback. Account ID is semi-public
    # (it's in every dashboard URL); the access key is masked; the secret is
    # never printed.
    _acct = os.environ.get("R2_ACCOUNT_ID", "")
    _akid = os.environ.get("R2_ACCESS_KEY_ID", "")
    _bkt = os.environ.get("R2_BUCKET", "")

    def _mask(s: str) -> str:
        return f"{s[:6]}...{s[-4:]}" if len(s) > 12 else "(short!)"

    print(f"  R2 target: bucket={_bkt}  account={_acct}  key={_mask(_akid)}")

    forwarded_args = sys.argv[1:]

    pull()
    orch_exit = 0
    push_exit = 0
    try:
        orch_exit = run_orchestrator(forwarded_args)
    finally:
        # ALWAYS push — even if orchestrator crashed, Ctrl+C'd, or
        # exited cleanly. Each date's work is already committed to local
        # DB inside backfill_one_date, so pushing partial progress is
        # the safe move; the next session resumes from where this left off.
        push_exit = push()

    # Surface both exit codes in the final message so the user sees the
    # full picture even when they only glanced at the tail of the log.
    print("\n=== Done ===")
    print(f"  orchestrator exit: {orch_exit}"
          + ("  (Ctrl+C — resume mode picks up next run)" if orch_exit == 130 else ""))
    print(f"  R2 push exit:      {push_exit}"
          + ("  (FAILED — retry with python infra/r2_sync.py push)" if push_exit else "  (OK)"))

    # Exit code reflects the most serious failure: push failure > orch failure > success.
    if push_exit:
        sys.exit(push_exit)
    sys.exit(orch_exit)


if __name__ == "__main__":
    main()
