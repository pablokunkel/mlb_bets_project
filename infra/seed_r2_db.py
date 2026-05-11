#!/usr/bin/env python3
"""
seed_r2_db.py — One-time uploader to put your existing local hr_bets.db
into R2 so the scheduled GH Actions runs have something to pull on first
boot.

Run this ONCE from Pablo's laptop after creating the R2 bucket and the
API token (see docs/HOSTING.md). After this seed succeeds, GH Actions
takes over and you never touch this script again.

Mechanics:
  - Reads R2 creds from environment OR from <repo>/.env (matching the
    convention features_v2.py uses for VEGAS_ODDS_API_KEY).
  - Refuses to overwrite an existing remote DB unless --force is passed.
    This is the safety net for "oops I ran this twice and clobbered the
    GH-Actions-managed DB with my stale laptop copy."
  - Re-uses r2_sync.push under the hood — same WAL checkpoint, same
    atomic staging-rename pattern. We want the seed to look identical
    to a normal push so we don't have a separate code path to maintain.

Usage:
    # Default: read creds from ../<repo>/.env (or env vars)
    python infra/seed_r2_db.py

    # Force overwrite (use when migrating, NOT routinely)
    python infra/seed_r2_db.py --force

    # Dry-run to confirm wiring
    python infra/seed_r2_db.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
sys.path.insert(0, str(_REPO_ROOT))


def _load_dotenv_if_present() -> None:
    """Tiny .env loader matching features_v2.py's behavior.

    Non-overriding: existing env vars win. Lets you do
    ``R2_BUCKET=foo python seed_r2_db.py`` for ad-hoc overrides without
    editing .env.
    """
    env_file = _REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _check_remote_exists(bucket: str, key: str) -> bool:
    """Return True if s3://<bucket>/<key> exists in R2."""
    from infra.r2_sync import _r2_client  # local import; needs deps installed
    from botocore.exceptions import ClientError

    client = _r2_client()
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="One-time upload of laptop hr_bets.db to R2.")
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the remote DB even if it already exists. Use with care.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen, do not upload.",
    )
    args = p.parse_args(argv)

    _load_dotenv_if_present()

    # Defer imports so --help works even if boto3 isn't installed.
    from infra.r2_sync import push, _require_env, DEFAULT_OBJECT_KEY

    bucket = _require_env("R2_BUCKET")
    key = os.environ.get("R2_DB_KEY", DEFAULT_OBJECT_KEY)

    # Safety check: refuse to clobber unless --force.
    if not args.dry_run and not args.force:
        exists = _check_remote_exists(bucket, key)
        if exists:
            print(
                f"ABORT: s3://{bucket}/{key} already exists. The seed script is "
                f"meant for the FIRST upload only.\n"
                f"  - If this is the real first seed and the object is leftover from "
                f"a test, run with --force.\n"
                f"  - Otherwise, do NOT run this. The scheduled jobs are managing the "
                f"remote DB; overwriting it from your laptop would lose any work the\n"
                f"    cloud jobs have done since your last local pull."
            )
            return 2
        print(f"[seed] remote s3://{bucket}/{key} not found — safe to seed.")

    push(dry_run=args.dry_run)
    if not args.dry_run:
        print(f"\n[seed] DONE. GH Actions can now pull from s3://{bucket}/{key}.")
        print("[seed] Next: enable the daily-picks.yml workflow and watch the first run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
