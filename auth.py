"""Account creation, password hashing and the login-required guard."""

import functools
import hashlib
import hmac
import re
import secrets
import sqlite3

from flask import g, redirect, request, session, url_for

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


def register(conn, username, password, confirm):
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise Error("Username must be 3-32 characters: letters, digits, . _ -")
    if len(password or "") < 8:
        raise Error("Password must be at least 8 characters.")
    if password != confirm:
        raise Error("The two passwords do not match.")
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), db.now()),
        )
    except sqlite3.IntegrityError:
        raise Error(f"The username {username!r} is already taken.")
    return conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()


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


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    sent = request.form.get("csrf", "")
    return hmac.compare_digest(sent, session.get("csrf", ""))
