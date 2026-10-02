"""Resource CRUD and the counting-semaphore acquire/release logic."""

import re

import db

NAME_RE = re.compile(r"^[\w .\-/]{2,60}$")
LABEL_RE = re.compile(r"^[\w.\-]{1,30}$")


class Error(Exception):
    """A problem worth showing to the user."""


def parse_labels(raw):
    labels = []
    for part in re.split(r"[,\s]+", raw or ""):
        part = part.strip().lower()
        if not part:
            continue
        if not LABEL_RE.match(part):
            raise Error(f"Invalid label {part!r}: use letters, digits, . - _")
        if part not in labels:
            labels.append(part)
    if len(labels) > 12:
        raise Error("At most 12 labels per resource.")
    return labels


def _validate(name, capacity):
    if not NAME_RE.match(name or ""):
        raise Error("Name must be 2-60 characters (letters, digits, . - _ / space).")
    try:
        capacity = int(capacity)
    except (TypeError, ValueError):
        raise Error("Capacity must be a whole number.")
    if not 1 <= capacity <= 999:
        raise Error("Capacity must be between 1 and 999.")
    return capacity


def _set_labels(conn, resource_id, labels):
    conn.execute("DELETE FROM resource_labels WHERE resource_id = ?", (resource_id,))
    conn.executemany(
        "INSERT INTO resource_labels (resource_id, label) VALUES (?, ?)",
        [(resource_id, label) for label in labels],
    )


def create(conn, owner_id, name, description, capacity, labels):
    capacity = _validate(name, capacity)
    name = name.strip()
    exists = conn.execute("SELECT 1 FROM resources WHERE name = ?", (name,)).fetchone()
    if exists:
        raise Error(f"A resource named {name!r} already exists.")
    cur = conn.execute(
        "INSERT INTO resources (name, description, capacity, owner_id, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (name, (description or "").strip(), capacity, owner_id, db.now()),
    )
    _set_labels(conn, cur.lastrowid, labels)
    return cur.lastrowid


def update(conn, resource_id, name, description, capacity, labels):
    capacity = _validate(name, capacity)
    name = name.strip()
    clash = conn.execute(
        "SELECT 1 FROM resources WHERE name = ? AND id != ?", (name, resource_id)
    ).fetchone()
    if clash:
        raise Error(f"A resource named {name!r} already exists.")
    in_use = count_active(conn, resource_id)
    if capacity < in_use:
        raise Error(f"Capacity cannot drop below the {in_use} slot(s) in use.")
    conn.execute(
        "UPDATE resources SET name = ?, description = ?, capacity = ? WHERE id = ?",
        (name, (description or "").strip(), capacity, resource_id),
    )
    _set_labels(conn, resource_id, labels)


def delete(conn, resource_id):
    conn.execute("DELETE FROM resources WHERE id = ?", (resource_id,))


def get(conn, resource_id):
    return conn.execute(
        "SELECT r.*, u.username AS owner FROM resources r"
        " JOIN users u ON u.id = r.owner_id WHERE r.id = ?",
        (resource_id,),
    ).fetchone()


def labels_of(conn, resource_id):
    rows = conn.execute(
        "SELECT label FROM resource_labels WHERE resource_id = ? ORDER BY label",
        (resource_id,),
    ).fetchall()
    return [row["label"] for row in rows]


def all_labels(conn):
    rows = conn.execute(
        "SELECT label, COUNT(*) AS n FROM resource_labels GROUP BY label ORDER BY label"
    ).fetchall()
    return [(row["label"], row["n"]) for row in rows]


def listing(conn, label=None, query=None):
    sql = [
        "SELECT r.*, u.username AS owner,",
        " (SELECT COUNT(*) FROM claims c"
        "  WHERE c.resource_id = r.id AND c.released_at IS NULL) AS in_use",
        " FROM resources r JOIN users u ON u.id = r.owner_id",
    ]
    params = []
    where = []
    if label:
        where.append(
            "r.id IN (SELECT resource_id FROM resource_labels WHERE label = ?)"
        )
        params.append(label)
    if query:
        where.append("(r.name LIKE ? OR r.description LIKE ?)")
        params += [f"%{query}%", f"%{query}%"]
    if where:
        sql.append(" WHERE " + " AND ".join(where))
    sql.append(" ORDER BY r.name COLLATE NOCASE")
    rows = conn.execute("".join(sql), params).fetchall()
    return [(row, labels_of(conn, row["id"])) for row in rows]


