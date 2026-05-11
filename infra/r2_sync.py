#!/usr/bin/env python3
"""
r2_sync.py — Pull/push hr_bets.db between Cloudflare R2 and the local
``../data/`` directory that ``etl/db.py`` resolves to.

Used by the scheduled GitHub Actions workflows. R2 plays the role that
OneDrive sync plays on Pablo's laptop: it's the durable home for the
SQLite file between job runs.

Why R2 (vs. committing the DB, vs. GH Actions cache):
  - Committed: bloats repo history (~32MB × N days). Hard pass.
  - GH Actions cache: 10GB cap fine, BUT caches evict after 7 idle days
    and there is no cross-workflow visibility guarantee (caches are scoped
    per branch and may be GC'd unpredictably). Acceptable as a *secondary*
    cache for the heavy pybaseball/savant pulls, NOT as the source of
    truth for the DB.
  - R2: free up to 10GB, S3-compatible, zero egress fees within CF, and
    Pablo already has a CF account for the workers. Wins on every axis.

Concurrency:
  Only one workflow should be touching the DB at a time. Each workflow
  declares `concurrency: { group: hr-bets-db, cancel-in-progress: false }`
  so noon/1AM/2AM jobs serialize naturally. This script does NOT acquire
  its own lock — the workflow-level group is the lock.

Safety:
  - ``pull`` is idempotent: downloads to the resolved DB_PATH, overwriting
    whatever's there. Stale local artifacts are harmless because the very
    next job step is the ETL, which reads fresh.
  - ``push`` checkpoints WAL first (``PRAGMA wal_checkpoint(TRUNCATE)``).
    Without this, the .db file on disk lags behind the in-WAL state and
    the next puller sees a stale snapshot — silent data loss.
  - A tempfile sidecar is uploaded then atomically renamed on R2 (boto3's
    ``copy_object``+``delete_object`` pattern), so a half-failed upload
    can't replace a good remote DB with garbage.

Usage:
    python infra/r2_sync.py pull           # GH Actions: start of each job
    python infra/r2_sync.py push           # GH Actions: end of each job
    python infra/r2_sync.py pull --dry-run # Print what would happen

Required env vars (set as GH Actions secrets):
    R2_ACCOUNT_ID          Cloudflare account ID (32-hex)
    R2_ACCESS_KEY_ID       R2 API token access key
    R2_SECRET_ACCESS_KEY   R2 API token secret
    R2_BUCKET              Bucket name, e.g. "mlb-hr-bets-db"
    R2_DB_KEY              Optional, defaults to "hr_bets.db". The object
                           key inside the bucket. Override for staging/test
                           buckets that share a single CF account.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# Make the repo root importable so we can re-use the canonical DB_PATH from
# etl/db.py. Single source of truth: if Pablo ever relocates the DB, this
# script follows automatically.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
sys.path.insert(0, str(_REPO_ROOT))

from etl.db import DB_PATH  # noqa: E402  (path mutation above is intentional)


# ────────────────────────────────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────────────────────────────────

DEFAULT_OBJECT_KEY = "hr_bets.db"
STAGING_SUFFIX = ".staging"   # R2 upload target before atomic rename


def _r2_client():
    """Build a boto3 S3 client pointed at the user's R2 account."""
    import boto3
    from botocore.config import Config

    account_id = _require_env("R2_ACCOUNT_ID")
    access_key = _require_env("R2_ACCESS_KEY_ID")
    secret_key = _require_env("R2_SECRET_ACCESS_KEY")

    endpoint = f"https://{account_id}.r2.cloudflarestorage.com"

    # R2 supports v4 signatures but rejects the AWS-specific addressing
    # style. Force path-style and the auto region "auto" (R2's convention).
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 4}),
    )


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(
            f"ERROR: required env var {name} is not set. "
            f"In GH Actions, add it under repo Settings → Secrets and variables → Actions."
        )
    return val


# ────────────────────────────────────────────────────────────────────────────
# Pull (R2 → local)
# ────────────────────────────────────────────────────────────────────────────

