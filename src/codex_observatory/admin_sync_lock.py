"""Small SQLite lease used to coordinate Admin sync processes."""

from __future__ import annotations

import time
from typing import Any


def acquire(connection: Any, table: str, owner_id: str, *, lease_seconds: int = 300) -> bool:
    """Acquire a single-row lease, returning false when another process owns it."""
    try:
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("BEGIN IMMEDIATE")
    except Exception:  # noqa: BLE001 - a locked SQLite writer means busy.
        connection.rollback()
        connection.execute("PRAGMA busy_timeout=5000")
        return False
    now = int(time.time())
    row = connection.execute(
        f"SELECT owner_id,lease_expires_at FROM {table} WHERE lock_id=1"
    ).fetchone()
    if row and int(row["lease_expires_at"]) > now:
        connection.rollback()
        connection.execute("PRAGMA busy_timeout=5000")
        return False
    connection.execute(
        f"INSERT INTO {table}(lock_id,owner_id,lease_expires_at) VALUES(1,?,?) "
        "ON CONFLICT(lock_id) DO UPDATE SET owner_id=excluded.owner_id,lease_expires_at=excluded.lease_expires_at",
        (owner_id, now + lease_seconds),
    )
    connection.commit()
    connection.execute("PRAGMA busy_timeout=5000")
    return True


def release(connection: Any, table: str, owner_id: str) -> None:
    with connection:
        connection.execute(
            f"DELETE FROM {table} WHERE lock_id=1 AND owner_id=?", (owner_id,)
        )
