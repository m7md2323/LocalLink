"""Database connection setup and test-only swap helpers.

This module owns the `db` instance so both `models.py` and `database.py` can
import it without creating a circular import.

Tests can replace the active database with `set_db(...)`; everything in
`database.py` reads through `get_db()`.
"""
import os
import sys
from pathlib import Path
from typing import Optional

from playhouse.sqliteq import SqliteQueueDatabase


def _default_db_path() -> Path:
    """Return the stable location for the SQLite database.

    When running as a frozen .exe, state lives NEXT TO the .exe so the
    whole folder is portable (copy it to a USB stick, zip it up, etc.).

    When running from source (``python run.py``), state lives in CWD
    (the repo root), matching the developer workflow.
    """
    if getattr(sys, "frozen", False) and getattr(sys, "executable", None):
        return Path(sys.executable).parent / "locallink.db"
    return Path("locallink.db")  # dev: CWD = repo root


def _resolve_db_path() -> str:
    """Resolve the DB path.

    Rules (mirroring ``keys._resolve_keys_dir``):
      - An ABSOLUTE path in ``LOCALLINK_DB_PATH`` is honored.
      - A relative path is ignored (would re-introduce the
        CWD-dependent DB bug).
      - Otherwise, use the per-user default.

    In both branches we ``mkdir(parents=True)`` the parent dir, since
    ``SqliteQueueDatabase`` does NOT auto-create it and would raise
    ``unable to open database file`` on first run otherwise.
    """
    override = os.environ.get("LOCALLINK_DB_PATH", "").strip()
    if override:
        override_path = Path(override)
        if override_path.is_absolute():
            override_path.parent.mkdir(parents=True, exist_ok=True)
            return str(override_path)
    default = _default_db_path()
    default.parent.mkdir(parents=True, exist_ok=True)
    return str(default)


_db_path = _resolve_db_path()

_db: SqliteQueueDatabase = SqliteQueueDatabase(
    _db_path,
    pragmas={
        "journal_mode": "wal",  # readers don't block writer
        "cache_size": -64000,    # 64 MB page cache
        "foreign_keys": 1,       # enforce FK constraints
    },
    autostart=True,
)


def get_db() -> SqliteQueueDatabase:
    """Return the active database. Tests may swap it via `set_db`."""
    return _db


def set_db(new_db: SqliteQueueDatabase) -> None:
    """Replace the active database. Intended for tests only.

    Note: this updates the connection used by functions in `database.py`,
    but does NOT rebind the `Meta.database` attribute on model classes —
    those are bound at import time. Tests that need model-level isolation
    should set `LOCALLINK_DB_PATH` to an absolute path before importing.
    """
    global _db
    _db = new_db
