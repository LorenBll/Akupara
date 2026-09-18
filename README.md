# Akupara

Akupara is a scaffold for building local web services. It provides a consistent project structure, setup and deployment scripts, a local-first API pattern with health endpoint, and a browser UI — ready to be adapted to any new service.

## Table of Contents

- [About](#about)
- [Features](#features)
- [Setup](#setup)
- [Run](#run)
- [Configuration](#configuration)
- [Logging](#logging)
- [Access Control](#access-control)
- [API Endpoints](#api-endpoints)
- [Notes](#notes)
- [Support](#support)
- [License](#license)
- [Author](#author)

## About

Akupara binds to `127.0.0.1` on port `49150` and rejects non-local requests. It serves as a starting point: rename, repurpose, and fill in the business logic.

**What's included:**

- **Project structure** — scripts, deployment, resources, src/models, and ui folders following the same conventions as Cipher, DiskIdentifier, YoutubeDownloader, and LibraccioAPI.
- **Health endpoint** — `GET /api/health` returns service status, bind address, port, hostname, and PID.
- **Browser UI** — a landing page with project title, description, and an empty card ready to hold service-specific content. Styling and page-entry animations match LibraccioAPI.
- **Local-only access control** — all `/api/*` endpoints are restricted to the local device.
- **Headless mode** — set `"guiEnabled": false` in configuration to disable UI routes.

> **Safety notice**: Akupara is intended only for environments where safety is not a major risk — the chances of malevolent actors are low, and the consequences of an eventual mishap are low.

## Features

- **Local-first service scaffold** — provides the consistent project structure, configuration and deployment scripts used across Akupara-family projects. Exists so new services can be bootstrapped without boilerplate. Use the scaffold as-is and fill in business logic in `src/main.py` and `src/models/`.
- **Health endpoint** (`GET /api/health`) — returns `bind_address`, `port`, `hostname` and `PID`. Exists for liveness checks. Call `GET /api/health` from the local device.
- **Browser UI** — serves `ui/pages/index.html` and static assets. Exists as a placeholder for service-specific UI. Open `/` in a browser on the local device.
- **Access control** — all `/api/*`, `/` and `/ui/*` routes reject non-local requests (`403`). Exists to keep the service local-only. No API key is required; local requests are accepted.
- **Plugin system** — hash-range catalog in `resources/plugins-lib/` with `reverse-index.json` search, GPG trust marks and install/upgrade flow. Exists to discover and manage plugins. Use `POST /api/plugins/search` and the settings UI.
- **Update checks** — compares the local project hash and plugin library hash against the latest GitHub release/commit. Exists to keep the deployment up to date. Triggered at startup and via `/api/check-for-updates` / `/api/check-for-plugin-updates`; respects `AUTOMATIC_UPDATE` flags. When `GITHUB_TOKEN` is set, GitHub API requests are authenticated to raise the rate limit (see [Configuration](#configuration)).
- **GitHub rate-limit handling** — optional `GITHUB_TOKEN` (`.env`). Exists to increase the GitHub API limit from ~60 to ~5000 req/hour for frequent checks. Generate a token with no scopes and no expiration, add `GITHUB_TOKEN=<token>` to `.env`; the server uses it first and falls back to unauthenticated on rate limit; double rate limit on version checks is treated as "no update" so the process keeps running.

## Setup

1. Install Python dependencies: `pip install -r requirements.txt`.
2. Install GnuPG (`gpg`) and ensure it is on `PATH` (`C:\Program Files\GnuPG\bin` on Windows) — **required for plugins**: trust marks in `resources/plugins-lib/` are verified against `resources/plugins-lib/lorenbll-akupara-pub` via `gpg --verify` (the plugin loader checks the recomputed folder hash against the hash in the latest commit on the Akupara repository, and `verify_plugin_signature` in `src/plugin_bridge.py`).
3. Review `resources/configuration.json` to configure port and UI visibility.
4. Optionally set `GITHUB_TOKEN` in `.env` to raise the GitHub API rate limit. Generate a token with no scopes/permissions and no expiration (see [Configuration](#configuration) / `GITHUB_TOKEN`). Without a token frequent update checks may hit the unauthenticated rate limit (60 req/hour).
5. Leave the project structure intact so the service can find `resources/` and `src/`.

> **TODO for future README update (future self):** When this README is next updated, keep/expand the GPG prerequisite above — state explicitly that **GPG must be installed for plugins to work** (`gpg --armor --detach-sign` / `--verify`, public key `lorenbll-akupara-pub`). Do not remove this notice.

## Run

1. Windows: run `scripts\run.bat` (add `--debug` for debug output).
2. Unix-like: run `bash scripts/run.sh` (add `--debug` for debug output).
3. Manual: run `python src/main.py` from the project root (add `--debug` for debug output).

The `run` scripts support `--debug`. When run with `--debug`, `DEBUG` log events are written; otherwise they are suppressed.

## Configuration

Configuration is stored in `resources/configuration.json` and in the `.env` file at the project root. `resources/configuration.json` controls port and UI visibility (`port`, `guiEnabled`, `development`). `.env` controls runtime behaviour. Every option has a documented default and can be changed without code changes.

| Key | Default | Effect |
| --- | --- | --- |
| `USERS` | `[]` | JSON list of `{"username","password","admin","root","id"}` — login credentials. Each `password` is an Argon2id hash (salt `akupara-salt`, `m=65536,t=2,p=1`). |
| `INTERNAL_INTERACTIONS` | `false` | Enable internal plugin interactions. |
| `API_KEYS_ENABLED` | `false` | Enable API key authentication (`X-Api-Key`). |
| `DISPLAY_PROMOTION` | `true` | Show the "report issues" promotion line. |
| `API_KEYS` | `[]` | Stored API keys (Fernet-encrypted). |
| `API_KEY_ENCRYPTION_KEY` | _(generated)_ | Fernet key for `API_KEYS`. Generated on first run if not set. |
| `SHARED_MEMORY` / `SHARED_MEMORY_ENABLED` | `[]` / `false` | Internal interactions shared memory. |
| `PLAY_AUDIOS` | `true` | Enable audio playback. |
| `PLAY_LOG_SOUNDS` / `PLAY_STARTUP_SOUND` | `false` / `true` | Log and startup sounds (require `PLAY_AUDIOS`). |
| `EXTERNAL_INTERACTIONS` | `false` | Enable the external interactions worker (network listener). |
| `EXTERNAL_INTERACTIONS_INCOMING_IPS` / `EXTERNAL_INTERACTIONS_OUTGOING_IPS` | `[]` | Firewall entries (`{"ip","plugins","action","Note"}`). |
| `EXTERNAL_INTERACTIONS_ALLOW_NEW` / `EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING` | `false` | Default policy for unknown IPs. |
| `AUTOMATIC_UPDATE` | `false` | Automatically update the project when an update is available. |
| `AUTOMATIC_PLUGIN_LIBRARY_UPDATE` | `false` | Automatically update the plugin library. |
| `AUTOMATIC_PLUGIN_UPGRADE` | `false` | Automatically upgrade installed plugins. |
| `GITHUB_TOKEN` | _(empty)_ | Optional GitHub personal access token to increase the GitHub API rate limit. When set, every GitHub API request is first sent with `Authorization: token <GITHUB_TOKEN>` without trying unauthenticated first. If the token-authenticated request is rate limited, Akupara retries the same request without the token. If that is also rate limited — and **only** in that double-rate-limited case — version checks are silently skipped so Akupara keeps running as usual; other GitHub-dependent operations log an error but do not crash the process. Generate the token at `https://github.com/settings/tokens` (classic) or `https://github.com/settings/personal-access-tokens/new` (fine-grained). No specific scopes or permissions are required; select none. The token is best created with no expiration date — the absence of an expiration date does not compromise security when the token has no permissions. Add it to `.env` as `GITHUB_TOKEN=<token>` (or set the environment variable). |

The `GITHUB_TOKEN` behaviour covers frequent update checks (`_fetch_remote_project_hash`, `_fetch_latest_commit_sha`, `_fetch_github_latest_release`, etc.). Without a token the API allows ~60 requests/hour; with a token the limit is ~5000/hour.

## Logging

Akupara writes JSON log events to `logs/` (one file per start, e.g. `DD-MM-YYYY_HH.MM.SS.json`). Each event carries `timestamp`, `type` (`ERROR`/`WARN`/`INFO`/`DEBUG`), `title`, `data` and `hash`. `DEBUG` events are only written when the service is started with `--debug`. Log files older than 14 days are pruned automatically at every start.

## Access Control

All `/api/*`, `/`, and `/ui/*` endpoints are local-device only. Non-local requests are rejected with:
- `403` -> `{ "error": "Local device access only." }`
- All endpoints also support `HEAD` and `OPTIONS`.
- HTML page responses use `Connection: keep-alive` (all other responses use `Connection: close`).

No API key is required — all endpoints accept requests from the local device without additional authentication.

## API Endpoints

### `GET /api/health` (also `HEAD`, `OPTIONS`)
Service health check.
- Auth: local-device only (no API key required)
- Body: none
- Returns:
	- `200` ->
		```json
		{
			"status": "ok",
			"service": "Akupara",
			"bind_address": "127.0.0.1",
			"port": 49150,
			"hostname": "workstation-name",
			"pid": 12345
		}
		```

### `GET /` (also `HEAD`, `OPTIONS`)
Serves the browser UI (`ui/pages/index.html`).
- Auth: local-device only (no API key required)
- Body: none
- Returns:
	- `200` -> `text/html`

### `GET /ui/css/<path:filename>` (also `HEAD`, `OPTIONS`)
Serves static CSS files from the `ui/css/` directory.
- Auth: local-device only (no API key required)
- Path parameters:
	- `filename` (string, required): path to a CSS file relative to `ui/css/`.
- Body: none
- Returns:
	- `200` -> `text/css`
	- `404` -> HTML error page

## Notes

### How to Use

1. Clone or copy the scaffold.
2. Rename the project (replace "Akupara" with your service name across all files).
3. Add your API endpoints to `src/main.py`.
4. Add your request/response models to `src/models/` if needed.
5. Update `ui/pages/index.html` and `ui/css/index.css` with your UI.

---

## Support

- Open an issue on [GitHub](https://github.com/LorenBll/Akupara/issues) for bug reports, feature requests, or help.

## License

- [LICENSE](LICENSE)

## Author

- [LorenBll](https://github.com/LorenBll)
