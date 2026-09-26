"""Minimal request handlers (framework-agnostic)."""

from taskboard.auth import AuthError, verify_token
from taskboard.users import UserStore, login


def handle_login(store: UserStore, body: dict) -> tuple[int, dict]:
    """POST /login"""
    try:
        token = login(store, body.get("username", ""), body.get("password", ""))
    except AuthError:
        return 401, {"error": "invalid credentials"}
    return 200, {"access_token": token, "token_type": "bearer"}


def _current_user(store: UserStore, headers: dict):
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise AuthError("missing bearer token")
    user = store.by_id(verify_token(auth.removeprefix("Bearer ")))
    if user is None:
        raise AuthError("unknown user")
    return user


def handle_me(store: UserStore, headers: dict) -> tuple[int, dict]:
    """GET /me"""
    try:
        user = _current_user(store, headers)
    except AuthError as exc:
        return 401, {"error": str(exc)}
    return 200, {"id": user.id, "username": user.username}


def handle_tasks(store: UserStore, headers: dict) -> tuple[int, dict]:
    """GET /tasks"""
    try:
        user = _current_user(store, headers)
    except AuthError as exc:
        return 401, {"error": str(exc)}
    return 200, {"tasks": list(user.tasks)}
