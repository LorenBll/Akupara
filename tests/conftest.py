"""Shared fixtures for the Akupara test suite.

Isolation strategy — the codebase relies on module-level globals
(``src/state.py``), a root ``.env`` (``src/env_store.py``), JSON log files
(``src/logginglib``) and audio side effects (``src/audio.py``). Tests must
never read or replace the developer's real data, so every fixture redirects
those resources to a per-test temporary location:

* ``env_store._env_path`` -> a per-test temporary ``.env``.
* ``dotenv.load_dotenv`` -> neutralised, so the real repo ``.env`` is never read.
* ``audio.play_sound`` -> silenced, so no audio subprocess is spawned.
* ``logginglib.init_logging`` -> a per-test temporary log directory.
* ``state`` globals -> reset before every test.
"""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

import pytest

# Ensure src/ is importable even without the ``pythonpath`` ini option.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import dotenv  # noqa: E402

import audio  # noqa: E402
import auth  # noqa: E402
import config  # noqa: E402
import env_store  # noqa: E402
import logginglib  # noqa: E402
import main  # noqa: E402
import state  # noqa: E402

# A valid-looking Argon2id hash used as a stand-in for a real client hash. It is
# never stored: ``make_user`` wraps it with :func:`auth._wrap_password`, and a
# login that submits it is verified against that verifier. No real Argon2
# computation is needed, because the server only compares the tag.
SENTINEL_HASH = "$argon2id$test-sentinel-hash"

# A well-formed 16-byte base64url client salt (22 characters, no padding).
TEST_CLIENT_SALT = "abcdefghijklmnopqrstuv"

_ENV_KEYS = [
    "USERS", "API_KEYS", "API_KEY_ENCRYPTION_KEY", "INTERNAL_INTERACTIONS",
    "API_KEYS_ENABLED", "DISPLAY_PROMOTION", "SHARED_MEMORY",
    "SHARED_MEMORY_ENABLED", "PLAY_AUDIOS", "PLAY_LOG_SOUNDS",
    "PLAY_STARTUP_SOUND", "STARTUP_SOUND", "ACKNOWLEDGE_SOUND", "WARN_SOUND",
    "PROCESS_SOUND", "SUCCESS_SOUND", "ERROR_SOUND",
    "EXTERNAL_INTERACTIONS", "EXTERNAL_INTERACTIONS_INCOMING_IPS",
    "EXTERNAL_INTERACTIONS_OUTGOING_IPS", "EXTERNAL_INTERACTIONS_ALLOW_NEW",
    "EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING", "AUTOMATIC_UPDATE",
    "AUTOMATIC_PLUGIN_LIBRARY_UPDATE", "AUTOMATIC_PLUGIN_UPGRADE",
    "PLUGIN_EVENT_SUBSCRIPTIONS", "GITHUB_TOKEN",
]

TEST_CONFIG = {"port": 49155, "guiEnabled": True, "development": False}

_STATE_DEFAULTS = {
    "SERVICE_HOST": "127.0.0.1",
    "SERVICE_PORT": None,
    "GUI_ENABLED": True,
    "DEVELOPMENT": False,
    "INTERNAL_INTERACTIONS": False,
    "API_KEYS_ENABLED": False,
    "DISPLAY_PROMOTION": True,
    "PLAY_AUDIOS": True,
    "PLAY_LOG_SOUNDS": False,
    "PLAY_STARTUP_SOUND": True,
    "SHARED_MEMORY_ENABLED": False,
    "EXTERNAL_INTERACTIONS": False,
    "EXTERNAL_INTERACTIONS_ALLOW_NEW": False,
    "EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING": False,
    "AUTOMATIC_UPDATE": False,
    "AUTOMATIC_PLUGIN_LIBRARY_UPDATE": False,
    "AUTOMATIC_PLUGIN_UPGRADE": False,
}


