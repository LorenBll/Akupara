# Akupara

Akupara is a local-first web service with a browser UI, user, session and API-key authentication, a signed and hash-verified plugin library, internal and external plugin interactions, audio feedback, and update and integrity checks.

User documentation: [README.md](README.md). Repository rules for contributors and agents: [AGENTS.md](AGENTS.md).

## Table of Contents

- [Features](#features)
- [Project Structure](#project-structure)
- [API Endpoints](#api-endpoints)
- [Setup](#setup)
- [Run](#run)
- [Logging](#logging)
- [Testing](#testing)
- [Configuration](#configuration)
- [Support](#support)
- [Licence](#licence)
- [Author](#author)

## Features

- **Local-first service with a browser UI** — the service binds to `127.0.0.1` on the port in `resources/configuration.json` and serves a single-page UI whose navigation switches between the home, settings and plugins pages. It exists so the service is reachable only from the device that runs it. Start it (see [Run](#run)) and open `http://127.0.0.1:<port>/` in a browser; every page except the login page and the static assets requires a session, and a protected `/api/*` endpoint without a session returns `401` so the frontend redirects to `/login`.

- **Accounts, sessions and roles** — login credentials live in the `USERS` `.env` variable as peppered verifiers: the browser stretches the password with Argon2id using a per-account salt (`m=65536, t=2, p=1`) and the server stores only `akupara$2$<salt>$<tag>`, an HMAC-SHA256 tag keyed by `resources/password-pepper`. The pepper belongs to one installation (one setup run), so re-running setup rotates it and every stored verifier must then be regenerated. A leaked `.env` cannot be attacked offline and its contents cannot be replayed at `/api/login`; an entry that is not in this format is refused, so every account must be generated with `scripts/generate-password-hash.js`. On a start that finds `USERS` empty, a root account `admin` with the password `admin123` is created so a fresh installation can be entered; change that password immediately, because it is public. A successful login issues a sliding `akupara-refresh` cookie (renewed on every authenticated response; 15 minutes of validity) whose session is held server-side per user, so several users can be logged in at the same time. It exists to keep the service restricted to known users. Log in at `/login`, and manage the accounts from the Users card: register a user, rename it, switch it between Standard and Admin, or delete it; a user marked `"root": true` in `.env` cannot lose admin status or be deleted, and only the root account may rename itself. A standard user may view the settings page but not administer it.

- **API keys** — with `API_KEYS_ENABLED` on, the API Keys card generates keys and the service accepts them in the `X-Api-Key` header on the management and external-interaction endpoints (`api_key_or_admin_authenticated`). Keys exist so plugins and scripts can act without a browser session. The `.env` stores each key Fernet-encrypted under `API_KEYS`, with `API_KEY_ENCRYPTION_KEY` generated on first run; the plaintext key is shown once in the card so it can be copied.

- **Signed, hash-verified plugin library** — `resources/plugins-lib/` holds the catalogue of installable plugins as hash-range JSON files (`<low>-<high>.json`) plus a `reverse-index.json` word index, and `hash` is the library's signed content hash. It exists to certify which plugin versions Akupara may run. A plugin is `{"hash","name","description","repo","version","akupara_version","trust_mark"}`, and at startup the service verifies the stored hash's PGP signature and recomputes the folder hash, so a modified or uncertified library stops the service; search it with `POST /api/plugins/search` or the Plugins page.

- **Plugin discovery and upgrades** — installed plugins live in `plugins/<hash>` folders; the service verifies each folder's trust mark and content hash, compares the installed version with the plugin's GitHub releases, and offers or applies upgrades. It exists to keep installed plugins valid and current. Use the Plugins page (`Check for Updates`, `Update All`, per-row `Update` and `Delete`) or the `/api/installed-plugins*` endpoints; `dev-*` folders skip every check and load only in development mode.

- **Internal interactions (shared variables)** — a `SHARED_MEMORY` entry is `{"name","type","value","editor","reader"}`, where `editor` and `reader` are plugin-name lists; a plugin present in both is kept in `editor`. It exists so plugins can share values without a direct link. Register and edit variables from the Internal Interactions card (at least one plugin is required; updates are partial, so omitted fields are left untouched); each plugin's access is enforced per variable, and only an API key or an admin session may create or delete a variable.

- **Plugin event subscriptions** — `PLUGIN_EVENT_SUBSCRIPTIONS` maps an event name (`[A-Za-z0-9_.-]+`) to the sorted list of subscribed plugin names. It exists so plugins can address one another by event. Manage the subscriptions from the Internal Interactions card or through the `/api/plugin-events*` endpoints while internal interactions are enabled.

- **External interactions (IP firewall and network worker)** — with external interactions enabled, `ExternalInteractionsWorker` serves the marked endpoints on the device's first non-loopback IPv4 address on the same port, in parallel with the loopback server; the incoming IP list (`allow`, `unknown`, `block`) and `EXTERNAL_INTERACTIONS_ALLOW_NEW` decide which remote clients pass, and a parallel outgoing list and `EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING` exist for outgoing requests. It exists to expose the service to selected devices without opening it to everyone. Manage both lists from the External Interactions card; new incoming IPs are recorded automatically as `unknown` scoped to `akupara`. Opening it to other machines over plain HTTP is not advisable: the worker serves the same endpoints without TLS, so the login exchange and the `akupara-refresh` cookie travel unencrypted and a captured password hash can be replayed. Put the service behind a TLS reverse proxy or inside a VPN or SSH tunnel, or keep external interactions disabled. While external interactions are enabled and the page is not reached over TLS, the card border turns warn-yellow and hovering the card shows a "Plain HTTP Usage for External Interactions is Discouraged" tooltip just outside its right edge.

- **Audio feedback** — the service plays a configurable sound for six events (`startup`, `acknowledge`, `warn`, `process`, `success`, `error`); each event's file is set by its `.env` variable or from the Customisation card, and playback requires `PLAY_AUDIOS`. It exists to confirm actions audibly. Toggle playback, the startup sound and the log sounds from the Customisation card.

- **Update checks** — the service compares the signed project hash with the hash asset of the latest GitHub release, and the plugin-library hash with the hash at the latest commit, at startup and on demand. It exists to keep the deployment at a certified version. Check manually from the General and Plugins cards (`Check for Updates`) or through `/api/check-for-updates` and `/api/check-for-plugin-updates`; `AUTOMATIC_UPDATE`, `AUTOMATIC_PLUGIN_LIBRARY_UPDATE` and `AUTOMATIC_PLUGIN_UPGRADE` apply the result automatically. Set `GITHUB_TOKEN` to raise the GitHub API rate limit.

- **Integrity verification** — the repo-root `hash` and `resources/plugins-lib/hash` files each carry a hash plus an armoured PGP signature, and the service verifies the signature and recomputes the hash from the tracked files at every start. It exists to detect an illicit modification or a partial installation. The plugin library must always match its hash; the project may differ from its hash in development mode only, where it logs a warning instead of stopping. The General and Plugins cards show the effective and indicated version hashes side by side.

- **Server-rendered settings by role** — the settings page is rendered from card fragments under `ui/cards/settings/` and sent with the session's role, so standard users see the General, Account and Customisation cards while admin-only cards are loaded only for administrators. It exists to avoid exposing administrative content to standard users. Open `/ui/pages/settings.html` from the UI.

## Project Structure

- `src/` — the service source (Python).
  - `main.py` — the web layer: the Flask app, all `/api/*` handlers, the UI view functions, the settings-page context, the startup sequence and the `__main__` entry point.
  - `app_factory.py` — the shared Flask decorators (`standard_endpoint`, `_options_response`, `_head_response`, `set_connection_header`) and `_register_ui_routes`, which registers the UI routes when `guiEnabled` is `true`.
  - `auth.py` — the authentication decorators (`session_authenticated`, `api_key_or_admin_authenticated`, `admin_session_authenticated`, `log_change`), the server-side session store, user management, API-key management, the peppered password-verifier storage (installation-scoped pepper, Argon2id compatibility hashing), the bootstrap root account created when `USERS` is empty, and `_users_raw_list`, which refuses to rewrite a `USERS` value that is not a JSON list.
  - `state.py` — the mutable process-wide state: service flags, the session and API-key stores, caches and locks.
  - `config.py` — loads `resources/configuration.json` and initialises the service state; also resolves the device's local addresses.
  - `env_store.py` — reads and writes `.env` variables (including boolean parsing).
  - `audio.py` — the `AudioOrchestrator` worker, the sound-event map and playback.
  - `firewall.py` — the external-interactions entry store, the IP policy used by the network worker, and `_external_interactions_plain_http`, which flags an enabled external-interaction surface reached without TLS so the settings card can warn about it.
  - `network.py` — the `ExternalInteractionsWorker`, the `external_interactions_worker_callable` marker and the guarded WSGI app.
  - `plugin_bridge.py` — the plugin bridge: catalogue loading, reverse-index search, library hashing, trust verification, and installed-plugin discovery and upgrades.
  - `plugin_events.py` — plugin-event subscription storage.
  - `shared_memory.py` — internal-interaction shared variables and their role checks.
  - `integrity.py` — project- and plugin-library-hash computation and verification, and the disabled-styles check.
  - `updates.py` — the project and plugin-library update checks and their application.
  - `github_api.py` — GitHub request helpers and response classification.
  - `signing.py` — GPG signing, verification and reading of the `hash` files.
  - `validation.py` — shared input validation (key names, event names, plaintext values).
  - `logginglib/` — the JSON logging library (`__init__.py`).
- `ui/` — the browser UI: `pages/` (the HTML pages), `cards/settings/` (one fragment per settings card), `css/`, `js/` (including `js/argon2/`), `fonts/` and `icons/`.
- `resources/` — `configuration.json` (service configuration), `installation.json` (the marker that scopes the pepper to one installation; written when the installation is first prepared — by its first start or by the generator — and deleted by the setup scripts), `password-pepper` (the installation's pepper, created when the installation is first prepared and rotated when a new installation starts), `plugins-lib/` (the plugin catalogue, its signed `hash`, `reverse-index.json` and the public GPG key `lorenbll-akupara-pub`) and `audios/` (the shipped sound files).
- `scripts/` — `setup.bat`/`setup.sh` (create `.venv`, install dependencies and start a new installation by deleting the installation marker), `run.bat`/`run.sh` (launch the service, supporting `--debug`), `generate-password-hash.js` (prints a `USERS` entry — peppered verifier and client salt — for a chosen username and password, applying the same installation-scoped pepper policy as the service) and the developer-only `administration.py` helpers (not committed).
- `tests/` — `conftest.py`, `unit/`, `api/` and `scripts/`.
- `plugins/` — installed plugins, one `plugins/<hash>` folder per plugin; `dev-*` folders load only in development mode.
- `deployment/` — service templates: `service.service` (systemd), `com.service.plist` (launchd) and `startup-windows.vbs` (Windows startup folder or scheduled task).
- `hash` — the signed project hash. `AGENTS.md` — repository guidelines. `SECURITY.md` — the security policy. `.env.example` — the configuration template.
- `.github/workflows/` — the CI workflows: `tests.yml` and `disabled-styles.yml`.

## API Endpoints

The service exposes two groups of routes: the JSON endpoints under `/api/*`, registered in `src/main.py`, and the UI routes, registered by `src/app_factory.py::_register_ui_routes` and only when `guiEnabled` is `true` in `resources/configuration.json`. Every route is served from `http://127.0.0.1:<port>` (the main server binds to the loopback address). When external interactions are enabled, the network-callable routes are also served on the device's first non-loopback IPv4 address on the same port.

Shared behaviour:

- **`OPTIONS`** is handled by the `standard_endpoint` decorator and returns `200` with an empty JSON body (`{}`) and the headers `Allow` and `Access-Control-Allow-Methods` (both listing the endpoint's methods) and `Access-Control-Allow-Headers: Content-Type`.
- **`HEAD`**, where declared, is also handled by `standard_endpoint` and returns `200` with an empty JSON body.
- `OPTIONS` and `HEAD` requests still pass through the endpoint's authentication decorators, so an authenticated endpoint answers them only for an authenticated caller.
- The `after_request` hook `set_connection_header` sets `Connection: keep-alive` on `text/html` responses and `Connection: close` on every other response.
- **Authentication** is one of `session_authenticated` (any logged-in session), `admin_session_authenticated` (a logged-in administrator session; an API key is not accepted), `api_key_or_admin_authenticated` (a valid `X-Api-Key` header or a logged-in administrator session) or none.
- When the credential is missing or invalid, the response depends on the path: a `/api/*` path returns `401 {"error": "API key required."}`, while any other path is redirected to `/login` with `302`. The `api_key_or_admin_authenticated` decorator also returns `401` for a logged-in non-admin session, because it accepts only an API key or an admin session.
- With `admin_session_authenticated` (and the `POST /api/settings` admin branch), a valid session that is not an administrator returns `403 {"error": "Admin privileges required."}`.
- **Network callability.** Only endpoints marked `@network.external_interactions_worker_callable` are served by the `ExternalInteractionsWorker` (see `EXTERNAL_INTERACTIONS` in [Configuration](#configuration)); any other request reaching that worker is answered with a `404` byte-identical to an unknown path. `/api/update-now` and `/api/update-plugins-now` are not network callable and additionally require the client to be a local device, returning `403 {"error": "Local device access only."}` otherwise. `/api/settings` is not network callable.
- **Change log.** Endpoints decorated with `@log_change` append a `Change recorded` `INFO` event after a successful (2xx) `POST`, `PATCH` or `DELETE`, resolving the actor from the session or the `X-Api-Key` header to a stable id.
- JSON bodies are read with `request.get_json(silent=True) or {}`, so a malformed or absent body is treated as an empty object.

### `GET /api/health`

- **Methods**: `GET`, `HEAD`, `OPTIONS`. **Auth**: none. **Network-callable**: yes.
- **Purpose**: liveness probe reporting the bind address, port, hostname and process id.
- **Request**: none.
- **Response**: `200`
  ```json
  {"status": "ok", "service": "Akupara", "bind_address": "127.0.0.1", "port": 49150, "hostname": "host", "pid": 1234}
  ```
  For a non-local client arriving through the external-interactions worker, `bind_address` is the worker's bind address.
- **Errors**: none beyond the shared behaviour.

### `POST /api/terminate`

- **Methods**: `POST`, `OPTIONS`. **Auth**: `api_key_or_admin_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: stop the process (it sends `SIGTERM` to itself after 0.5 s).
- **Request**: none.
- **Response**: `200 {"status": "ok", "message": "Akupara is terminating."}`
- **Errors**: `401` missing or invalid credential (including a non-admin session).

### `POST /api/restart`

- **Methods**: `POST`, `OPTIONS`. **Auth**: `api_key_or_admin_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: restart the process (it spawns a new interpreter with the same arguments after 0.5 s and exits).
- **Request**: none.
- **Response**: `200 {"status": "ok", "message": "Akupara is restarting."}`
- **Errors**: `401` missing or invalid credential (including a non-admin session).

### `GET /api/settings` and `POST /api/settings`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `session_authenticated`; the `POST` branch additionally requires an admin session through `_require_admin_session()`. **Network-callable**: no. **`@log_change`**: yes.
- **Purpose**: read and change the three General-card feature switches.
- **Request (`POST`)**: JSON object that may contain any of `internalInteractions`, `displayPromotion` and `externalInteractions`; each provided value must be a boolean. Unknown keys are ignored.
- **Response**: `200`
  ```json
  {"internalInteractions": false, "displayPromotion": true, "externalInteractions": false}
  ```
  The `GET` branch returns the values read from `.env` (falling back to the in-memory state); the `POST` branch returns the values after applying the changes.
- **Errors**:
  - `400 {"error": "No known setting provided."}` when the body contains none of the three keys.
  - `400 {"error": "<key> must be a boolean."}` when a provided value is not a boolean.
  - `403 {"error": "Admin privileges required."}` when a logged-in non-admin sends `POST`.
  - `401` unauthenticated.

### `POST /api/password-salt`

- **Methods**: `POST`, `OPTIONS`. **Auth**: none. **Network-callable**: yes.
- **Purpose**: return the Argon2id salts the browser needs to hash a password, so each account can use its own salt without storing anything client-side. It is unauthenticated because the login page needs it before a session exists.
- **Request**: `{"username": "alice"}`.
- **Response**: `200 {"salt": "<22-character base64url>", "newSalt": "<22-character base64url>"}`. `salt` hashes the current password and is stable for a given username; an unknown username receives a stable pepper-derived decoy, so the response does not reveal whether an account exists. `newSalt` is fresh on every call and must be sent as `new_client_salt` with a password change or as `client_salt` with a registration.
- **Errors**:
  - `400 {"error": "Invalid request."}` when `username` is missing, empty or not a string.
  - `500 {"error": "Password storage is unavailable."}` when the pepper is unusable, or when the account has no valid `client_salt`.

### `POST /api/login`

- **Methods**: `POST`, `OPTIONS`. **Auth**: none. **Network-callable**: yes.
- **Purpose**: authenticate a user and issue the `akupara-refresh` session cookie.
- **Request**: `{"username": "alice", "password_hash": "$argon2id$..."}`. The hash must be computed by the browser with the `salt` returned by `POST /api/password-salt`.
- **Response**: `200 {"status": "ok"}` and a `Set-Cookie: akupara-refresh=<token>; HttpOnly; SameSite=Lax; Max-Age=900; Path=/`. A successful login resets the consecutive failed-login counter.
- **Errors**:
  - `400 {"error": "Invalid request."}` when `username` or `password_hash` is not a string.
  - `403 {"error": "Login is not configured."}` when `USERS` yields no usable user (`_load_users()` is empty: the value is empty or not a valid list of entries). A real start seeds the bootstrap account first, so this is only reached when seeding is skipped or the value is malformed.
  - `401 {"error": "Invalid credentials."}` on a wrong username or password.
  - `500 {"error": "Password storage is unavailable."}` when the pepper is unusable, or when the account's stored verifier is not in the supported `akupara$2$…` format.

### `POST /api/logout`

- **Methods**: `POST`, `OPTIONS`. **Auth**: `session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: drop the caller's server-side session and clear the cookie.
- **Request**: none.
- **Response**: `200 {"status": "ok"}` and a cookie deletion.
- **Errors**: `401` unauthenticated.

### `GET /api/session`

- **Methods**: `GET`, `HEAD`, `OPTIONS`. **Auth**: `session_authenticated`. **Network-callable**: yes.
- **Purpose**: return the current session's identity for the frontend's periodic sync.
- **Request**: none.
- **Response**: `200 {"username": "alice", "admin": true, "root": false}`
- **Errors**: `401` unauthenticated.

### `POST /api/change-password`

- **Methods**: `POST`, `OPTIONS`. **Auth**: `session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: change the logged-in account's password, updating every configured record for that username and giving the account a fresh salt.
- **Request**: `{"current_password_hash": "$argon2id$...", "new_password_hash": "$argon2id$...", "new_client_salt": "<newSalt>"}`. The current hash must use the account's `salt` and the new hash the `newSalt` returned by `POST /api/password-salt`; `new_client_salt` is that `newSalt`.
- **Response**: `200 {"status": "ok"}`. On success every other live session of the account is revoked, only the caller's session is kept.
- **Errors**:
  - `400 {"error": "Invalid request."}` when any field is missing, not a non-empty string, or not a well-formed client salt.
  - `400 {"error": "Invalid new password hash."}` when `new_password_hash` does not start with `$argon2id$`.
  - `404 {"error": "Account not found."}` when the account no longer exists.
  - `403 {"error": "Current password is incorrect."}` when the current hash does not match.
  - `401` unauthenticated.
  - `500 {"error": "Password storage is unavailable."}` when the pepper is unusable, or when the account's stored verifier is not in the supported `akupara$2$…` format.

### `GET /api/users` and `POST /api/users`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: list the configured users and register a new one.
- **Request (`POST`)**: `{"username": "bob12345", "password_hash": "$argon2id$...", "client_salt": "<newSalt>", "admin": false}`. `password_hash` must be computed with `client_salt`, the `newSalt` returned by `POST /api/password-salt`, which is stored with the account. The username must be at least 8 characters long and must not contain a space, `,`, `;`, `:`, `\`, `/`, `%`, `"` or `'`. The `admin` flag defaults to `false`.
- **Response**: `200` (`GET`) `{"users": [{"username": "root", "admin": true, "root": true}]}`, sorted root-first, then admins, then by username; `201` (`POST`) `{"status": "ok"}`.
- **Errors**:
  - `400 {"error": "Invalid request."}` for a missing/too-short/prohibited username, a missing or malformed `client_salt`, or a non-boolean `admin`.
  - `400 {"error": "Invalid password hash."}` when `password_hash` does not start with `$argon2id$`.
  - `409 {"error": "Already exists."}` when the username is taken.
  - `401` unauthenticated; `403` admin required.
  - `500 {"error": "Password storage is unavailable."}` when the pepper is unusable, or when `USERS` is not a JSON list: the value is left untouched rather than replaced by a fresh list, so the registration must be retried after the value is repaired.

### `PATCH /api/users/<username>` and `DELETE /api/users/<username>`

- **Methods**: `PATCH`, `DELETE`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: rename a user or toggle its admin status (`PATCH`), or delete it (`DELETE`).
- **Request (`PATCH`)**: `{"new_username": "carol678"}` to rename, or `{"admin": true}` to toggle. Providing neither returns `400`. Rename applies the same username rules as registration.
- **Response**: `200` `{"username": "carol678", "admin": false, "root": false}` for a rename or admin toggle; `200 {"status": "ok"}` for a deletion.
- **Errors**:
  - `400 {"error": "Invalid request."}` for an invalid new username, a non-boolean `admin`, or a `PATCH` with neither field.
  - `409 {"error": "Already exists."}` when the new username is taken.
  - `404 {"error": "Not found."}` when the target does not exist.
  - `403 {"error": "You cannot delete your own account."}` for a self-deletion.
  - `403 {"error": "You cannot change your own admin status."}` for a self admin toggle.
  - `403 {"error": "Only the root user can change its username."}` when a non-root session renames the root user.
  - `403 {"error": "Invalid request."}` when `DELETE` targets the root user or when `PATCH` demotes the root user.
  - `401` unauthenticated; `403` admin required.

### `GET /api/api-keys` and `POST /api/api-keys`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `api_key_or_admin_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: list the API keys (names only) and generate a new key.
- **Request (`POST`)**: `{"name": "my-key"}`.
- **Response**: `200` (`GET`) `{"apiKeys": [{"name": "my-key", "key": "<plaintext>"}]}`; `201` (`POST`) `{"name": "my-key", "key": "<plaintext>"}`. The plaintext key is returned only at creation.
- **Errors**:
  - `403 {"error": "Functionality disabled."}` when `API_KEYS_ENABLED` is off.
  - `400 {"error": "Invalid request."}` for an invalid name.
  - `409 {"error": "Already exists."}` when the name is taken.
  - `401` unauthenticated.

### `PATCH /api/api-keys/<key>` and `DELETE /api/api-keys/<key>`

- **Methods**: `PATCH`, `DELETE`, `OPTIONS`. **Auth**: `api_key_or_admin_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: rename a key (`PATCH`) or delete it (`DELETE`). The path segment is the key's plaintext value.
- **Request (`PATCH`)**: `{"name": "new-name"}`.
- **Response**: `200` (`PATCH`) `{"name": "new-name", "key": "<plaintext>"}`; `200` (`DELETE`) `{"status": "ok"}`.
- **Errors**:
  - `403 {"error": "Functionality disabled."}` when `API_KEYS_ENABLED` is off.
  - `400 {"error": "Invalid request."}` for an invalid name.
  - `409 {"error": "Already exists."}` when the name is taken.
  - `404 {"error": "Not found."}` when the key does not exist.
  - `401` unauthenticated.

### `GET /api/audio` and `POST /api/audio`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: read and change the audio settings (playback master switch and each event's sound file).
- **Request (`POST`)**: `{"playAudios": true}` and/or `{"event": "success", "sound": "success.wav"}`. At least one of `playAudios` or `event` must be present. `sound` may be empty to clear the assignment.
- **Response**: `200`
  ```json
  {"playAudios": true, "sounds": {"startup": "logo-reveal.wav", "acknowledge": "", "warn": "warn.wav", "process": "process.wav", "success": "success.wav", "error": "error.wav"}, "available": ["acknowledge.wav"]}
  ```
- **Errors**:
  - `400 {"error": "Invalid request."}` when neither key is present, when a value has the wrong type, or when the event or file is invalid.
  - `403 {"error": "Functionality disabled."}` when `PLAY_AUDIOS` is off and a sound file is being set.
  - `401` unauthenticated; `403` admin required.

### `POST /api/audio/play`

- **Methods**: `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: play one sound event immediately.
- **Request**: `{"event": "success"}`.
- **Response**: `200 {"status": "ok"}`
- **Errors**:
  - `403 {"error": "Functionality disabled."}` when `PLAY_AUDIOS` is off.
  - `400 {"error": "Invalid request."}` for a non-string or unknown event.
  - `401` unauthenticated; `403` admin required.

### `GET /api/log-sounds-enabled` and `POST /api/log-sounds-enabled`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: read and toggle whether a sound plays for each logged event.
- **Request (`POST`)**: `{"playLogSounds": true}`.
- **Response**: `200 {"playLogSounds": false}`
- **Errors**:
  - `400 {"error": "Invalid request."}` when the field is missing or not a boolean.
  - `403 {"error": "Functionality disabled."}` when `PLAY_AUDIOS` is off.
  - `401` unauthenticated; `403` admin required.

### `GET /api/startup-sound-enabled` and `POST /api/startup-sound-enabled`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: read and toggle the startup sound.
- **Request (`POST`)**: `{"playStartupSound": true}`.
- **Response**: `200 {"playStartupSound": true}`
- **Errors**:
  - `400 {"error": "Invalid request."}` when the field is missing or not a boolean.
  - `403 {"error": "Functionality disabled."}` when `PLAY_AUDIOS` is off.
  - `401` unauthenticated; `403` admin required.

### `GET /api/shared-memory` and `POST /api/shared-memory`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `api_key_or_admin_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: list and create internal-interaction shared variables.
- **Request (`POST`)**: `{"name": "greeting", "type": "string", "value": "hi", "editor": ["plugin-a"], "reader": ["plugin-b"]}`. At least one plugin must be given across `editor` and `reader`; a plugin present in both is kept in `editor`.
- **Response**: `200` (`GET`) `{"sharedMemory": [{"name": "greeting", "type": "string", "value": "hi", "editor": ["plugin-a"], "reader": []}]}`; `201` (`POST`) the created variable.
- **Errors**:
  - `400 {"error": "Invalid request."}` for a malformed plugin list, an unknown type, or an invalid value.
  - `400 {"error": "At least one plugin is required."}` when both plugin lists are empty.
  - `403 {"error": "Functionality disabled."}` when the shared-memory feature is off.
  - `403 {"error": "The internal interactions functionality is disabled."}` from the logic layer.
  - `403 {"error": "Access denied."}` on an access violation.
  - `409 {"error": "A shared variable with this name already exists."}` when the name is taken.
  - `401` unauthenticated.

### `DELETE /api/shared-memory/<name>` and `PATCH /api/shared-memory/<name>`

- **Methods**: `DELETE`, `PATCH`, `OPTIONS` (registered as two view functions on the same path). **Auth**: `api_key_or_admin_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: delete a shared variable (`DELETE`) or partially update it (`PATCH`).
- **Request (`PATCH`)**: any subset of `{"value": ..., "type": "list", "editor": [...], "reader": [...]}`. Omitted fields are left untouched; a type-only change re-normalises the existing value.
- **Response**: `200 {"status": "ok"}` for a deletion; `200` the updated variable for an update.
- **Errors**:
  - `403 {"error": "Functionality disabled."}` when the feature is off.
  - `403 {"error": "Access denied."}` on an access violation.
  - `400 {"error": "Invalid request."}` for an invalid value or plugin list.
  - `404 {"error": "Not found."}` when the variable does not exist.
  - `401` unauthenticated.

### `GET /api/shared-memory-enabled` and `POST /api/shared-memory-enabled`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: read and toggle shared variables within internal interactions.
- **Request (`POST`)**: `{"sharedMemoryEnabled": true}`.
- **Response**: `200 {"sharedMemoryEnabled": false}`
- **Errors**:
  - `400 {"error": "Invalid request."}` when the field is missing or not a boolean.
  - `403 {"error": "Functionality disabled."}` when `INTERNAL_INTERACTIONS` is off.
  - `401` unauthenticated; `403` admin required.

### `GET /api/api-keys-enabled` and `POST /api/api-keys-enabled`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: read and toggle the API-keys feature.
- **Request (`POST`)**: `{"apiKeysEnabled": true}`.
- **Response**: `200 {"apiKeysEnabled": false}`
- **Errors**: `400 {"error": "Invalid request."}` when the field is missing or not a boolean; `401` unauthenticated; `403` admin required.

### `GET /api/external-interactions-enabled` and `POST /api/external-interactions-enabled`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: read and toggle the external-interactions master feature (toggling it starts or stops the network worker).
- **Request (`POST`)**: `{"externalInteractions": true}`.
- **Response**: `200 {"externalInteractions": false}`
- **Errors**: `400 {"error": "Invalid request."}` when the field is missing or not a boolean; `401` unauthenticated; `403` admin required.

### `GET /api/automatic-update-enabled` and `POST /api/automatic-update-enabled`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: read and toggle automatic project updates. The toggle is disabled in development mode and when project functionalities are disabled after an integrity failure.
- **Request (`POST`)**: `{"automaticUpdate": true}`.
- **Response**: `200 {"automaticUpdate": false}`
- **Errors**:
  - `403 {"automaticUpdate": <value>, "disabled": true, "error": "Project functionalities disabled due to integrity check failure."}` when the project functionalities are disabled (any method).
  - `403 {"automaticUpdate": <value>, "disabled": true, "error": "Automatic updates are disabled in development mode."}` when development mode is on (`POST`).
  - `400 {"error": "Invalid request."}` when the field is missing or not a boolean.
  - `401` unauthenticated; `403` admin required.

### `GET /api/automatic-plugin-library-update-enabled` and `POST /api/automatic-plugin-library-update-enabled`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: read and toggle automatic plugin-library updates.
- **Request (`POST`)**: `{"automaticPluginLibraryUpdate": true}`.
- **Response**: `200 {"automaticPluginLibraryUpdate": false}`
- **Errors**: `400 {"error": "Invalid request."}` when the field is missing or not a boolean; `401` unauthenticated; `403` admin required.

### `GET /api/automatic-plugin-upgrade-enabled` and `POST /api/automatic-plugin-upgrade-enabled`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: read and toggle automatic upgrades of installed plugins.
- **Request (`POST`)**: `{"automaticPluginUpgrade": true}`.
- **Response**: `200 {"automaticPluginUpgrade": false}`
- **Errors**: `400 {"error": "Invalid request."}` when the field is missing or not a boolean; `401` unauthenticated; `403` admin required.

### `POST /api/check-for-updates`

- **Methods**: `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: compare the stored project hash with the hash asset of the latest GitHub release.
- **Request**: none.
- **Response**: `200 {"updateAvailable": false, "currentVersion": "0123456789ab", "integrityOk": true}`
- **Errors**: `401` unauthenticated; `403` admin required. In development mode the response is always `updateAvailable: false` and no remote fetch happens; in non-development mode a failed integrity check exits the process (`exit(1)`).

### `POST /api/update-now`

- **Methods**: `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: no. **`@log_change`**: yes. **Local-device-only**: yes.
- **Purpose**: apply the available project update and restart.
- **Request**: none.
- **Response**: `200 {"status": "ok", "message": "Updating and restarting."}`
- **Errors**:
  - `403 {"error": "Local device access only."}` when the client is not a local device.
  - `403 {"error": "Updates are disabled in development mode."}` in development mode.
  - `500 {"error": "Project integrity check failed."}` when the effective project hash differs from the indicated one (non-development).
  - `400 {"error": "No update available."}` when no update is available.
  - `401` unauthenticated; `403` admin required.

### `POST /api/check-for-plugin-updates`

- **Methods**: `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: compare the stored plugin-library hash with the hash at the latest commit.
- **Request**: none.
- **Response**: `200 {"updateAvailable": false, "currentVersion": "0123456789ab", "integrityOk": true}`; `integrityOk` is `false` and `updateAvailable` is `false` when the plugin library failed its integrity check.
- **Errors**: `401` unauthenticated; `403` admin required.

### `POST /api/update-plugins-now`

- **Methods**: `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: no. **`@log_change`**: yes. **Local-device-only**: yes.
- **Purpose**: apply the available plugin-library update and restart.
- **Request**: none.
- **Response**: `200 {"status": "ok", "message": "Updating and restarting."}`
- **Errors**:
  - `403 {"error": "Local device access only."}` when the client is not a local device.
  - `400 {"error": "Plugin library integrity check failed."}` when the effective library hash differs from the indicated one.
  - `400 {"error": "No update available."}` when no update is available.
  - `401` unauthenticated; `403` admin required.

### `POST /api/check-for-plugin-upgrades`

- **Methods**: `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: check the installed plugins for available upgrades without applying any.
- **Request**: none.
- **Response**: `200`
  ```json
  {"upgradesAvailable": false, "plugins": [{"hash": "ab12…", "name": "sample", "installedVersion": "1.0.0", "latestTag": "1.1.0"}]}
  ```
- **Errors**: `500 {"error": "Upgrade check failed."}` when the discovery raises; `401` unauthenticated; `403` admin required.

### `POST /api/upgrade-all-plugins`

- **Methods**: `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: upgrade every installed plugin that has a pending upgrade.
- **Request**: none.
- **Response**: `200`
  ```json
  {"status": "ok", "stillPending": false, "plugins": [{"hash": "ab12…", "name": "sample", "installedVersion": "1.0.0", "latestTag": "1.1.0"}]}
  ```
- **Errors**: `500 {"error": "Upgrade failed."}` when the discovery or upgrade raises; `401` unauthenticated; `403` admin required.

### `GET /api/installed-plugins`

- **Methods**: `GET`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: list the installed plugin folders (disk-only, no network).
- **Request**: none.
- **Response**: `200`
  ```json
  {"plugins": [{"folder": "ab12…", "name": "sample", "dev": false, "effective": "…", "indicated": "…"}]}
  ```
  `dev-*` folders have `dev: true` and empty `effective`/`indicated`.
- **Errors**: `401` unauthenticated; `403` admin required.

### `POST /api/installed-plugins/<folder>` and `DELETE /api/installed-plugins/<folder>`

- **Methods**: `POST`, `DELETE`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: upgrade a single installed plugin (`POST`) or stop and delete its folder (`DELETE`).
- **Request**: none.
- **Response**: `200` (`POST`) `{"status": "ok", "upgraded": true, "upToDate": false, "stillPending": false}` (an already current plugin returns `upgraded: false, upToDate: true`); `200` (`DELETE`) `{"status": "ok"}`.
- **Errors**:
  - `400 {"error": "Dev plugins cannot be upgraded."}` when `POST` targets a `dev-*` folder.
  - `404 {"error": "Not found."}` when the folder does not exist or is invalid.
  - `500 {"error": "Upgrade check failed."}` or `500 {"error": "Upgrade failed."}` when discovery or upgrade raises.
  - `500 {"error": "Deletion failed."}` when `DELETE` raises an `OSError`.
  - `401` unauthenticated; `403` admin required.

### `GET /api/plugins/search` and `POST /api/plugins/search`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes.
- **Purpose**: search the plugin catalogue by regular expression, using the reverse index first and falling back to a full scan.
- **Request (`GET`)**: query parameter `q`, `query` or `pattern` (the first present wins). **Request (`POST`)**: `{"query": "..."}`, also accepting `pattern` or `q`.
- **Response**: `200 {"plugins": [{"hash": "ab12…", "name": "sample", "description": "…", "repo": "…", "version": "1.0.0", "akupara_version": "…", "trust_mark": ""}]}`
- **Errors**: `400 {"error": "Invalid request."}` for a non-string or invalid pattern; `401` unauthenticated; `403` admin required.

### `GET /api/plugin-events` and `POST /api/plugin-events`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: list plugin-event subscriptions and create an event.
- **Request (`POST`)**: `{"event": "on-ready", "plugin": "sample"}`.
- **Response**: `200` (`GET`) `{"pluginEvents": {"on-ready": ["sample"]}}`; `201` (`POST`) `{"event": "on-ready", "plugins": ["sample"]}`.
- **Errors**:
  - `403 {"error": "Internal interactions disabled."}` when `INTERNAL_INTERACTIONS` is off.
  - `409 {"error": "Already exists."}` when the event or subscription already exists.
  - `400 {"error": "Invalid request."}` for an invalid event name or plugin.
  - `401` unauthenticated; `403` admin required.

### `DELETE /api/plugin-events/<event>`

- **Methods**: `DELETE`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: delete a plugin event and its subscriptions.
- **Request**: none.
- **Response**: `200 {"status": "ok"}`
- **Errors**: `403 {"error": "Internal interactions disabled."}` when the feature is off; `400 {"error": "Invalid request."}`; `404 {"error": "Not found."}`; `401` unauthenticated; `403` admin required.

### `POST /api/plugin-events/<event>/plugins`

- **Methods**: `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: subscribe a plugin to an event.
- **Request**: `{"plugin": "sample"}`.
- **Response**: `201 {"event": "on-ready", "plugin": "sample"}`
- **Errors**: `403 {"error": "Internal interactions disabled."}` when the feature is off; `409 {"error": "Already exists."}`; `400 {"error": "Invalid request."}`; `401` unauthenticated; `403` admin required.

### `DELETE /api/plugin-events/<event>/plugins/<plugin>`

- **Methods**: `DELETE`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: unsubscribe a plugin from an event.
- **Request**: none.
- **Response**: `200 {"status": "ok"}`
- **Errors**: `403 {"error": "Internal interactions disabled."}` when the feature is off; `400 {"error": "Invalid request."}`; `404 {"error": "Not found."}`; `401` unauthenticated; `403` admin required.

### `GET /api/external-interactions-incoming-ips`

- **Methods**: `GET`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: list the incoming firewall entries.
- **Request**: none.
- **Response**: `200 {"incomingIps": [{"ip": "192.0.2.10", "plugins": ["akupara"], "action": "unknown", "Note": ""}]}`
- **Errors**: `403 {"error": "Functionality disabled."}` when external interactions are off; `401` unauthenticated; `403` admin required.

### `GET /api/external-interactions-outgoing-ips`

- **Methods**: `GET`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: list the outgoing firewall entries.
- **Request**: none.
- **Response**: `200 {"outgoingIps": [{"ip": "192.0.2.10", "plugins": ["akupara"], "action": "unknown", "Note": ""}]}`
- **Errors**: `403 {"error": "Functionality disabled."}` when external interactions are off; `401` unauthenticated; `403` admin required.

### `PATCH /api/external-interactions-incoming-ips/<ip>` and `DELETE /api/external-interactions-incoming-ips/<ip>`

- **Methods**: `PATCH`, `DELETE`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: update an incoming firewall entry or delete it.
- **Request (`PATCH`)**: JSON object containing any of `action` (`allow`, `unknown` or `block`), `note`, `new_ip` (rename), `remove_plugin` and `add_plugin`; at least one field must be present, and all present fields must be strings.
- **Response**: `200` the updated entry, or `200 {"status": "ok"}` for a deletion.
- **Errors**:
  - `403 {"error": "Functionality disabled."}` when the feature is off.
  - `400 {"error": "Invalid request."}` when no field is given, a field has the wrong type, or a value is invalid.
  - `409 {"error": "Already exists."}` when `new_ip` duplicates another entry.
  - `404 {"error": "Not found."}` when the entry does not exist.
  - `401` unauthenticated; `403` admin required.

### `PATCH /api/external-interactions-outgoing-ips/<ip>` and `DELETE /api/external-interactions-outgoing-ips/<ip>`

- **Methods**: `PATCH`, `DELETE`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: update an outgoing firewall entry or delete it. Behaviour, request shape and errors are identical to the incoming item endpoint above, on the outgoing list.

### `GET /api/external-interactions-allow-new` and `POST /api/external-interactions-allow-new`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: read and toggle the default policy for new incoming clients.
- **Request (`POST`)**: `{"externalInteractionsAllowNew": true}`.
- **Response**: `200 {"externalInteractionsAllowNew": false}`
- **Errors**: `400 {"error": "Invalid request."}` when the field is missing or not a boolean; `403 {"error": "Functionality disabled."}` when the feature is off; `401` unauthenticated; `403` admin required.

### `GET /api/external-interactions-allow-new-outgoing` and `POST /api/external-interactions-allow-new-outgoing`

- **Methods**: `GET`, `POST`, `HEAD`, `OPTIONS`. **Auth**: `admin_session_authenticated`. **Network-callable**: yes. **`@log_change`**: yes.
- **Purpose**: read and toggle the default policy for outgoing requests with no matching entry.
- **Request (`POST`)**: `{"externalInteractionsAllowNewOutgoing": true}`.
- **Response**: `200 {"externalInteractionsAllowNewOutgoing": false}`
- **Errors**: `400 {"error": "Invalid request."}` when the field is missing or not a boolean; `403 {"error": "Functionality disabled."}` when the feature is off; `401` unauthenticated; `403` admin required.

### UI routes

The UI routes are registered by `_register_ui_routes` only when `guiEnabled` is `true`; otherwise they are absent and the server returns `404` for them. Each route declares `GET`, `HEAD` and `OPTIONS`, handled by `standard_endpoint` as described above, and each is network-callable.

- **`GET /login`** (`login_page`) — unauthenticated. Serves the login page; a request carrying a valid session cookie is redirected to `/`. Response: `200` HTML, or `302` to `/`.
- **`GET /`** (`index`) — `session_authenticated`. Serves the single-page UI shell, rendered with `display_promotion`, `is_admin` and `development`. Unauthenticated requests are redirected to `/login`; otherwise `200` HTML.
- **`GET /ui/pages/settings.html`** (`ui_settings_page`) — `session_authenticated`. Serves the settings shell rendered with the session's role, the card indicators and the static settings context. Unauthenticated requests are redirected to `/login`; otherwise `200` HTML.
- **`GET /ui/cards/settings/<card_id>`** (`ui_settings_card`) — `session_authenticated`. Serves one settings card fragment. An unauthenticated request is redirected to `/login`; unknown ids return `404 {"error": "Unknown card."}`; a known admin-only card requested by a non-admin session returns `403 {"error": "Admin privileges required."}`; otherwise `200` HTML.
- **`GET /ui/pages/plugins.html`** (`ui_plugins_page`) — `admin_session_authenticated`. Serves the plugins page; when the plugin library failed its integrity check it returns a `200` HTML notice with the page disabled. An unauthenticated request is redirected to `/login`; a non-admin session returns `403 {"error": "Admin privileges required."}`.
- **`GET /ui/pages/<path:filename>`** (`ui_page`) — `session_authenticated`. Serves any page file from `ui/pages/`; unknown files return `404`. Unauthenticated requests are redirected to `/login`.
- **`GET /ui/css/<path:filename>`** (`ui_css`) — unauthenticated. Serves a file from `ui/css/`; unknown files return `404`.
- **`GET /ui/fonts/<path:filename>`** (`ui_font`) — unauthenticated. Serves a file from `ui/fonts/`; unknown files return `404`.
- **`GET /ui/icons/<path:filename>`** (`ui_icon`) — unauthenticated. Serves a file from `ui/icons/`; unknown files return `404`.
- **`GET /ui/icons/akupara.svg`** (`ui_login_icon`) — unauthenticated. Serves the service icon used by the login page; `404` when absent.
- **`GET /ui/js/<path:filename>`** (`ui_js`) — unauthenticated. Serves a file from `ui/js/`; unknown files return `404`.
- **`GET /ui/js/argon2/argon2-bundled.min.js`** (`ui_argon2_script`) — unauthenticated. Serves the bundled Argon2 script used to hash passwords in the browser; `404` when absent.

## Setup

1. Install Python 3.10 or newer and make it available on `PATH`.
2. Run `scripts\setup.bat` on Windows, or `bash scripts/setup.sh` on Unix-like systems. The script creates a `.venv` virtual environment and installs `requirements.txt` into it. To do it manually, create a virtual environment and run `pip install -r requirements.txt`.
3. Install GnuPG (`gpg`) and make it available on `PATH` (`C:\Program Files\GnuPG\bin` on Windows). It is required: the shipped `hash` files and plugin trust marks are verified with `gpg --verify` against `resources/plugins-lib/lorenbll-akupara-pub`, and the service refuses to start when the signature cannot be verified.
4. Copy `.env.example` to `.env`. You can leave `USERS=[]`: the first start creates a root account `admin` with the password `admin123`, which you should change immediately from the General card. To add another account, or to replace the default one, run `node scripts/generate-password-hash.js <username> <password>` (Node.js 18 or newer) and paste the printed entry into `USERS`, a JSON list of `{"username","password","client_salt","admin","root","id"}` where `password` is the stored verifier and `client_salt` the browser salt for that account. The generator scopes the pepper to the current installation: it creates `resources/password-pepper` on a new installation, rotates a pepper left by a previous one, and records the installation so the first start keeps that pepper. Back the pepper up and keep it out of version control, because losing it makes every stored password unusable. An entry holding a raw Argon2id hash, or missing `client_salt`, is refused at login.
5. Review `resources/configuration.json` to set the port, whether the UI routes are enabled (`guiEnabled`) and whether the service runs in development mode (`development`).
6. Keep the project structure intact so the service can find `resources/`, `src/` and `ui/`, and leave the signed `hash` files unmodified.

## Run

Start the service:

- Windows: `scripts\run.bat`.
- Unix-like: `bash scripts/run.sh`.

Both scripts check for Python 3.10+, create or reuse `.venv`, install `requirements.txt`, and start `src/main.py`. Without `--debug` the service starts in the background with no console output; with `--debug` it runs in the foreground and writes a short trace of its progress.

A start with no `resources/installation.json` is a new installation: it creates or rotates the pepper and records the marker. A start that finds the marker but no usable pepper logs the reason, plays the error sound and exits without serving. A start whose `USERS` list is empty creates the default root account `admin` with the password `admin123`; a value that is not a valid JSON list is left untouched, and logins are refused while no usable user exists. Registering a user is refused for the same reason instead of replacing that value.

To start it directly, run `python src/main.py` from the project root.

Add `--debug` to write `DEBUG` events to the log; without it, `DEBUG` events are not written. The log itself is described in [README-DEV.md](README-DEV.md#logging).

## Logging

The service logs through `src/logginglib/`, which implements the shared logging standard.

- **Location and naming.** Log files are written to `logs/` at the project root (or to the directory passed to `init_logging`). Each run opens a fresh file named `DD-MM-YYYY_HH.MM.SS.json`. The file holds a JSON list; every event is appended to it.
- **Event shape.** Each event is an object with five fields:
  - `timestamp` — the moment the event was logged (`ISO-8601`, second precision).
  - `type` — one of `ERROR`, `WARN`, `INFO`, `DEBUG`.
  - `title` — a brief description.
  - `data` — the payload, or `null`.
  - `hash` — a `SHA-256` hash computed over the canonical JSON of `[timestamp, title, data]`, so an entry can be referenced uniquely by the `(project name, hash)` pair.
- **`--debug`.** `run.bat` / `run.sh` pass `--debug` to `src/main.py`, which forwards it to `init_logging(..., debug=...)`. When `--debug` is given, `DEBUG` events are written; otherwise `_write_event` drops them before appending.
- **Retention.** At every `init_logging` call the library prunes files older than 14 days. The comparison is by calendar date only (the date parsed from the file name, or the file's modification time when the name cannot be parsed); files from the same date are never pruned, regardless of time.
- **Log sounds.** When both `PLAY_AUDIOS` and `PLAY_LOG_SOUNDS` are on, each written event plays a sound by type (`INFO`/`DEBUG` → acknowledge, `WARN` → warn, `ERROR` → error), except `Change recorded` events and events logged with `silent=True`.
- **Concurrency.** Writes are guarded by a lock, so events are appended without interleaving.
- **Tests.** `logginglib` is redirected to a temporary directory by an autouse fixture in `tests/conftest.py`, so the test suite never touches the real `logs/`. See [Testing](#testing).

## Testing

The suite lives in `tests/`, is driven by `pytest` (`pytest.ini` sets `testpaths = tests`, `pythonpath = src` and `addopts = -ra`) and contains 259 collected tests: 174 in `tests/unit`, 83 in `tests/api` and 2 in `tests/scripts`.

### Isolation guarantees

The codebase relies on module-level globals (`src/state.py`), a root `.env` (`src/env_store.py`), JSON log files (`src/logginglib`) and audio side effects (`src/audio.py`). Tests must never read or replace the developer's real data, so the autouse fixtures redirect each of those resources to a per-test temporary location:

- `env_store._env_path` is pointed at a per-test temporary `.env`.
- `dotenv.load_dotenv` is neutralised, so the repository's real `.env` is never read.
- `audio.play_sound` is silenced, so no audio subprocess is spawned.
- `logginglib.init_logging` is pointed at a per-test temporary log directory.
- `auth._password_pepper_path` and `auth._installation_marker_path` are pointed at a per-test temporary directory, and the pepper cache is reset, so the developer's real `resources/password-pepper` and `resources/installation.json` are never read or written. The fixture initialises a settled installation (pepper and marker), so tests that register users have a usable pepper; the pepper-policy tests remove that state to exercise a new or a pepper-less installation.
- Every mutable `state` global is reset before each test and a fixed list of `.env` keys is deleted from the environment.

`tests/scripts/test_setup_run.py` adds its own isolation: it copies the tracked files into a temporary directory and creates a fresh git checkout there, so the setup and run scripts operate only on the copy.

### Layout and module descriptions

`tests/conftest.py` holds the shared fixtures. The modules and their scope:

`tests/unit/` (174 collected):

- `test_app_factory.py` (5) — the shared Flask decorators and helpers (`standard_endpoint`, the `OPTIONS`/`HEAD` responses, `set_connection_header`).
- `test_audio.py` (8) — pure audio helpers and the sound defaults: every event names a file shipped in `resources/audios/`, with `warn.wav` and `process.wav` pinned.
- `test_auth_api_keys.py` (9) — API-key management in the auth layer.
- `test_auth_password_storage.py` (53) — peppered verifiers, client salts, rename behaviour, the installation-scoped pepper policy, the bootstrap root account, the browser-hash parameters and every rejection path of the stored-verifier format (raw hashes, missing parts, undecodable base64, wrong salt length, missing pepper, and a verifier that does not survive a pepper rotation).
- `test_auth_passwords.py` (3) — password change in the auth layer.
- `test_auth_sessions.py` (3) — session helpers.
- `test_auth_users.py` (19) — user management, including registration refusing to replace a `USERS` value that is not a JSON list.
- `test_config.py` (6 functions, 7 collected) — service configuration, including a missing pepper aborting startup, the default-root seeding switch and the sound defaults written into a `.env` that lacks them; one test is parametrised over `development` `True`/`False`.
- `test_env_store.py` (6) — `.env` persistence helpers.
- `test_firewall.py` (10) — external-interactions helpers, including the plain-HTTP flag.
- `test_github_api.py` (2) — pure GitHub response classification.
- `test_integrity.py` (2) — pure integrity helpers.
- `test_logginglib.py` (6) — the JSON logging library and the logging standard.
- `test_network.py` (3) — the external-interactions worker guard.
- `test_plugin_events.py` (8) — plugin-event subscription helpers.
- `test_shared_memory.py` (13) — shared-variable helpers.
- `test_signing.py` (6) — hash-file signing helpers.
- `test_updates.py` (5) — offline update-check classification.
- `test_validation.py` (6) — input-validation helpers.

`tests/api/` (83 collected):

- `test_api_keys.py` (6) — `/api/api-keys`.
- `test_authz.py` (9) — the authorisation matrix for the three cookie/API-key decorators.
- `test_change_password.py` (5) — `/api/change-password`.
- `test_external_interactions_api.py` (5) — the external-interactions (firewall) endpoints.
- `test_health.py` (3) — `GET /api/health`.
- `test_login_logout_session.py` (6) — login, logout and session.
- `test_password_salt.py` (4) — `/api/password-salt`.
- `test_plugin_events_api.py` (5) — `/api/plugin-events`.
- `test_settings_toggles.py` (10) — settings and feature toggles.
- `test_shared_memory_api.py` (7) — `/api/shared-memory`.
- `test_ui_routes.py` (13) — the UI routes, including whether the External Interactions card carries the plain-HTTP warning for the request's scheme (plain, disabled feature, `X-Forwarded-Proto: https` and a forwarded list).
- `test_users.py` (10) — `/api/users`, including the **500** returned when `USERS` is malformed.

`tests/scripts/` (2 collected):

- `test_setup_run.py` (2) — script smoke tests (see below).

### How to run

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest                     # everything (259 tests)
python -m pytest tests/unit          # unit tests only (174)
python -m pytest tests/api           # API tests only (83)
python -m pytest tests/scripts       # script smoke tests only (2)
python -m pytest tests/unit/test_auth_users.py         # one module
python -m pytest tests/api/test_users.py::<test_name>  # one test
```

`requirements-dev.txt` pins `pytest>=8.0`, `pytest-cov>=5.0` and `pytest-github-actions-annotate-failures>=0.3`.

### Development mode in the API tests

The `client` fixture monkeypatches `config._load_configuration` with `TEST_CONFIG = {"port": 49155, "guiEnabled": True, "development": False}` and calls `main._initialize_service_config(seed_default_root=False)` before returning `main.app.test_client()`; the seeding switch is off so each scenario starts from an empty `USERS` and controls its own accounts. The API suite therefore exercises the non-development configuration: `state.DEVELOPMENT` is `False`, the UI routes are registered, and the development-only shortcuts in the endpoints are not taken.

### Fixtures

Autouse fixtures (applied to every test):

- `_reset` — restores every mutable `state` global to its declared default and deletes the fixed `.env` keys from the environment.
- `_isolate_env` — redirects `env_store._env_path` to a per-test temporary `.env` and neutralises `dotenv.load_dotenv`; returns the temporary path.
- `_silence_audio` — replaces `audio.play_sound` with a no-op.
- `_isolate_logging` — calls `logginglib.init_logging` with a per-test temporary log directory; returns that directory.
- `_isolate_password_pepper` — points `auth._password_pepper_path` and `auth._installation_marker_path` at a per-test temporary directory, clears the pepper cache before and after the test and initialises a settled installation (pepper plus marker); returns the pepper path. The pepper-policy tests remove that state to exercise a new or a pepper-less installation.

`conftest.py` also registers the UI routes once on the module-level Flask app (`main._register_ui_routes(main.app)`) when `state.GUI_ENABLED` is true, because the real application only does so under `__main__`.

Helper fixtures (return callables):

- `client` — a Flask test client initialised as described above.
- `make_user` — appends a `USERS` entry (`password` defaults to the sentinel hash, wrapped into a peppered verifier, with optional `admin`, `root`, `id` and `client_salt`) and returns the username.
- `login` — `POST`s `/api/login` with a username and password hash.
- `login_admin` — creates an admin user and logs in as it.
- `enable_api_keys` — sets `state.API_KEYS_ENABLED` and generates an `API_KEY_ENCRYPTION_KEY` when absent.
- `make_api_key` — enables API keys and creates one, returning its plaintext value.

The sentinel hash `"$argon2id$test-sentinel-hash"` is a valid-looking client hash: `make_user` stores only its peppered verifier, and the tests submit the sentinel itself, so no real Argon2 computation is needed.

### Script smoke tests

`tests/scripts/test_setup_run.py` runs the setup and run scripts in a disposable copy (see [Isolation guarantees](#isolation-guarantees)).

- `test_setup_script` runs `setup.bat`/`setup.sh` in the copy and asserts a zero exit status, the presence of `.venv`, and the deletion of a pre-existing `resources/installation.json` (a setup run starts a new installation).
- `test_run_script` starts `run.bat`/`run.sh` with `--debug`, streaming the server output to `run.log` (so the pipe buffer cannot stall the server), then polls `GET /api/health` on a free port for up to 180 s using `http.client` (never `urllib`, which could route the loopback request through a proxy). Once healthy it asserts that the sandbox `.env` holds exactly one root entry, `admin`, and that `POST /api/login` with `_client_password_hash("admin123", <client_salt>)` returns `200`, so the bootstrap account is proven usable on a genuinely fresh installation rather than only in unit tests. On failure it prints the tails of `run.log` and of the latest `logs/*.json` before re-raising. The test is skipped when `gpg` or `git` is unavailable, because the shipped `hash` files must verify and `git ls-files` drives the integrity check.

The sandbox is a fresh git checkout in non-development mode, so the project-integrity check must pass for the server to start.

### Signing and hash tests

`tests/unit/test_signing.py` covers `src/signing.py`. Most tests parse only; the tests that need a real keyring are marked `requires_gpg` and skipped when `gpg` is unavailable.

`_point_gpg_at_an_empty_keyring` redirects `GNUPGHOME` to an empty temporary keyring, in the path form the local `gpg` understands (native GnuPG needs the Windows form, while the Git-for-Windows MSYS build needs the POSIX form; the form is chosen from the `Home:` line of `gpg --version`). This isolation is essential: the signing key is present on the developer's machine, so without a clean keyring a broken or unreadable public key stays invisible and only ever fails on a clean machine. The test verifies both shipped `hash` files (the repo root and `resources/plugins-lib/`) against the shipped public key, and checks that a UTF-8 BOM in the public key does not break import.

### Continuous integration

`.github/workflows/tests.yml` runs on every push, pull request and manual dispatch, with `contents: read` permission, and has two jobs:

- **`unit-api`** — matrix of `ubuntu-latest` and `windows-latest` × Python `3.10`, `3.11` and `3.12`, `fail-fast: false`. Installs `requirements.txt` and `requirements-dev.txt`, then runs `python -m pytest tests/unit tests/api --tb=short -ra -v`.
- **`scripts`** — matrix of `ubuntu-latest` and `windows-latest`, Python `3.11`. Installs `gpg` (through `apt-get` on Linux; resolving it from `PATH` and falling back to Chocolatey or a fixed directory on Windows), installs the dependencies, then runs `python -m pytest tests/scripts --tb=short -ra -v`.

`.github/workflows/disabled-styles.yml` runs on changes to `ui/css/index.css` or `src/main.py`, on `ubuntu-latest`, and verifies that every interactive `:disabled` rule in `ui/css/index.css` contains `cursor: not-allowed`. The same check runs at startup through `src/integrity.py:_verify_disabled_styles`.

### Adding a test

- Choose the directory by scope: pure helper logic in `tests/unit/`, endpoint behaviour through the Flask `client` in `tests/api/`, and script/integration smoke tests in `tests/scripts/`.
- Use the existing fixtures (`client`, `make_user`, `login`, `login_admin`, `enable_api_keys`, `make_api_key`) instead of touching real files or environment variables; the autouse isolation fixtures handle `.env`, logs, audio and `state`.
- Monkeypatch external boundaries (GitHub requests, `subprocess`/`gpg`, version helpers) rather than performing real I/O. `tests/api/test_ui_routes.py` shows a version-helper mock; `tests/unit/test_signing.py` shows the gpg keyring isolation pattern; mark keyring-dependent tests with `requires_gpg`.
- Keep the module docstring and naming style of the surrounding files; British English applies to comments and docstrings.
- Run `python -m pytest tests/unit tests/api -q` before committing.

### Troubleshooting

- **gpg-dependent tests are skipped.** `test_signing.py`'s keyring tests and `test_run_script` require `gpg` on `PATH`; install GnuPG (or let CI do it) and re-run. On Windows, ensure the GnuPG `bin` directory is on `PATH`.
- **`test_run_script` is skipped.** It also requires `git`, because the sandbox integrity check is driven by `git ls-files`.
- **`test_run_script` times out.** Read the printed `run.log` tail and the latest `logs/*.json` tail: a missing signing key, an unverified `hash` file or a busy port are the usual causes. The sandbox uses a free port and non-development mode on purpose.
- **The suite touched nothing real.** All paths are redirected per test; if a real `.env` or `logs/` file appears to change, the cause is outside the fixtures and should be investigated.
- **Stale caches.** `pytest.ini` does not disable caching; a leftover `.pytest_cache/` or `__pycache__/` can be removed safely and does not affect results.

## Configuration

Configuration is split between two files: `resources/configuration.json` for the service itself and `.env` at the project root for runtime behaviour. Copy `.env.example` to `.env`; the service reads `.env` on every start and mirrors every change made through the UI back into it.

`resources/password-pepper` is scoped to one installation, meaning one `setup.bat`/`setup.sh` run. `resources/installation.json` records that the installation has started and is deleted by the setup scripts. A new installation creates a pepper, or rotates one left by the previous installation; an installation that has already started keeps its pepper and refuses to start, logging the error, when the pepper is missing or unusable. Neither file is a `.env` key, and both are excluded from the project hash. Back the pepper up, because without it no stored password can be verified; after a re-setup regenerates it, every `USERS` entry must be regenerated too.

`resources/configuration.json`:

| Key | Default | Effect |
| --- | --- | --- |
| `port` | `49150` | Port the service binds to on `127.0.0.1`. Must be free and, on Unix-like systems, at least 1024 unless the process has elevated privileges. |
| `guiEnabled` | `true` | When `true`, the UI routes (`/`, `/login`, `/ui/*`) are registered. Set to `false` for headless operation. |
| `development` | `false` | When `true`, the project may differ from its recorded hash (a warning is logged and the update checks are skipped) and the manual and automatic project updates are refused. The plugin library is still verified strictly. |

`.env`:

| Key | Default | Effect |
| --- | --- | --- |
| `USERS` | `[]` | JSON list of `{"username","password","client_salt","admin","root","id"}`, where `password` is the stored peppered verifier (`akupara$2$…`), `client_salt` the Argon2 salt the browser used, and `admin` (default `false`) grants the administrator role. Generate every entry with `scripts/generate-password-hash.js`; a raw hash or a missing `client_salt` is refused. When the list is empty at startup, a root account `admin` with the password `admin123` is created; change that password immediately. A malformed value is left untouched, and logins are refused while no usable user exists. |
| `INTERNAL_INTERACTIONS` | `false` | Enables internal interactions: shared variables and plugin event subscriptions, and their cards. |
| `SHARED_MEMORY_ENABLED` | `false` | Switches shared variables on within internal interactions. Both this and `INTERNAL_INTERACTIONS` must be on for the shared-variable endpoints to work. When the key is absent from `.env`, the reader falls back to `true`. |
| `SHARED_MEMORY` | `[]` | JSON list of `{"name","type","value","editor","reader"}` shared variables. `type` is one of `string`, `list`, `dictionary`, `integer`, `float`, `boolean`. |
| `PLUGIN_EVENT_SUBSCRIPTIONS` | `{}` | JSON object mapping an event name to its sorted list of subscribed plugin names. |
| `API_KEYS_ENABLED` | `false` | Enables API keys: the API Keys card, `/api/api-keys*`, and authentication through the `X-Api-Key` header. |
| `API_KEYS` | `[]` | JSON list of `{"name","key","id"}`, where `key` is the Fernet-encrypted API key. Written by the service; the plaintext key is never stored, logged or returned after creation. |
| `API_KEY_ENCRYPTION_KEY` | generated on first run | Fernet key that encrypts and decrypts `API_KEYS`. Losing it makes the stored keys undecryptable. |
| `DISPLAY_PROMOTION` | `true` | Shows the promotion line at the bottom of the UI. |
| `PLAY_AUDIOS` | `true` | Enables audio playback for every event. When it is off, setting a sound file, toggling the startup or log sounds, or playing an event returns `403`. |
| `PLAY_LOG_SOUNDS` | `false` | Plays a sound for each logged event while `PLAY_AUDIOS` is on. |
| `PLAY_STARTUP_SOUND` | `true` | Plays the startup sound once after the loading operations, while `PLAY_AUDIOS` is on. |
| `STARTUP_SOUND` | `logo-reveal.wav` | Audio file from `resources/audios/` played for the `startup` event; empty plays nothing. |
| `ACKNOWLEDGE_SOUND` | `acknowledge.wav` | Audio file played for the `acknowledge` event; empty plays nothing. |
| `WARN_SOUND` | `warn.wav` | Audio file played for the `warn` event; empty plays nothing. |
| `PROCESS_SOUND` | `process.wav` | Audio file played for the `process` event; empty plays nothing. |
| `SUCCESS_SOUND` | `success.wav` | Audio file played for the `success` event; empty plays nothing. |
| `ERROR_SOUND` | `error.wav` | Audio file played for the `error` event; empty plays nothing. |
| `FFMPEG_PATH` | empty | Path to an `ffmpeg` binary; its neighbouring `ffplay` is used for playback when present. Without it, playback falls back to PowerShell's `SoundPlayer` on Windows or `aplay` elsewhere. |
| `EXTERNAL_INTERACTIONS` | `false` | Enables external interactions and starts the network worker. When off, the firewall endpoints return `403`. |
| `EXTERNAL_INTERACTIONS_INCOMING_IPS` | `[]` | JSON list of `{"ip","plugins","action","Note"}` incoming firewall entries, where `action` is `allow`, `unknown` or `block` and `plugins` is a plugin-name list. |
| `EXTERNAL_INTERACTIONS_OUTGOING_IPS` | `[]` | Outgoing firewall entries, in the same shape. |
| `EXTERNAL_INTERACTIONS_ALLOW_NEW` | `false` | Lets through remote clients that are in the incoming list as `unknown` or absent from it. Requests from `allow` clients always pass and from `block` clients are always refused. |
| `EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING` | `false` | Default policy for outgoing requests with no matching entry. |
| `AUTOMATIC_UPDATE` | `false` | Applies an available project update (and restart) at startup. Refused in development mode. |
| `AUTOMATIC_PLUGIN_LIBRARY_UPDATE` | `false` | Applies an available plugin-library update (and restart) at startup. |
| `AUTOMATIC_PLUGIN_UPGRADE` | `false` | Upgrades installed plugins that have a newer release at startup. |
| `GITHUB_TOKEN` | empty | Optional token sent to GitHub hosts as `Authorization: token <token>`. It exists to raise the API rate limit (~60 to ~5000 requests per hour). Generate one with no scopes and no expiration, and add it to `.env`; without it, frequent checks may hit the limit, in which case version checks are skipped rather than failing, while operations that need GitHub data log an error and keep running. |

The sound-file defaults above are the values shipped in `.env.example`; when a sound key is absent from `.env`, the service falls back to the same file for every event, so no event is silently muted. A key present with an empty value plays nothing for that event.

## Support

- Open an issue on [GitHub](https://github.com/LorenBll/Akupara/issues) for bug reports, feature requests or help. Security issues should be reported privately as described in [SECURITY.md](SECURITY.md).

## Licence

- [LICENSE](LICENSE)

## Author

- [LorenBll](https://github.com/LorenBll)