def count_active(conn, resource_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM claims"
        " WHERE resource_id = ? AND released_at IS NULL",
        (resource_id,),
    ).fetchone()["n"]


def active_claims(conn, resource_id):
    return conn.execute(
        "SELECT c.*, u.username FROM claims c JOIN users u ON u.id = c.user_id"
        " WHERE c.resource_id = ? AND c.released_at IS NULL"
        " ORDER BY c.acquired_at",
        (resource_id,),
    ).fetchall()


def history(conn, resource_id, limit=15):
    return conn.execute(
        "SELECT c.*, u.username FROM claims c JOIN users u ON u.id = c.user_id"
        " WHERE c.resource_id = ? AND c.released_at IS NOT NULL"
        " ORDER BY c.released_at DESC LIMIT ?",
        (resource_id, limit),
    ).fetchall()


def my_claims(conn, user_id):
    return conn.execute(
        "SELECT c.*, r.name FROM claims c JOIN resources r ON r.id = c.resource_id"
        " WHERE c.user_id = ? AND c.released_at IS NULL"
        " ORDER BY c.acquired_at",
        (user_id,),
    ).fetchall()


def _claim_slot(conn, resource_id, user_id, note):
    """Insert a claim. Caller must already hold a write transaction."""
    cur = conn.execute(
        "INSERT INTO claims (resource_id, user_id, note, acquired_at)"
        " VALUES (?, ?, ?, ?)",
        (resource_id, user_id, (note or "").strip()[:200], db.now()),
    )
    return cur.lastrowid


def acquire(conn, resource_id, user_id, note):
    """Take one slot. The transaction is what keeps two users off the last slot."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        resource = conn.execute(
            "SELECT * FROM resources WHERE id = ?", (resource_id,)
        ).fetchone()
        if resource is None:
            raise Error("That resource no longer exists.")
        if count_active(conn, resource_id) >= resource["capacity"]:
            raise Error(f"{resource['name']} is fully occupied right now.")
        claim_id = _claim_slot(conn, resource_id, user_id, note)
        conn.execute("COMMIT")
        return claim_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


def acquire_by_label(conn, label, user_id, note):
    """Lock the first resource carrying `label` that still has a free slot."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        candidates = conn.execute(
            "SELECT r.* FROM resources r"
            " JOIN resource_labels l ON l.resource_id = r.id"
            " WHERE l.label = ? ORDER BY r.name COLLATE NOCASE",
            (label,),
        ).fetchall()
        if not candidates:
            raise Error(f"No resource carries the label {label!r}.")
        for resource in candidates:
            if count_active(conn, resource["id"]) < resource["capacity"]:
                claim_id = _claim_slot(conn, resource["id"], user_id, note)
                conn.execute("COMMIT")
                return claim_id, resource
        raise Error(f"Every resource labelled {label!r} is occupied right now.")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def by_name_or_id(conn, value):
    """Resources are addressable by numeric id or by name in the API."""
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        row = get(conn, int(value))
        if row is not None:
            return row
    return conn.execute(
        "SELECT r.*, u.username AS owner FROM resources r"
        " JOIN users u ON u.id = r.owner_id WHERE r.name = ?",
        (str(value),),
    ).fetchone()


def release(conn, claim_id, user_id, is_owner):
    claim = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    if claim is None or claim["released_at"] is not None:
        raise Error("That slot has already been released.")
    if claim["user_id"] != user_id and not is_owner:
        raise Error("Only the holder or the resource owner can release this slot.")
    conn.execute(
        "UPDATE claims SET released_at = ? WHERE id = ?", (db.now(), claim_id)
    )
