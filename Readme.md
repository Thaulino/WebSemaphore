# Semaphore

A small Flask app for sharing lab hardware: remote machines, test devices, dev boards —
anything where people need to know *who is using it right now*.

Each resource is a counting semaphore. It has a number of slots; you take a slot while you
work and release it when you are done. One slot means exclusive access, which is the usual
case for a single piece of hardware.

## Features

- Username/password login with PBKDF2-hashed passwords and a signed session cookie.
- Create resources with a free-text description of what they are and what they do.
- Labels per resource (`hardware`, `rack-b`, `can-bus`, ...) with one-click filtering plus
  a name/description search.
- Take and release slots, with an optional note saying what you are doing.
- Live occupancy (`1 / 2 free`), who is holding each slot and for how long.
- Recent history per resource, so you can see who had the device before you.
- The owner of a resource can edit it, delete it, or force-release a slot someone forgot.

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
| `app.py` | Routes, request setup, app config |
| `auth.py` | Registration, password hashing, login guard, CSRF |
| `resources.py` | Resource CRUD and the acquire/release logic |
| `db.py` | SQLite connection handling and schema |
| `templates/`, `static/` | Jinja templates and stylesheet |

## Notes before putting it on a shared network

`app.py` runs Flask's development server, which is fine for a team on a trusted LAN but not
for anything exposed publicly. For wider use, serve it through a production WSGI server
behind HTTPS, set `SEMAPHORE_SECRET` explicitly, and add
`SESSION_COOKIE_SECURE=True` to the app config. Registration is currently open to anyone who
can reach the page.
