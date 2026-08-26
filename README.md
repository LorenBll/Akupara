# Akupara

Akupara is a scaffold for building local web services. It provides a consistent project structure, setup and deployment scripts, a local-first API pattern with health endpoint, a browser UI, and optional ServiceHandler integration — ready to be adapted to any new service.

## About

Akupara binds to `127.0.0.1` on port `49150` and rejects non-local requests. It serves as a starting point: rename, repurpose, and fill in the business logic.

**What's included:**

- **Project structure** — scripts, deployment, resources, src/models, and ui folders following the same conventions as Cipher, DiskIdentifier, YoutubeDownloader, LibraccioAPI, and ServiceHandler.
- **Health endpoint** — `GET /api/health` returns service status, bind address, port, hostname, and PID.
- **Browser UI** — a landing page with project title, description, and an empty card ready to hold service-specific content. Styling and page-entry animations match LibraccioAPI.
- **Local-only access control** — all `/api/*` endpoints are restricted to the local device.
- **Headless mode** — set `"guiEnabled": false` in configuration to disable UI routes.
- **ServiceHandler integration** — optionally registers with ServiceHandler for service discovery and endpoint registration.

> **Safety notice**: Akupara is intended only for environments where safety is not a major risk — the chances of malevolent actors are low, and the consequences of an eventual mishap are low.

## Setup

1. Install Python dependencies: `pip install -r requirements.txt`.
2. Install GnuPG (`gpg`) and ensure it is on `PATH` (`C:\Program Files\GnuPG\bin` on Windows) — **required for plugins**: trust marks in `resources/plugins-lib/` are verified against `resources/plugins-lib/lorenbll-akupara-pub` via `gpg --verify` (the plugin loader checks `resources/plugins-lib/hash` vs GitHub latest and recomputed folder hash, and `verify_plugin_signature` in `src/plugin_bridge.py`).
3. Review `resources/configuration.json` to configure port, UI visibility, ServiceHandler integration, and ServiceHandler port.
4. Leave the project structure intact so the service can find `resources/` and `src/`.

> **TODO for future README update (future self):** When this README is next updated, keep/expand the GPG prerequisite above — state explicitly that **GPG must be installed for plugins to work** (`gpg --armor --detach-sign` / `--verify`, public key `lorenbll-akupara-pub`). Do not remove this notice.

## Run

1. Windows: run `scripts\run.bat` (add `--debug` for debug output).
2. Unix-like: run `bash scripts/run.sh` (add `--debug` for debug output).
3. Manual: run `python src/main.py` from the project root (add `--debug` for debug output).

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

### ServiceHandler Integration

Akupara can register with ServiceHandler for service discovery. This is enabled by default (`servicehandlerEnabled: true` in `resources/configuration.json`); set to `false` to disable. Configure the ServiceHandler port via `servicehandlerPort` (default: `49155`). A background thread polls keepalive every 15 seconds and re-registers if the keepalive check fails (re-registration retries every 5 seconds).

---

## Support

- Open an issue on [GitHub](https://github.com/LorenBll/Akupara/issues) for bug reports, feature requests, or help.

## License

- [LICENSE](LICENSE)

## Author

- [LorenBll](https://github.com/LorenBll)
