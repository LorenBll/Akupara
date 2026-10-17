"""Unit tests for session helpers in the auth layer."""

import time

import auth
import state


def test_prune_expired_sessions():
    state._SESSION_STORE.clear()
    state._SESSION_STORE["old"] = {"username": "u", "admin": False, "last_refresh": time.time() - 100000}
    state._SESSION_STORE["new"] = {"username": "u", "admin": False, "last_refresh": time.time()}
    auth._prune_expired_sessions()
    assert "old" not in state._SESSION_STORE
    assert "new" in state._SESSION_STORE


def test_generate_session_token():
    token = auth._generate_session_token()
    assert isinstance(token, str)
    assert token


def test_delete_session_username():
    state._SESSION_STORE.clear()
    state._SESSION_STORE["t1"] = {"username": "alice1234", "admin": False, "last_refresh": time.time()}
    state._SESSION_STORE["t2"] = {"username": "bob123456", "admin": False, "last_refresh": time.time()}
    auth._delete_session_username("alice1234")
    assert "t1" not in state._SESSION_STORE
    assert "t2" in state._SESSION_STORE
