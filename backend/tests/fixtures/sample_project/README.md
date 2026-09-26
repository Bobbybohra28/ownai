# TaskBoard

A small task-tracking service used as the OwnAI end-to-end fixture.

- `taskboard/auth.py` — password hashing and signed, expiring access tokens
- `taskboard/users.py` — user store and login
- `taskboard/api.py` — request handlers (`POST /login`, `GET /me`, `GET /tasks`)
- `taskboard/config.py` — configuration from environment variables

Run the tests with `python -m pytest -q`.
