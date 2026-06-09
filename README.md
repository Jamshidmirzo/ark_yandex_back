# ark_yandex

Django 5.2 project (Python 3.13).

## Layout

```
config/        project settings, urls, wsgi/asgi
core/          first app (health endpoint + sample test)
manage.py
requirements.txt        runtime deps
requirements-dev.txt    dev/test/lint deps
.env / .env.example     environment config (django-environ)
```

## Setup

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # then edit SECRET_KEY etc.
python manage.py migrate
```

## Run

```bash
source .venv/bin/activate
python manage.py runserver
# http://127.0.0.1:8000/health/   -> {"status": "ok"}
# http://127.0.0.1:8000/admin/
```

Create an admin user: `python manage.py createsuperuser`

## Test & lint

```bash
pytest          # tests
ruff check .    # lint
ruff format .   # or: black .
```

## Configuration

Settings are read from environment variables (see `.env.example`):

- `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`
- `DATABASE_URL` — unset = SQLite; set to `postgres://...` for Postgres.
