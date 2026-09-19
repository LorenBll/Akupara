"""Central application state — all mutable globals for Akupara.

This module is the single source of truth for service-wide flags, caches and
stores. Sub-modules import this file and read/write ``state.VAR`` directly.
"""

from __future__ import annotations

import threading
import traceback
from pathlib import Path


def _format_exc() -> str:
    return traceback.format_exc()


# ---------------------------------------------------------------------------
# Service / feature flags (populated by config._initialize_service_config)
# ---------------------------------------------------------------------------
SERVICE_HOST: str = "127.0.0.1"
SERVICE_PORT: int | None = None

GUI_ENABLED: bool = True
DEVELOPMENT: bool = False

INTERNAL_INTERACTIONS: bool = False
API_KEYS_ENABLED: bool = False
DISPLAY_PROMOTION: bool = True
PLAY_AUDIOS: bool = True
PLAY_LOG_SOUNDS: bool = False
PLAY_STARTUP_SOUND: bool = True
SHARED_MEMORY_ENABLED: bool = False
EXTERNAL_INTERACTIONS: bool = False
EXTERNAL_INTERACTIONS_ALLOW_NEW: bool = False
EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING: bool = False

AUTOMATIC_UPDATE: bool = False
AUTOMATIC_PLUGIN_LIBRARY_UPDATE: bool = False
AUTOMATIC_PLUGIN_UPGRADE: bool = False

# ---------------------------------------------------------------------------
# Update / integrity caches
# ---------------------------------------------------------------------------
_UPDATE_AVAILABLE: bool = False
_UPDATE_AVAILABLE_AT_STARTUP: bool = False
_PLUGIN_UPDATE_AVAILABLE: bool = False
_PLUGIN_UPDATE_AVAILABLE_AT_STARTUP: bool = False
_INSTALLED_PLUGINS_PENDING_UPGRADES: list[dict] = []
_PROJECT_INTEGRITY_OK: bool = False
_PLUGIN_INTEGRITY_OK: bool = False

# External-interactions worker handle (set by firewall/worker helpers)
_external_interactions_worker = None  # : network.ExternalInteractionsWorker | None

# Config cache
_CONFIG_CACHE: dict | None = None

# ---------------------------------------------------------------------------
# Session / login
# ---------------------------------------------------------------------------
SESSION_COOKIE_NAME: str = "akupara-refresh"
_SESSION_STORE: dict[str, dict] = {}
_SESSION_MAX_AGE: int = 900
_SESSION_LOCK = threading.Lock()

_FAILED_LOGIN_ATTEMPTS: int = 0
_FAILED_LOGIN_LOCK = threading.Lock()
_MAX_FAILED_LOGIN_WARNS: int = 3

# ---------------------------------------------------------------------------
# Local address cache
# ---------------------------------------------------------------------------
_LOCAL_ADDR_CACHE: set[str] | None = None
_LOCAL_ADDR_CACHE_TS: float = 0.0
_LOCAL_ADDR_TTL: float = 30.0
_LOCAL_ADDR_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Settings card helpers cache
# ---------------------------------------------------------------------------
_SETTINGS_STATIC_CACHE: dict | None = None
_SETTINGS_STATIC_TS: float = 0.0
_SETTINGS_STATIC_TTL: float = 30.0

# ---------------------------------------------------------------------------
# Firewall / external-interactions constants
# ---------------------------------------------------------------------------
_EXTERNAL_INTERACTIONS_ACTIONS = ("allow", "unknown", "block")
_EXTERNAL_INTERACTIONS_DIRECTIONS = ("incoming", "outgoing")
_INCOMING_IPS_VAR: str = "EXTERNAL_INTERACTIONS_INCOMING_IPS"
_OUTGOING_IPS_VAR: str = "EXTERNAL_INTERACTIONS_OUTGOING_IPS"

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
_FORBIDDEN_KEY_NAME_CHARS: set[str] = set(" ,;:\\/%\"'")

