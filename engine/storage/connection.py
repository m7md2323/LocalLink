"""Database connection setup and test-only swap helpers.

This module owns the `db` instance so both `models.py` and `database.py` can
import it without creating a circular import.

Tests can replace the active database with `set_db(...)`; everything in
`database.py` reads through `get_db()`.
"""
import os
from typing import Optional

from playhouse.sqliteq import SqliteQueueDatabase


_db: SqliteQueueDatabase = SqliteQueueDatabase(
    os.getenv("LOCALLINK_DB_PATH", "locallink.db"),
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
    should set `LOCALLINK_DB_PATH` before importing.
    """
    global _db
    _db = new_db
