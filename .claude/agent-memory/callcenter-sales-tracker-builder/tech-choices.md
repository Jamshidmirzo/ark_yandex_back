---
name: naffAI tech choices
description: Locked tech-stack and architecture decisions specific to naffAI
type: project
---

Stack: Django 5.x + DRF 3.15+, PostgreSQL 16, openpyxl, drf-spectacular; React 18 + Vite + TS +
Tailwind + Recharts + zustand + react-query; aiogram for the Telegram bot. Python pinned to
3.12, packaged with `uv`. Frontend is a React SPA, not HTMX.

Layering follows HackSoft styleguide strictly:
`models.py` (pure data shapes) → `selectors.py` (read) → `services.py` (write, transactional) →
`apis.py` (thin DRF views) → `urls.py`. **Audit log is written explicitly from service
functions, never from signals.** Money is `Decimal(14, 2)` everywhere — never `float`.

Auth: Django session + DRF TokenAuth. Three roles via `apps.users.Profile.role`:
`team_lead` (write), `manager` (read-only), `operator` (own scope). Default superuser is
bootstrapped from `DJANGO_SUPERUSER_USERNAME/PASSWORD` env vars by the entrypoint.

Why: spec mandated HackSoft + Decimal + explicit audit; React SPA was the documented default;
uv chosen because the user's machine already had Homebrew Python 3.13 (uv installs/manages
3.12 itself for the project).

How to apply: when adding a new endpoint or domain rule, do not put logic in serializers or
views — refactor into a `services.py` function and call from a thin `perform_create`/POST view.
Any new write must produce an `AuditLog` entry via `apps.audit.services.audit_log_create`.
