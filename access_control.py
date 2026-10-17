from __future__ import annotations

import os
import hashlib
import sqlite3
import time
from pathlib import Path


def _db_path() -> Path:
    return Path(os.getenv("ACCESS_DB", "/data/proxyvip_reseller_access.sqlite3"))


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS access ("
        "user_id INTEGER PRIMARY KEY, expires_at INTEGER NOT NULL, approved_by INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS alerts ("
        "user_id INTEGER PRIMARY KEY, last_alert INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS generated_keys ("
        "fingerprint TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, "
        "created_at INTEGER NOT NULL, deleted_at INTEGER)"
    )
    conn.commit()
    return conn


def is_allowed(user_id: int, admin_id: int, now: int | None = None) -> bool:
    if user_id == admin_id:
        return True
    now = int(time.time()) if now is None else int(now)
    with _connect() as conn:
        row = conn.execute("SELECT expires_at FROM access WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return False
        if int(row[0]) <= now:
            conn.execute("DELETE FROM access WHERE user_id = ?", (user_id,))
            conn.commit()
            return False
        return True


def approve(user_id: int, days: int, admin_id: int, now: int | None = None) -> int:
    if user_id <= 0:
        raise ValueError("ID do Telegram inválido")
    if not 1 <= days <= 30:
        raise ValueError("o prazo deve ficar entre 1 e 30 dias")
    now = int(time.time()) if now is None else int(now)
    expires_at = now + days * 86400
    with _connect() as conn:
        conn.execute(
            "INSERT INTO access(user_id, expires_at, approved_by) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at, "
            "approved_by=excluded.approved_by",
            (user_id, expires_at, admin_id),
        )
        conn.commit()
    return expires_at


def revoke(user_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM access WHERE user_id = ?", (user_id,))
        conn.commit()


def list_active(now: int | None = None) -> list[tuple[int, int]]:
    now = int(time.time()) if now is None else int(now)
    with _connect() as conn:
        conn.execute("DELETE FROM access WHERE expires_at <= ?", (now,))
        rows = conn.execute("SELECT user_id, expires_at FROM access ORDER BY expires_at", ()).fetchall()
        conn.commit()
    return [(int(user_id), int(expires_at)) for user_id, expires_at in rows]


def should_alert(user_id: int, cooldown_seconds: int = 3600, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else int(now)
    with _connect() as conn:
        row = conn.execute("SELECT last_alert FROM alerts WHERE user_id = ?", (user_id,)).fetchone()
        if row and now - int(row[0]) < cooldown_seconds:
            return False
        conn.execute(
            "INSERT INTO alerts(user_id, last_alert) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_alert=excluded.last_alert",
            (user_id, now),
        )
        conn.commit()
    return True


def _key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.strip().upper().encode("utf-8")).hexdigest()


def record_generated_keys(owner_id: int, keys: list[str], now: int | None = None) -> None:
    """Record only a one-way fingerprint, never the plaintext key."""
    now = int(time.time()) if now is None else int(now)
    rows = [(_key_fingerprint(key), int(owner_id), now) for key in keys if key.strip()]
    if not rows:
        return
    with _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO generated_keys(fingerprint, owner_id, created_at) VALUES(?,?,?)",
            rows,
        )
        conn.commit()


def generated_key_record(key: str) -> tuple[int, int | None] | None:
    """Return (owner ID, deletion timestamp), or None for an untracked key."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT owner_id, deleted_at FROM generated_keys WHERE fingerprint = ?",
            (_key_fingerprint(key),),
        ).fetchone()
    return (int(row[0]), int(row[1]) if row[1] is not None else None) if row else None


def key_deletion_denial(user_id: int, admin_id: int, key: str) -> str | None:
    """Return a denial reason unless the requester may delete this key."""
    record = generated_key_record(key)
    if record and record[1] is not None:
        return "already_deleted"
    if int(user_id) == int(admin_id):
        return None
    if record is None:
        return "untracked"
    if record[0] != int(user_id):
        return "not_owner"
    return None


def mark_generated_key_deleted(
    key: str,
    now: int | None = None,
    owner_id: int | None = None,
) -> None:
    now = int(time.time()) if now is None else int(now)
    with _connect() as conn:
        if owner_id is not None:
            conn.execute(
                "INSERT OR IGNORE INTO generated_keys(fingerprint, owner_id, created_at, deleted_at) "
                "VALUES(?,?,?,?)",
                (_key_fingerprint(key), int(owner_id), now, now),
            )
        conn.execute(
            "UPDATE generated_keys SET deleted_at = ? WHERE fingerprint = ? AND deleted_at IS NULL",
            (now, _key_fingerprint(key)),
        )
        conn.commit()
