"""Semaphore: a small web app for booking shared machines and test devices."""

import datetime
import os
import secrets

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import api
import auth
import db
import openapi
import resources

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_secret_key():
    key = os.environ.get("SEMAPHORE_SECRET")
    if key:
        return key
    # Persist a generated key so sessions survive a restart in local use.
    path = os.path.join(BASE_DIR, ".secret_key")
    if not os.path.exists(path):
        with open(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600), "w") as fh:
            fh.write(secrets.token_urlsafe(48))
    with open(path) as fh:
        return fh.read().strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=14),
)
app.teardown_appcontext(db.close_db)
app.register_blueprint(api.bp)


@app.before_request
def before_request():
    auth.load_user()
    # The API authenticates by bearer token and ignores cookies, so a CSRF
    # token would protect nothing there.
    if request.blueprint == "api":
        return
    if request.method == "POST" and not auth.check_csrf():
        abort(400, "Invalid or expired form token. Please try again.")


@app.context_processor
def inject_globals():
    return {"csrf_token": auth.csrf_token, "user": g.get("user")}


@app.template_filter("since")
def since(timestamp):
    """Render a timestamp as a compact age, e.g. '3h 12m'."""
    seconds = max(0, db.now() - int(timestamp))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


@app.template_filter("stamp")
def stamp(timestamp):
    if not timestamp:
        return "never"
    return datetime.datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M")


def anchor(resource_id):
    """Back to the list, scrolled to one resource: there is no detail page."""
    return f"{url_for('index')}#r{resource_id}"


def safe_back(default):
    """Honour a 'back' form field, but only for paths on this site."""
    target = request.form.get("back", "")
    if target.startswith("/") and not target.startswith("//"):
        return target
    return default


def can_manage(resource):
    """Owners administer their own resources; admins administer everything."""
    return resource["owner_id"] == g.user["id"] or bool(g.user["is_admin"])


# --- authentication ---------------------------------------------------------


@app.route("/register", methods=("GET", "POST"))
def register():
    if request.method == "POST":
        try:
            user = auth.register(
                db.get_db(),
                request.form.get("username"),
                request.form.get("password"),
                request.form.get("confirm"),
            )
        except auth.Error as exc:
            flash(str(exc), "error")
        else:
            auth.log_in(user)
            flash(f"Welcome, {user['username']}.", "ok")
            return redirect(url_for("index"))
    return render_template("register.html", form=request.form.to_dict())


@app.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        user = auth.authenticate(
            db.get_db(), request.form.get("username"), request.form.get("password")
        )
        if user is None:
            flash("Wrong username or password.", "error")
        else:
            auth.log_in(user)
            target = request.form.get("next") or url_for("index")
            if not target.startswith("/"):
                target = url_for("index")
            return redirect(target)
    return render_template("login.html", next=request.args.get("next", ""))


@app.post("/logout")
def logout():
    auth.log_out()
    flash("Signed out.", "ok")
    return redirect(url_for("login"))


# --- resources --------------------------------------------------------------


@app.get("/")
@auth.login_required
def index():
    conn = db.get_db()
    label = request.args.get("label") or None
    query = (request.args.get("q") or "").strip() or None
    # Everything lives on this page, so each row carries its holders and history.
    items = []
    for resource, labels in resources.listing(conn, label=label, query=query):
        active = resources.active_claims(conn, resource["id"])
        items.append(
            {
                "resource": resource,
                "labels": labels,
                "active": active,
                "free": resource["capacity"] - len(active),
                "history": resources.history(conn, resource["id"], limit=10),
                "can_manage": can_manage(resource),
            }
        )
    return render_template(
        "index.html",
        items=items,
        labels=resources.all_labels(conn),
        mine=resources.my_claims(conn, g.user["id"]),
        active_label=label,
        query=query or "",
        back=request.full_path.rstrip("?"),
    )


@app.route("/resources/new", methods=("GET", "POST"))
@auth.login_required
def new_resource():
    if request.method == "POST":
        try:
            labels = resources.parse_labels(request.form.get("labels"))
            resource_id = resources.create(
                db.get_db(),
                g.user["id"],
                request.form.get("name"),
                request.form.get("description"),
                request.form.get("capacity"),
                labels,
            )
        except resources.Error as exc:
            flash(str(exc), "error")
        else:
            flash("Resource created.", "ok")
            return redirect(anchor(resource_id))
    return render_template(
        "edit.html", resource=None, form=request.form.to_dict(), labels=""
    )


