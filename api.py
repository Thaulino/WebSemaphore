"""Token-authenticated REST API for locking resources from scripts and CI.

Authentication is bearer-token only: session cookies are deliberately ignored
here, which is what keeps these endpoints out of reach of cross-site requests.
"""

import functools

from flask import Blueprint, g, jsonify, request

import auth
import db
import openapi
import resources

bp = Blueprint("api", __name__, url_prefix="/api")


def error(status, message):
    return jsonify({"error": message}), status


def token_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return error(401, "Send 'Authorization: Bearer <token>'.")
        user = auth.user_for_token(db.get_db(), token.strip())
        if user is None:
            return error(401, "Unknown or revoked token.")
        g.api_user = user
        return view(*args, **kwargs)

    return wrapped


def resource_json(conn, resource, in_use=None):
    if in_use is None:
        in_use = resources.count_active(conn, resource["id"])
    return {
        "id": resource["id"],
        "name": resource["name"],
        "description": resource["description"],
        "labels": resources.labels_of(conn, resource["id"]),
        "capacity": resource["capacity"],
        "in_use": in_use,
        "free": resource["capacity"] - in_use,
    }


def lock_json(conn, claim, resource):
    return {
        "lock_id": claim["id"],
        "resource_id": resource["id"],
        "resource": resource["name"],
        "note": claim["note"],
        "acquired_at": claim["acquired_at"],
        "user": g.api_user["username"],
    }


@bp.get("/openapi.json")
def spec():
    return jsonify(openapi.spec())


@bp.get("/me")
@token_required
def me():
    return jsonify(
        {
            "id": g.api_user["id"],
            "username": g.api_user["username"],
            "is_admin": bool(g.api_user["is_admin"]),
        }
    )


@bp.get("/v1/resources")
@token_required
def list_resources():
    conn = db.get_db()
    label = request.args.get("label") or None
    free_only = request.args.get("free", "").lower() in ("1", "true", "yes")
    items = resources.listing(conn, label=label)
    payload = [
        resource_json(conn, resource, resource["in_use"]) for resource, _ in items
    ]
    if free_only:
        payload = [item for item in payload if item["free"] > 0]
    return jsonify({"resources": payload})


@bp.get("/v1/resources/<resource_id>")
@token_required
def get_resource(resource_id):
    conn = db.get_db()
    resource = resources.by_name_or_id(conn, resource_id)
    if resource is None:
        return error(404, "No such resource.")
    payload = resource_json(conn, resource)
    payload["holders"] = [
        {
            "lock_id": claim["id"],
            "user": claim["username"],
            "note": claim["note"],
            "acquired_at": claim["acquired_at"],
        }
        for claim in resources.active_claims(conn, resource["id"])
    ]
    return jsonify(payload)


@bp.post("/v1/locks")
@token_required
def create_lock():
    conn = db.get_db()
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return error(400, "Body must be a JSON object.")
    target = body.get("resource")
    label = body.get("label")
    note = body.get("note", "")
    if bool(target) == bool(label):
        return error(400, "Give exactly one of 'resource' or 'label'.")

    try:
        if target:
            resource = resources.by_name_or_id(conn, target)
            if resource is None:
                return error(404, "No such resource.")
            claim_id = resources.acquire(conn, resource["id"], g.api_user["id"], note)
        else:
            claim_id, resource = resources.acquire_by_label(
                conn, str(label).strip().lower(), g.api_user["id"], note
            )
    except resources.Error as exc:
        # "occupied" is a conflict the caller can retry; anything else is their input.
        status = 409 if "occupied" in str(exc) else 404
        return error(status, str(exc))

    claim = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    return jsonify(lock_json(conn, claim, resource)), 201


@bp.get("/v1/locks")
@token_required
def list_locks():
    conn = db.get_db()
    return jsonify(
        {
            "locks": [
                {
                    "lock_id": claim["id"],
                    "resource_id": claim["resource_id"],
                    "resource": claim["name"],
                    "note": claim["note"],
                    "acquired_at": claim["acquired_at"],
                }
                for claim in resources.my_claims(conn, g.api_user["id"])
            ]
        }
    )


@bp.delete("/v1/locks/<int:lock_id>")
@token_required
def delete_lock(lock_id):
    conn = db.get_db()
    claim = conn.execute("SELECT * FROM claims WHERE id = ?", (lock_id,)).fetchone()
    if claim is None:
        return error(404, "No such lock.")
    resource = resources.get(conn, claim["resource_id"])
    is_owner = resource is not None and resource["owner_id"] == g.api_user["id"]
    try:
        resources.release(
            conn, lock_id, g.api_user["id"], is_owner or bool(g.api_user["is_admin"])
        )
    except resources.Error as exc:
        return error(409, str(exc))
    return jsonify({"released": lock_id})
