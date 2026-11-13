# Django interview starter

A minimal, working hello-world project for a full-stack interview. Python handles the backend; Django templates, HTML, CSS, and plain JavaScript handle the frontend. SQLite is configured and initialized. No Node.js, Docker, API keys, or separate frontend server needed.

This matches the setup described in Jenna's email: an empty project with a working hello-world frontend and backend. The language and framework are agnostic, and an optional interview scaffold may be provided. No interview feature or practice solution is prebuilt here.

## Start on this Mac

The dependencies and a separate Python 3.12 runtime are already installed inside this folder.

```sh
cd /Users/eddiekao/Desktop/django-interview-starter
./start.command
```

Open **http://127.0.0.1:8001**. Click **Say hello to the backend** to make a real request to Python. The status below it checks both the backend and a real SQLite query. Press **Ctrl+C** in the terminal to stop the server. You can also double-click `start.command` in Finder. Port 8001 is the default because another app on this Mac already uses port 8000.

If port 8001 is busy, use `PORT=8002 ./start.command` and open http://127.0.0.1:8002.

## Setup on another machine

Use a working Python 3.12 installation (Python 3.10–3.14 also supported by this Django release). Do not copy `.venv`, `.python`, or `.tools` between machines.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./start.command
```

On this Mac, the preexisting Homebrew Python has a broken native XML library dependency. This project uses its own runtime under `.python/`, copied from the prepared starter, leaving system installations unchanged. Keep `.python/` when running the existing environment. If you move the project, recreate `.venv` with a working interpreter.

## Where to code during the interview

| File | Purpose |
| --- | --- |
| `core/views.py` | Python request handlers and JSON endpoints |
| `core/urls.py` | URL routes |
| `core/models.py` | Add database models when the prompt calls for them |
| `core/templates/core/home.html` | Frontend HTML / Django template |
| `core/static/core/app.js` | Browser behavior and API requests |
| `core/static/core/styles.css` | Styling |
| `config/settings.py` | Project configuration and SQLite database |
| `core/tests.py` | Checks for the scaffold |

Request flow: browser → URL route → Python view → HTML or JSON response. JavaScript's `fetch()` calls `/api/hello/` on the same server, so no CORS configuration is necessary.

### Current endpoints

- `GET /`: renders the frontend.
- `GET /api/hello/`: returns `{"message": "Hello, world! From your Python backend."}`.
- `GET /api/health/`: runs `SELECT 1` in SQLite and returns `{"status": "ok", "database": "connected"}`; returns 503 if the database connection fails.
- `/admin/`: standard Django admin; create a user only if needed with `.venv/bin/python manage.py createsuperuser`.

### Useful commands

Run these from this folder, in a second terminal if the server is running:

```sh
.venv/bin/python manage.py check
.venv/bin/python manage.py test
.venv/bin/python manage.py makemigrations  # after adding/changing models
.venv/bin/python manage.py migrate
.venv/bin/python manage.py shell
```

Python changes reload automatically. Refresh the browser after changing templates, CSS, or JavaScript. Future POST forms should include `{% csrf_token %}`; future JavaScript writes should send Django's CSRF token in the `X-CSRFToken` header. Standard CSRF middleware remains enabled.

Before the interview, start the server, click the hello button, check the green database status, and keep the project open in your editor. The launch script automatically applies migrations.

This is a local development scaffold: debug mode, a development-only secret key, and Django's development server. Runtime folders and the local database are excluded from Git; `requirements.txt` pins installed dependencies for reproducible setup.

Official reference: [Django 5.2 tutorial](https://docs.djangoproject.com/en/5.2/intro/tutorial01/).
