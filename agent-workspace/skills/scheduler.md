# Scheduler tasks

Use the scheduler REST API on the configured port to manage tasks:

- `POST /api/tasks`
- `GET /api/tasks`
- `GET /api/tasks/<id>`
- `PUT /api/tasks/<id>`
- `DELETE /api/tasks/<id>`
- `POST /api/tasks/<id>/run`
- `GET /api/health`

Cron supports `*`, `*/N`, `N-M`, and `N,M` in the five standard fields.