# ---------------------------------------------------------------------------
# Project hash
# ---------------------------------------------------------------------------
_PROJECT_HASH_EXCLUDE_DIRS = {".git", ".venv", "venv", "ENV", "env", ".vscode", ".idea", "logs", "__pycache__", ".pytest_cache", "htmlcov", "dist", "build", ".github/__pycache__"}
_PROJECT_HASH_EXCLUDE_SUFFIXES = (".pyc", ".pyo")

STARTUP_SOUND_FILE: str = "logo-reveal.wav"

# ---------------------------------------------------------------------------
# Settings cards (used for on-demand loading)
# ---------------------------------------------------------------------------
_SETTINGS_CARDS: list[dict] = [
    {
        "id": "general",
        "title": "General",
        "fragment": "general.html",
        "roles": ("all",),
        "icon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
    },
    {
        "id": "plugins",
        "title": "Plugins",
        "fragment": "plugins.html",
        "roles": ("admin",),
        "icon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15.39 4.39a1 1 0 0 0 1.68-.474 2.5 2.5 0 1 1 3.014 3.015 1 1 0 0 0-.474 1.68l1.683 1.682a2.414 2.414 0 0 1 0 3.414L19.61 15.39a1 1 0 0 1-1.68-.474 2.5 2.5 0 1 0-3.014 3.015 1 1 0 0 1 .474 1.68l-1.683 1.682a2.414 2.414 0 0 1-3.414 0L8.61 19.61a1 1 0 0 0-1.68.474 2.5 2.5 0 1 1-3.014-3.015 1 1 0 0 0 .474-1.68l-1.683-1.682a2.414 2.414 0 0 1 0-3.414L4.39 8.61a1 1 0 0 1 1.68.474 2.5 2.5 0 1 0 3.014-3.015 1 1 0 0 1-.474-1.68l1.683-1.682a2.414 2.414 0 0 1 3.414 0z"/></svg>',
    },
    {
        "id": "api-keys",
        "title": "API Keys",
        "fragment": "api-keys.html",
        "roles": ("admin",),
        "icon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2.586 17.414A2 2 0 0 0 2 18.828V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.172a2 2 0 0 0 1.414-.586l.814-.814a6.5 6.5 0 1 0-4-4z"/><circle cx="16.5" cy="7.5" r=".5" fill="currentColor"/></svg>',
    },
    {
        "id": "internal-interactions",
        "title": "Internal Interactions",
        "fragment": "internal-interactions.html",
        "roles": ("admin",),
        "icon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 10a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 14.286V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/><path d="M20 9a2 2 0 0 1 2 2v10.286a.71.71 0 0 1-1.212.502l-2.202-2.202A2 2 0 0 0 17.172 19H10a2 2 0 0 1-2-2v-1"/></svg>',
    },
    {
        "id": "external-interactions",
        "title": "External Interactions",
        "fragment": "external-interactions.html",
        "roles": ("admin",),
        "icon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16.247 7.761a6 6 0 0 1 0 8.478"/><path d="M19.075 4.933a10 10 0 0 1 0 14.134"/><path d="M4.925 19.067a10 10 0 0 1 0-14.134"/><path d="M7.753 16.239a6 6 0 0 1 0-8.478"/><circle cx="12" cy="12" r="2"/></svg>',
    },
    {
        "id": "users",
        "title": "Users",
        "fragment": "users.html",
        "roles": ("admin",),
        "icon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
    },
    {
        "id": "account",
        "title": "Account",
        "fragment": "account.html",
        "roles": ("all",),
        "icon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 20a6 6 0 0 0-6-6 6 6 0 0 0-6 6"/><circle cx="12" cy="10" r="4"/><circle cx="12" cy="12" r="10"/></svg>',
    },
    {
        "id": "customisation",
        "title": "Customisation",
        "fragment": "customisation.html",
        "roles": ("all",),
        "icon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/><circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z"/></svg>',
    },
]

# ---------------------------------------------------------------------------
# API-key in-memory store (plaintext, decrypted from .env)
# ---------------------------------------------------------------------------
_api_key_store: list[dict] = []

# ---------------------------------------------------------------------------
# Flask app placeholder — set by app_factory / main
# ---------------------------------------------------------------------------
# Kept here so other modules can import it without circular imports via main.
# main.py will assign `state.app = Flask(__name__)`.
app = None  # type: ignore
