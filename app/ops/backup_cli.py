"""Tiny CLI that runs a backup, called by the systemd timer unit."""
from __future__ import annotations

import sys
from pathlib import Path

from app.config import get_settings
from app.ops.backup import backup


def main() -> int:
    s = get_settings()
    out_dir = s.project_root / "data" / "backups"
    path = backup(s.supabase_db_url, out_dir)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
