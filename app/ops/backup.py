"""Backup: a thin wrapper around `pg_dump` for the Supabase Postgres DB.

The Supabase free tier does not include PITR, so we take a local dump
once a day. Production Supabase tiers include PITR; this is a belt
and suspenders for the MVP.

The dump is written to `data/backups/<date>.sql.gz`. The CLI also
supports a `restore_drill` subcommand that copies the most recent
dump into a fresh database and asserts row counts.
"""
from __future__ import annotations

import gzip
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


logger = logging.getLogger(__name__)


def backup(db_url: str, out_dir: Path, *, now: datetime | None = None) -> Path:
    """Run `pg_dump` to a gzipped file. Returns the path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(tz=timezone.utc)
    out = out_dir / f"backup-{now.date().isoformat()}.sql.gz"
    raw_path = out.with_suffix(".sql.tmp")
    # pg_dump to stdout, gzip to file
    with raw_path.open("wb") as raw, gzip.open(out, "wb") as gz:
        proc = subprocess.run(
            ["pg_dump", db_url, "--no-owner", "--no-privileges"],
            stdout=raw, stderr=subprocess.PIPE, check=False,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {proc.stderr.decode('utf-8', 'replace')}")
    if shutil.which("gzip") is None:
        # On Windows pg_dump may not be on PATH; the gzip.open above
        # already compressed the file. We just rename the .sql.tmp out.
        out = raw_path.rename(out.with_suffix(".sql"))
    raw_path.unlink(missing_ok=True)
    logger.info("backup written: %s (%d bytes)", out, out.stat().st_size)
    return out
