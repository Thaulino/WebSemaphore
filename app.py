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
    url_for,
)

import auth
import db
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


@app.before_request
def before_request():
    auth.load_user()
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
    return datetime.datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M")


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
    return render_template(
        "index.html",
        items=resources.listing(conn, label=label, query=query),
        labels=resources.all_labels(conn),
        mine=resources.my_claims(conn, g.user["id"]),
        active_label=label,
        query=query or "",
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
            return redirect(url_for("resource_detail", resource_id=resource_id))
    return render_template(
        "edit.html", resource=None, form=request.form.to_dict(), labels=""
    )


@app.get("/resources/<int:resource_id>")
@auth.login_required
def resource_detail(resource_id):
    conn = db.get_db()
    resource = resources.get(conn, resource_id)
    if resource is None:
        abort(404)
    active = resources.active_claims(conn, resource_id)
    return render_template(
        "resource.html",
        resource=resource,
        labels=resources.labels_of(conn, resource_id),
        active=active,
        history=resources.history(conn, resource_id),
        free=resource["capacity"] - len(active),
        held_by_me=any(c["user_id"] == g.user["id"] for c in active),
        is_owner=resource["owner_id"] == g.user["id"],
    )


@app.route("/resources/<int:resource_id>/edit", methods=("GET", "POST"))
@auth.login_required
def edit_resource(resource_id):
    conn = db.get_db()
    resource = resources.get(conn, resource_id)
    if resource is None:
        abort(404)
    if resource["owner_id"] != g.user["id"]:
        abort(403, "Only the owner can edit this resource.")
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
            return redirect(url_for("resource_detail", resource_id=resource_id))
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
    if resource["owner_id"] != g.user["id"]:
        abort(403, "Only the owner can delete this resource.")
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
        flash("Slot is yours.", "ok")
    return redirect(url_for("resource_detail", resource_id=resource_id))


@app.post("/claims/<int:claim_id>/release")
@auth.login_required
def release(claim_id):
    conn = db.get_db()
    claim = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    if claim is None:
        abort(404)
    resource = resources.get(conn, claim["resource_id"])
    try:
        resources.release(
            conn, claim_id, g.user["id"], resource["owner_id"] == g.user["id"]
        )
    except resources.Error as exc:
        flash(str(exc), "error")
    else:
        flash("Slot released.", "ok")
    return redirect(
        request.form.get("back") or url_for("resource_detail", resource_id=resource["id"])
    )


if __name__ == "__main__":
    db.init()
    app.run(
        host=os.environ.get("SEMAPHORE_HOST", "127.0.0.1"),
        port=int(os.environ.get("SEMAPHORE_PORT", 5000)),
        debug=bool(os.environ.get("SEMAPHORE_DEBUG")),
    )