@app.route("/resources/<int:resource_id>/edit", methods=("GET", "POST"))
@auth.login_required
def edit_resource(resource_id):
    conn = db.get_db()
    resource = resources.get(conn, resource_id)
    if resource is None:
        abort(404)
    if not can_manage(resource):
        abort(403, "Only the owner or an admin can edit this resource.")
    if request.method == "POST":
        try:
            labels = resources.parse_labels(request.form.get("labels"))
            resources.update(
                conn,
                resource_id,
                request.form.get("name"),
                request.form.get("description"),
                request.form.get("capacity"),
                labels,
            )
        except resources.Error as exc:
            flash(str(exc), "error")
        else:
            flash("Resource updated.", "ok")
            return redirect(anchor(resource_id))
        return render_template(
            "edit.html",
            resource=resource,
            form=request.form.to_dict(),
            labels=request.form.get("labels", ""),
        )
    return render_template(
        "edit.html",
        resource=resource,
        form=dict(resource),
        labels=" ".join(resources.labels_of(conn, resource_id)),
    )


@app.post("/resources/<int:resource_id>/delete")
@auth.login_required
def delete_resource(resource_id):
    conn = db.get_db()
    resource = resources.get(conn, resource_id)
    if resource is None:
        abort(404)
    if not can_manage(resource):
        abort(403, "Only the owner or an admin can delete this resource.")
    resources.delete(conn, resource_id)
    flash(f"Deleted {resource['name']}.", "ok")
    return redirect(url_for("index"))


# --- claiming ---------------------------------------------------------------


@app.post("/resources/<int:resource_id>/acquire")
@auth.login_required
def acquire(resource_id):
    try:
        resources.acquire(
            db.get_db(), resource_id, g.user["id"], request.form.get("note")
        )
    except resources.Error as exc:
        flash(str(exc), "error")
    else:
        flash("Resource locked.", "ok")
    return redirect(safe_back(anchor(resource_id)))


@app.post("/claims/<int:claim_id>/release")
@auth.login_required
def release(claim_id):
    conn = db.get_db()
    claim = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    if claim is None:
        abort(404)
    resource = resources.get(conn, claim["resource_id"])
    try:
        resources.release(conn, claim_id, g.user["id"], can_manage(resource))
    except resources.Error as exc:
        flash(str(exc), "error")
    else:
        flash("Resource released.", "ok")
    return redirect(safe_back(anchor(resource["id"])))


# --- API tokens -------------------------------------------------------------


@app.get("/settings")
@auth.login_required
def settings():
    return render_template(
        "settings.html",
        tokens=auth.list_tokens(db.get_db(), g.user["id"]),
        new_token=session.pop("new_token", None),
    )


@app.post("/settings/tokens")
@auth.login_required
def create_token():
    row, token = auth.create_token(db.get_db(), g.user["id"], request.form.get("name"))
    # Shown once on the next page load, then gone: only the hash is stored.
    session["new_token"] = {"name": row["name"], "token": token}
    return redirect(url_for("settings"))


@app.post("/settings/tokens/<int:token_id>/revoke")
@auth.login_required
def revoke_token(token_id):
    auth.revoke_token(db.get_db(), token_id, g.user["id"])
    flash("Token revoked.", "ok")
    return redirect(url_for("settings"))


@app.get("/api/docs")
@auth.login_required
def api_docs():
    # Rendered from the same document we serve at /api/openapi.json, so the
    # page cannot drift from the API and needs no third-party assets.
    return render_template("docs.html", spec=openapi.spec())


# --- administration ---------------------------------------------------------


@app.get("/admin/users")
@auth.admin_required
def admin_users():
    users = db.get_db().execute(
        "SELECT u.*,"
        " (SELECT COUNT(*) FROM resources r WHERE r.owner_id = u.id) AS owned,"
        " (SELECT COUNT(*) FROM claims c"
        "  WHERE c.user_id = u.id AND c.released_at IS NULL) AS holding"
        " FROM users u ORDER BY u.username COLLATE NOCASE"
    ).fetchall()
    return render_template("admin.html", users=users)


@app.post("/admin/users/<int:user_id>/password")
@auth.admin_required
def admin_set_password(user_id):
    try:
        auth.set_password(
            db.get_db(),
            user_id,
            request.form.get("password"),
            request.form.get("confirm"),
        )
    except auth.Error as exc:
        flash(str(exc), "error")
    else:
        flash("Password changed; that user's API tokens were revoked.", "ok")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<int:user_id>/admin")
@auth.admin_required
def admin_toggle_admin(user_id):
    try:
        auth.set_admin(db.get_db(), user_id, request.form.get("grant") == "1")
    except auth.Error as exc:
        flash(str(exc), "error")
    else:
        flash("Admin rights updated.", "ok")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<int:user_id>/delete")
@auth.admin_required
def admin_delete_user(user_id):
    try:
        user = auth.delete_user(db.get_db(), user_id, g.user["id"])
    except auth.Error as exc:
        flash(str(exc), "error")
    else:
        flash(f"Deleted {user['username']}; their resources are now yours.", "ok")
    return redirect(url_for("admin_users"))


if __name__ == "__main__":
    db.init()
    app.run(
        host=os.environ.get("SEMAPHORE_HOST", "127.0.0.1"),
        port=int(os.environ.get("SEMAPHORE_PORT", 5000)),
        debug=bool(os.environ.get("SEMAPHORE_DEBUG")),
    )
