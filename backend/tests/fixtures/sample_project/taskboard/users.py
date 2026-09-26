"""In-memory user store and login flow."""

from dataclasses import dataclass, field

from taskboard.auth import AuthError, create_token, hash_password, verify_password


@dataclass
class User:
    id: str
    username: str
    password_hash: str
    tasks: list[str] = field(default_factory=list)


class UserStore:
    def __init__(self) -> None:
        self._users: dict[str, User] = {}

    def add(self, user_id: str, username: str, password: str) -> User:
        user = User(user_id, username, hash_password(password))
        self._users[username] = user
        return user

    def by_username(self, username: str) -> User | None:
        return self._users.get(username)

    def by_id(self, user_id: str) -> User | None:
        return next((u for u in self._users.values() if u.id == user_id), None)


def login(store: UserStore, username: str, password: str, now: float | None = None) -> str:
    """Authenticate a user and return a fresh access token."""
    user = store.by_username(username)
    if user is None or not verify_password(password, user.password_hash):
        raise AuthError("invalid username or password")
    return create_token(user.id, now=now)