def _reset_state() -> None:
    """Restore every mutable ``state`` global to its declared default."""
    for key, value in _STATE_DEFAULTS.items():
        setattr(state, key, value)
    state._UPDATE_AVAILABLE = False
    state._UPDATE_AVAILABLE_AT_STARTUP = False
    state._PLUGIN_UPDATE_AVAILABLE = False
    state._PLUGIN_UPDATE_AVAILABLE_AT_STARTUP = False
    state._INSTALLED_PLUGINS_PENDING_UPGRADES = []
    state._PROJECT_INTEGRITY_OK = False
    state._PLUGIN_INTEGRITY_OK = False
    state._external_interactions_worker = None
    state._CONFIG_CACHE = None
    state._FAILED_LOGIN_ATTEMPTS = 0
    state._LOCAL_ADDR_CACHE = None
    state._LOCAL_ADDR_CACHE_TS = 0.0
    state._SETTINGS_STATIC_CACHE = None
    state._SETTINGS_STATIC_TS = 0.0
    state._SESSION_STORE.clear()
    state._api_key_store.clear()
    # ``state.app`` is intentionally left pointing at the Flask app.


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    _reset_state()
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path_factory, monkeypatch):
    env_file = tmp_path_factory.mktemp("env") / ".env"
    monkeypatch.setattr(env_store, "_env_path", lambda: env_file)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    return env_file


@pytest.fixture(autouse=True)
def _isolate_password_pepper(tmp_path_factory, monkeypatch):
    """Give every test its own settled installation, away from the real resources.

    The real ``resources/password-pepper`` and ``resources/installation.json``
    must never be touched, and most tests need a usable pepper, so the fixture
    initialises one in a temporary directory. The pepper-policy tests reset that
    state to exercise a new or a pepper-less installation.
    """
    resource_dir = tmp_path_factory.mktemp("pepper")
    pepper_file = resource_dir / "password-pepper"
    marker_file = resource_dir / "installation.json"
    monkeypatch.setattr(auth, "_password_pepper_path", lambda: pepper_file)
    monkeypatch.setattr(auth, "_installation_marker_path", lambda: marker_file)
    auth._reset_password_pepper_cache()
    auth._initialize_password_storage()
    yield pepper_file
    auth._reset_password_pepper_cache()


@pytest.fixture(autouse=True)
def _silence_audio(monkeypatch):
    monkeypatch.setattr(audio, "play_sound", lambda event, via_log=False: None)


@pytest.fixture(autouse=True)
def _isolate_logging(tmp_path_factory):
    log_dir = tmp_path_factory.mktemp("logs")
    logginglib.init_logging("Akupara", debug=False, log_dir=log_dir)
    return log_dir


# Register the UI routes once (they are only registered in ``main.__main__`` in
# the real application). The Flask app is a module-level singleton, so this must
# happen once, not once per test.
if state.GUI_ENABLED:
    main._register_ui_routes(main.app)


@pytest.fixture
def client(monkeypatch):
    _reset_state()
    monkeypatch.setattr(config, "_load_configuration", lambda: dict(TEST_CONFIG))
    main._initialize_service_config()
    main.app.config["TESTING"] = True
    return main.app.test_client()


# --- helper fixtures (return callables) ------------------------------------

@pytest.fixture
def make_user():
    def _make(username, password_hash=SENTINEL_HASH, admin=False, root=False, user_id=None, client_salt=None):
        raw = env_store.read_env_var("USERS") or "[]"
        try:
            users = json.loads(raw) if raw else []
        except (json.JSONDecodeError, ValueError, TypeError):
            users = []
        if not isinstance(users, list):
            users = []
        entry = {
            "username": username,
            # Accounts store a peppered verifier, never the client hash itself.
            "password": auth._wrap_password(password_hash),
            "client_salt": client_salt or TEST_CLIENT_SALT,
            "admin": bool(admin),
            "root": bool(root),
            "id": user_id or secrets.token_hex(16),
        }
        users.append(entry)
        env_store.write_env_var("USERS", json.dumps(users, ensure_ascii=False))
        return username
    return _make


@pytest.fixture
def login():
    def _login(client, username, password_hash=SENTINEL_HASH):
        return client.post("/api/login", json={"username": username, "password_hash": password_hash})
    return _login


@pytest.fixture
def login_admin(make_user, login):
    def _login_admin(client, username="adminuser"):
        make_user(username, admin=True)
        login(client, username)
    return _login_admin


@pytest.fixture
def enable_api_keys():
    def _enable():
        state.API_KEYS_ENABLED = True
        if not env_store.read_env_var("API_KEY_ENCRYPTION_KEY"):
            from cryptography.fernet import Fernet
            env_store.write_env_var("API_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return _enable


@pytest.fixture
def make_api_key(enable_api_keys):
    def _make(name="test-key"):
        enable_api_keys()
        return auth._create_api_key(name)["key"]
    return _make
