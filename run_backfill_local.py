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
# .env loader (no python-dotenv dep) — same pattern as features_v2._load_dotenv
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """Parse KEY=VALUE lines from .env into os.environ. Existing env wins."""
    env_path = REPO / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
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
