# Semaphore

A small Flask app for sharing lab hardware: remote machines, test devices, dev boards —
anything where people need to know *who is using it right now*.

Each resource is a counting semaphore. It has a number of slots; you take a slot while you
work and release it when you are done. One slot means exclusive access, which is the usual
case for a single piece of hardware.

## Features

- Username/password login with PBKDF2-hashed passwords and a signed session cookie.
  Password fields have a **Show/Hide** switch so you can check what you typed.
- Create resources with a free-text description of what they are and what they do.
- Labels per resource (`hardware`, `rack-b`, `can-bus`, ...) with one-click filtering plus
  a name/description search.
- Everything happens on one page: there is no separate detail view. Each entry in the list
  shows its description, labels, live occupancy (`1 / 2 free`), who is holding it, for how
  long, and their comment.
- Lock straight from the list: type a comment in the field next to the **Lock** button and
  the comment is recorded with your lock. Releasing happens in the same place.
- **Your resources** at the top is a shortcut to everything you currently hold.
- Recent history per resource, collapsed behind a **Recent history** toggle so it stays out
  of the way until you want it.
- The owner of a resource can edit it, delete it, or force-release a slot someone forgot.
- **Admin mode:** manage users, reset passwords, grant or revoke admin, delete accounts.
- **REST API** with bearer tokens for locking from scripts and CI, described in OpenAPI 3.1.

## Admin mode

The first account created becomes the admin; on an existing database, the oldest account is
promoted on the next start. Admins get an **Admin** link in the header leading to
`/admin/users`, where they can:

- set a new password for any user (which also revokes that user's API tokens),
- grant or revoke admin rights,
- delete an account.

Deleting an account transfers the resources it owned to the admin performing the deletion
and removes that account's claim history. The app refuses to delete your own account or to
remove the last remaining admin, so you cannot lock yourself out. Admins can also edit,
delete and force-release any resource, not just the ones they own.

## REST API

Create a token under **API tokens** in the header. The token is shown once and only its
SHA-256 hash is stored, so copy it when it appears. Send it as a bearer token:

```bash
# Lock one specific device
curl -X POST http://127.0.0.1:5000/api/v1/locks \
  -H "Authorization: Bearer sem_..." -H "Content-Type: application/json" \
  -d '{"resource": "lab-pi-03", "note": "nightly firmware run"}'

# Or lock whatever is free with a given label
curl -X POST http://127.0.0.1:5000/api/v1/locks \
  -H "Authorization: Bearer sem_..." -H "Content-Type: application/json" \
  -d '{"label": "rack-b", "note": "CI job 4711"}'

# Release it again
curl -X DELETE http://127.0.0.1:5000/api/v1/locks/42 -H "Authorization: Bearer sem_..."
```

A `201` means the slot is yours and the response says which device you got. A `409` means
everything matching is busy, which is the status to retry on.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/me` | Who the token belongs to |
| `GET` | `/api/v1/resources?label=&free=` | List resources with live occupancy |
| `GET` | `/api/v1/resources/{id-or-name}` | One resource and its current holders |
| `POST` | `/api/v1/locks` | Lock by `resource` or by `label` |
| `GET` | `/api/v1/locks` | Locks you currently hold |
| `DELETE` | `/api/v1/locks/{lock_id}` | Release a lock |

The OpenAPI 3.1 document is served at `/api/openapi.json`; point Postman, Swagger UI or a
client generator at it. Browsable documentation is at `/api/docs`, rendered server-side
from that same document, so it needs no internet access and cannot drift from the API.

API requests authenticate by token only — session cookies are deliberately ignored there,
which is what keeps the endpoints out of reach of cross-site requests.

## Running it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Then open http://127.0.0.1:5000 and create an account. The first thing to do is add a
resource; the description field is where you explain what the machine is for and how to
reach it.

Tests:

```bash
.venv/bin/python -m unittest test_app -v
```

## Configuration

All optional, via environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SEMAPHORE_DB` | `./data.db` | SQLite database file |
| `SEMAPHORE_HOST` | `127.0.0.1` | Bind address; use `0.0.0.0` to share on the network |
| `SEMAPHORE_PORT` | `5000` | Port |
| `SEMAPHORE_SECRET` | generated into `.secret_key` | Session signing key |
| `SEMAPHORE_DEBUG` | off | Set to any value for Flask's debug mode |

## Layout

| File | Contents |
| --- | --- |
| `app.py` | Web routes, request setup, app config |
| `api.py` | The token-authenticated REST API |
| `openapi.py` | The OpenAPI 3.1 document served at `/api/openapi.json` |
| `auth.py` | Registration, password hashing, admin actions, API tokens, CSRF |
| `resources.py` | Resource CRUD and the acquire/release logic |
| `db.py` | SQLite connection handling, schema and migrations |
| `templates/`, `static/` | Jinja templates, stylesheet, the Show/Hide password script |

The schema is applied and migrated on every start, so upgrading in place just means
restarting the app; existing data is preserved.

## Notes before putting it on a shared network

`app.py` runs Flask's development server, which is fine for a team on a trusted LAN but not
for anything exposed publicly. For wider use, serve it through a production WSGI server
behind HTTPS, set `SEMAPHORE_SECRET` explicitly, and add
`SESSION_COOKIE_SECURE=True` to the app config. Registration is currently open to anyone who
can reach the page.
