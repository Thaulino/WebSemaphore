"""SQLite storage layer for the shared-resource semaphore app."""

import os
import sqlite3
import time

from flask import g

DB_PATH = os.environ.get(
    "SEMAPHORE_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL DEFAULT '',
    prefix       TEXT    NOT NULL UNIQUE,
    token_hash   TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER
);

CREATE TABLE IF NOT EXISTS resources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT    NOT NULL DEFAULT '',
    capacity    INTEGER NOT NULL DEFAULT 1,
    owner_id    INTEGER NOT NULL REFERENCES users(id),
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_labels (
    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    label       TEXT    NOT NULL COLLATE NOCASE,
    PRIMARY KEY (resource_id, label)
);

CREATE TABLE IF NOT EXISTS claims (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    note        TEXT    NOT NULL DEFAULT '',
    acquired_at INTEGER NOT NULL,
    released_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_claims_active
    ON claims(resource_id) WHERE released_at IS NULL;
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def migrate(conn):
    """Bring an older database up to the current schema, keeping its data."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "is_admin" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
        )
    # Someone has to be able to get into the admin pages: the oldest account.
    admins = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
    ).fetchone()["n"]
    if admins == 0:
        conn.execute(
            "UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users)"
        )


def init():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        migrate(conn)
    finally:
        conn.close()


def now():
    return int(time.time())
