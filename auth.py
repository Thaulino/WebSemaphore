"""Account creation, password hashing and the login-required guard."""

import functools
import hashlib
import hmac
import re
import secrets
import sqlite3

from flask import abort, g, redirect, request, session, url_for

import db

PBKDF2_ROUNDS = 240_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


class Error(Exception):
    """A problem worth showing to the user."""


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        _, rounds, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def check_password_policy(password, confirm=None):
    if len(password or "") < 8:
        raise Error("Password must be at least 8 characters.")
    if confirm is not None and password != confirm:
        raise Error("The two passwords do not match.")


def register(conn, username, password, confirm):
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise Error("Username must be 3-32 characters: letters, digits, . _ -")
    check_password_policy(password, confirm)
    # The very first account owns the place, otherwise nobody could administer it.
    first_user = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at)"
            " VALUES (?, ?, ?, ?)",
            (username, hash_password(password), 1 if first_user else 0, db.now()),
        )
    except sqlite3.IntegrityError:
        raise Error(f"The username {username!r} is already taken.")
    return conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()


def set_password(conn, user_id, password, confirm=None):
    check_password_policy(password, confirm)
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(password), user_id),
    )
    # Force a fresh login everywhere: old session cookies keep working otherwise.
    conn.execute("DELETE FROM api_tokens WHERE user_id = ?", (user_id,))


def set_admin(conn, user_id, is_admin):
    if not is_admin and _admin_count(conn) <= 1:
        raise Error("There must be at least one admin left.")
    conn.execute(
        "UPDATE users SET is_admin = ? WHERE id = ?", (1 if is_admin else 0, user_id)
    )


def _admin_count(conn):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
    ).fetchone()["n"]


def delete_user(conn, user_id, acting_admin_id):
    """Remove an account, handing its resources to the admin doing the deleting."""
    if user_id == acting_admin_id:
        raise Error("You cannot delete your own account.")
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        raise Error("That user no longer exists.")
    if user["is_admin"] and _admin_count(conn) <= 1:
        raise Error("There must be at least one admin left.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE resources SET owner_id = ? WHERE owner_id = ?",
            (acting_admin_id, user_id),
        )
        conn.execute("DELETE FROM claims WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return user


def authenticate(conn, username, password):
    user = conn.execute(
        "SELECT * FROM users WHERE username = ?", ((username or "").strip(),)
    ).fetchone()
    if user is None:
        # Hash anyway so a missing user costs the same as a wrong password.
        verify_password(password or "", hash_password("decoy"))
        return None
    if not verify_password(password or "", user["password_hash"]):
        return None
    return user


def log_in(user):
    session.clear()
    session["user_id"] = user["id"]
    session["csrf"] = secrets.token_urlsafe(32)
    session.permanent = True


def log_out():
    session.clear()


def load_user():
    """Populate g.user from the session cookie; runs before every request."""
    g.user = None
    user_id = session.get("user_id")
    if user_id is not None:
        g.user = db.get_db().execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if g.user is None:
            session.clear()


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        if not g.user["is_admin"]:
            abort(403, "Admins only.")
        return view(*args, **kwargs)

    return wrapped


# --- API tokens -------------------------------------------------------------
#
# Tokens are random enough that a plain SHA-256 is the right primitive here;
# the slow PBKDF2 above exists for low-entropy human passwords.

TOKEN_PREFIX = "sem"


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(conn, user_id, name):
    """Return (row, plaintext). The plaintext is shown once and never stored."""
    prefix = secrets.token_hex(4)
    token = f"{TOKEN_PREFIX}_{prefix}_{secrets.token_urlsafe(32)}"
    conn.execute(
        "INSERT INTO api_tokens (user_id, name, prefix, token_hash, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (user_id, (name or "").strip()[:60], prefix, _token_hash(token), db.now()),
    )
    row = conn.execute(
        "SELECT * FROM api_tokens WHERE prefix = ?", (prefix,)
    ).fetchone()
    return row, token


def list_tokens(conn, user_id):
    return conn.execute(
        "SELECT * FROM api_tokens WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()


def revoke_token(conn, token_id, user_id):
    conn.execute(
        "DELETE FROM api_tokens WHERE id = ? AND user_id = ?", (token_id, user_id)
    )


def user_for_token(conn, token):
    """Resolve a bearer token to its owner, or None."""
    # Split only the two fixed fields off the front: the secret is urlsafe
    # base64 and may itself contain underscores.
    parts = (token or "").split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    row = conn.execute(
        "SELECT * FROM api_tokens WHERE prefix = ?", (parts[1],)
    ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(_token_hash(token), row["token_hash"]):
        return None
    conn.execute(
        "UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (db.now(), row["id"])
    )
    return conn.execute(
        "SELECT * FROM users WHERE id = ?", (row["user_id"],)
    ).fetchone()


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    sent = request.form.get("csrf", "")
    return hmac.compare_digest(sent, session.get("csrf", ""))
