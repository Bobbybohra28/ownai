import pytest

from taskboard.api import handle_login, handle_me
from taskboard.auth import AuthError, create_token, hash_password, verify_password, verify_token
from taskboard.users import UserStore


def test_password_roundtrip():
    stored = hash_password("correct horse")
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong", stored)


def test_fresh_token_is_accepted():
    token = create_token("u1", now=1_000)
    assert verify_token(token, now=1_010) == "u1"


def test_expired_token_is_rejected():
    token = create_token("u1", now=1_000)
    with pytest.raises(AuthError):
        verify_token(token, now=1_000 + 10_000)


def test_tampered_token_is_rejected():
    token = create_token("u1", now=1_000)
    user, exp, sig = token.split(".")
    with pytest.raises(AuthError):
        verify_token(f"u2.{exp}.{sig}", now=1_010)


def test_login_then_me():
    store = UserStore()
    store.add("u1", "alice", "s3cret-pass")
    status, body = handle_login(store, {"username": "alice", "password": "s3cret-pass"})
    assert status == 200
    status, me = handle_me(store, {"Authorization": f"Bearer {body['access_token']}"})
    assert status == 200 and me["username"] == "alice"