def pull(dry_run: bool = False) -> None:
    """Download the canonical hr_bets.db from R2 to ``DB_PATH``.

    Creates the parent dir if it doesn't exist. Overwrites any existing
    local file (the GH Actions runner is ephemeral, so there's never
    anything worth preserving locally).
    """
    bucket = _require_env("R2_BUCKET")
    key = os.environ.get("R2_DB_KEY", DEFAULT_OBJECT_KEY)

    dest = Path(DB_PATH)
    dest.parent.mkdir(parents=True, exist_ok=True)

    print(f"[r2_sync] pull  s3://{bucket}/{key} → {dest}")
    if dry_run:
        print("[r2_sync] dry-run; no download performed.")
        return

    t0 = time.time()
    client = _r2_client()

    # Download to a tempfile in the same dir first, then rename. Atomic
    # on POSIX; near-atomic on Windows. If the download errors out, the
    # half-written file never replaces a (possibly cached) good copy.
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=".hr_bets.db.dl-")
    os.close(tmp_fd)
    try:
        client.download_file(bucket, key, tmp_name)
        os.replace(tmp_name, dest)
    except Exception:
        # Clean up the half-written file so the next attempt is clean.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise

    size_mb = dest.stat().st_size / (1024 * 1024)
    print(f"[r2_sync] pull OK  ({size_mb:.1f} MB in {time.time() - t0:.1f}s)")


# ────────────────────────────────────────────────────────────────────────────
# Push (local → R2)
# ────────────────────────────────────────────────────────────────────────────

def push(dry_run: bool = False) -> None:
    """Upload the local hr_bets.db to R2 after a WAL checkpoint.

    Two-step upload for crash safety:
      1. PUT to ``<key>.staging``  — full bytes uploaded, no client sees it yet.
      2. COPY staging → key, then DELETE staging.

    If step 1 fails halfway, no consumer of <key> is affected. If step 2's
    copy fails, the previous version of <key> is still intact.
    """
    bucket = _require_env("R2_BUCKET")
    key = os.environ.get("R2_DB_KEY", DEFAULT_OBJECT_KEY)
    staging_key = key + STAGING_SUFFIX

    src = Path(DB_PATH)
    if not src.exists():
        raise SystemExit(
            f"ERROR: local DB {src} does not exist — refusing to push. "
            f"Did the pipeline steps run before this?"
        )

    print(f"[r2_sync] push  {src} → s3://{bucket}/{key}")

    # --- Checkpoint WAL into the main DB file. ---
    # Without this, .db-wal can contain unfinished transactions that
    # aren't yet in .db. We'd upload a stale snapshot, and the next
    # pull would silently miss the most recent writes.
    print("[r2_sync] PRAGMA wal_checkpoint(TRUNCATE)...")
    conn = sqlite3.connect(str(src))
    try:
        # TRUNCATE: checkpoint everything, then zero out the WAL file.
        # Returns (busy, log, checkpointed). busy=1 means a writer was
        # active — bail; we don't want to upload mid-write.
        busy, log, ckpt = conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
        if busy:
            raise SystemExit(
                f"ERROR: SQLite reports WAL busy (busy={busy}). Another "
                f"process is writing to {src}. Aborting push to avoid a "
                f"torn upload."
            )
        print(f"[r2_sync] checkpoint OK (log={log} ckpt={ckpt})")
    finally:
        conn.close()

    if dry_run:
        size_mb = src.stat().st_size / (1024 * 1024)
        print(f"[r2_sync] dry-run; would upload {size_mb:.1f} MB.")
        return

    # --- Upload to staging key first. ---
    t0 = time.time()
    client = _r2_client()
    client.upload_file(str(src), bucket, staging_key)
    size_mb = src.stat().st_size / (1024 * 1024)
    print(f"[r2_sync] staged ({size_mb:.1f} MB in {time.time() - t0:.1f}s)")

    # --- Atomic rename: copy staging → key, then delete staging. ---
    # R2 doesn't have a true server-side rename; copy+delete is the
    # closest equivalent. The window between copy and delete is the
    # only one where both exist; if delete fails, we just have an
    # orphaned .staging blob (harmless, cleanup script can sweep).
    client.copy_object(
        Bucket=bucket,
        CopySource={"Bucket": bucket, "Key": staging_key},
        Key=key,
    )
    try:
        client.delete_object(Bucket=bucket, Key=staging_key)
    except Exception as e:
        # Non-fatal — the live key has the new bytes. Log and move on.
        print(f"[r2_sync] WARN: failed to delete staging blob: {e}")

    print(f"[r2_sync] push OK  → s3://{bucket}/{key}")


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = p.add_subparsers(dest="cmd", required=True)

    pull_p = sub.add_parser("pull", help="Download hr_bets.db from R2 to local")
    pull_p.add_argument("--dry-run", action="store_true")

    push_p = sub.add_parser("push", help="Upload local hr_bets.db to R2 (with WAL checkpoint)")
    push_p.add_argument("--dry-run", action="store_true")

    args = p.parse_args(argv)

    if args.cmd == "pull":
        pull(dry_run=args.dry_run)
    elif args.cmd == "push":
        push(dry_run=args.dry_run)
    else:
        p.error(f"unknown command: {args.cmd}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
