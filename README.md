# PortHandler

PortHandler is a local web service registry with a web UI. It solves the problem of registering named services on the local machine so clients can look up each other by name, port, and metadata through a small HTTP API.

## About
PortHandler is scoped to service registration and discovery on the local device. The service binds to `127.0.0.1` on port `49155` and rejects API calls that do not come from the local device. Registered clients are kept in memory only — each service must re-register every time PortHandler starts. A background health-check thread pings registered clients every 30 seconds and removes unreachable ones.

The web UI (`web/index.html`) displays a dashboard with a status pill, a searchable and sortable grid of registered service cards, a sidebar for tweaking the sort order and group-by of columns, a health-check button, and an accuracy slider for fuzzy search threshold.

**Features:**

- **Search** — real-time filtering with fuzzy matching (Levenshtein distance). The accuracy threshold defaults to 30% and is adjustable via a slider with a reset button.
- **Filter panel** — expandable filter menu with text inputs per column and a status dropdown (Any / Operational / Broken). Tab navigates in column-major order (top-to-bottom, then next column). Shift+Tab from the search input returns focus to the last filter.
- **Sort & Group-by** — drag-to-reorder sort columns; group by a selected key; sort order persisted across restarts. The group-by zone is always visible regardless of card count.
- **Status grouping** — services are automatically tagged as Operational or Broken; can be sorted/grouped by status.
- **Card expansion/collapse** — click a card to expand it with full metadata; collapse returns the card to its grid position.
- **Health check** — global button re-checks all services (shows a spinner, then reloads the grid with the active search/filter applied). Per-card health endpoint refreshes expanded content in-place.
- **Broken service management** — broken services shown with red styling immediately. "Forget All Broken Services" and "Restart All Broken Services" buttons for bulk actions.
- **Keyboard shortcuts** — search auto-focused on load. Escape closes expanded card, sort menu, or filter menu. Tab navigates filter inputs in column-major order.
- **Accuracy slider** — adjustable fuzzy matching threshold (0-100%, default 30%). Persisted across restarts alongside sort settings.

> **Safety notice**: PortHandler is intended only for environments where safety is not a major risk — the chances of malevolent actors are low, and the consequences of an eventual mishap are low.

## Setup
1. Install Python dependencies: `pip install -r requirements.txt`.
2. Review `resources/configuration.json` if you want to change the port.
3. Leave the project structure intact so the service can find `resources/` and `src/`.

## Run
1. Windows: run `scripts\run.bat`.
2. Unix-like systems: run `bash scripts/run.sh`.
3. Manual: run `python src/main.py` from the project root.

## Access Control

All `/api/*` endpoints are local-device only. Requests from non-local addresses are rejected with:
- `403` -> `{ "error": "Local device access only." }`
- All endpoints also support `HEAD` and `OPTIONS`.
- API responses use `Connection: close` for non-HTML responses.

## API Endpoints

### `GET /` (also `HEAD`, `OPTIONS`)
Serves the web UI dashboard (`web/index.html`).

- Body: none
- Returns:
	- `200` -> `text/html`

### `GET /css/<path:filename>` (also `HEAD`, `OPTIONS`)
Serves static CSS files from the `web/css/` directory.

- Path parameters:
	- `filename` (string, required): path to a CSS file relative to `web/css/`.
- Body: none
- Returns:
	- `200` -> `text/css`
	- `404` -> HTML error page

### `POST /api/register` (also `HEAD`, `OPTIONS`)
Registers a new client service and returns a SHA-256 hash. Before registering, PortHandler probes the new client's health endpoint (`/api/health`) to confirm it is reachable.

- Body (JSON object):
	- `name` (string, required): name for the client service.
	- `port` (number, required): port number the client listens on (1–65535).
	- `starting_script` (string, optional): path to the client's startup script.
	- `pid` (number, optional): process ID of the running client.

	If a client with the same `name` is already registered, PortHandler checks whether that existing client is still alive. If it is, registration is rejected. If it is not, the stale registration is replaced.

	The recommended value for `starting_script` is the OS-appropriate run script — `scripts/run.bat` on Windows or `scripts/run.sh` on Unix — not the `main.py` file directly.

- Returns:
	- `201` ->
		```json
		{ "hash": "<sha256-hash>" }
		```
	- `400` -> `{ "error": "A non-empty name is required." }`
	- `400` -> `{ "error": "A port number is required." }`
	- `400` -> `{ "error": "Port must be a number between 1 and 65535." }`
	- `400` -> `{ "error": "Client health endpoint is not reachable." }`
	- `409` -> `{ "error": "A client with name '...' is already registered." }`

### `POST /api/question` (also `HEAD`, `OPTIONS`)
Looks up a registered client's port by name. No registration is required to ask.

- Body (JSON object):
	- `name` (string, required): name of the target client to look up.
- Returns:
	- `200` ->
		```json
		{
			"name": "<target-name>",
			"port": <target-port>
		}
		```
	- `400` -> `{ "error": "The name of the target client is required." }`
	- `404` -> `{ "error": "No client found with name '...'." }`

### `DELETE /api/unregister` (also `HEAD`, `OPTIONS`)
Unregisters a client by its hash.

- Body (JSON object):
	- `hash` (string, required): SHA-256 hash of the client to unregister.
- Returns:
	- `200` ->
		```json
		{
			"status": "unregistered",
			"hash": "<hash>"
		}
		```
	- `400` -> `{ "error": "A hash is required to unregister." }`
	- `404` -> `{ "error": "Hash not found." }`

### `GET /api/health` (also `HEAD`, `OPTIONS`)
Service health check with registration statistics.

- Body: none
- Returns:
	- `200` ->
		```json
		{
			"status": "ok",
			"service": "PortHandler",
			"bind_address": "127.0.0.1",
			"port": 49155,
			"hostname": "workstation-name",
			"registered_clients": 0
		}
		```

### `GET /api/clients` (also `HEAD`, `OPTIONS`)
Returns the list of all registered clients.

- Body: none
- Returns:
	- `200` ->
		```json
		{
			"clients": [
				{
					"hash": "<sha256>",
					"name": "my-service",
					"port": 8080,
					"pid": 12345,
					"ip": "127.0.0.1",
					"timestamp": "2025-01-01T00:00:00",
					"starting_script": "scripts/run.bat"
				}
			]
		}
		```

### `GET /api/sort-settings` (also `HEAD`, `OPTIONS`)
Returns the current column sort order, group-by key, and fuzzy accuracy threshold used by the web UI.

- Body: none
- Returns:
	- `200` ->
		```json
		{
			"sort_order": ["name", "port", "pid", "bind_address", "hostname", "status"],
			"group_by": "name",
			"original_sort_order": ["name", "port", "pid", "bind_address", "hostname", "status"],
			"accuracy": 30
		}
		```

### `PUT /api/sort-settings` (also `HEAD`, `OPTIONS`)
Updates the column sort order, group-by key, and/or fuzzy accuracy threshold.

- Body (JSON object):
	- `sort_order` (array of strings, optional): column keys in desired order.
	- `group_by` (string or null, optional): key to group by, or `null` to ungroup.
	- `original_sort_order` (array of strings, optional): baseline sort order for the ungrouped view.
	- `accuracy` (number, optional): fuzzy matching threshold (0–100). Persisted and used as the default on page load.
- Returns:
	- `200` ->
		```json
		{
			"sort_order": ["port", "name", "pid"],
			"group_by": "name",
			"original_sort_order": ["port", "name", "pid"],
			"accuracy": 30
		}
		```
	- `400` -> `{ "error": "sort_order must be a non-empty list." }`
	- `500` -> `{ "error": "Failed to read/write configuration." }`

### `POST /api/terminate` (also `HEAD`, `OPTIONS`)
Terminates a registered client process and unregisters it.

- Body (JSON object):
	- `hash` (string, required): SHA-256 hash of the client to terminate.
	- `pid` (number, optional): process ID to kill. If omitted, the server looks up the stored PID for the client.
- Returns:
	- `200` ->
		```json
		{
			"status": "terminated",
			"hash": "<hash>",
			"pid": 12345
		}
		```
	- `400` -> `{ "error": "A hash is required." }`
	- `400` -> `{ "error": "No PID available for this service." }`
	- `500` -> `{ "error": "Failed to terminate process: ..." }`

### `POST /api/restart` (also `HEAD`, `OPTIONS`)
Restarts a registered client process via its start script.

- Body (JSON object):
	- `hash` (string, required): SHA-256 hash of the client.
	- `starting_script` (string, required): path to the startup script.
	- `pid` (number, optional): current process ID (killed before restart). If omitted, the server looks up the stored PID.
- Returns:
	- `200` ->
		```json
		{
			"status": "restarted",
			"hash": "<hash>"
		}
		```
	- `400` -> `{ "error": "A hash is required." }`
	- `400` -> `{ "error": "A starting_script path is required." }`
	- `400` -> `{ "error": "No PID available for this service." }`
	- `500` -> `{ "error": "Failed to terminate process: ..." }`
	- `500` -> `{ "error": "Failed to start script: ..." }`

### `POST /api/broken/forget` (also `HEAD`, `OPTIONS`)
Removes a client from the broken list without requiring a termination. If the client is still registered, it is also unregistered and its process is killed if possible.

- Body (JSON object):
	- `hash` (string, required): SHA-256 hash of the broken client.
- Returns:
	- `200` ->
		```json
		{
			"status": "forgotten",
			"hash": "<hash>"
		}
		```
	- `400` -> `{ "error": "A hash is required." }`

### `POST /api/broken/restart` (also `HEAD`, `OPTIONS`)
Forgets a client from the broken list, kills its process if still running, then restarts it via its start script.

- Body (JSON object):
	- `hash` (string, required): SHA-256 hash of the broken client.
	- `starting_script` (string, required): path to the startup script.
- Returns:
	- `200` ->
		```json
		{
			"status": "restarted",
			"hash": "<hash>"
		}
		```
	- `400` -> `{ "error": "A hash is required." }`
	- `400` -> `{ "error": "A starting_script path is required." }`
	- `500` -> `{ "error": "Failed to start script: ..." }`

### `POST /api/health/check` (also `HEAD`, `OPTIONS`)
Checks the health of all registered clients. For each registered client, PortHandler sends a `GET` request to `http://127.0.0.1:<client-port>/api/health`. Unresponsive or non-200 responses are marked as unhealthy.

- Body: none (or empty JSON `{}`)
- Returns:
	- `200` ->
		```json
		{
			"checked": true,
			"unhealthy": [
				{
					"hash": "<sha256>",
					"name": "my-service",
					"port": 8080,
					"pid": 12345,
					"ip": "127.0.0.1",
					"timestamp": "2025-01-01T00:00:00",
					"starting_script": "scripts/run.bat"
				}
			]
		}
		```

### `POST /api/health/check/<hash>` (also `HEAD`, `OPTIONS`)
Checks the health of a specific client by hash.

- Path parameters:
	- `hash` (string, required): SHA-256 hash of the client to check.
- Body: none
- Returns:
	- `200` ->
		```json
		{
			"hash": "<sha256>",
			"healthy": true
		}
		```
		or
		```json
		{
			"hash": "<sha256>",
			"healthy": false
		}
		```
	- `404` -> `{ "error": "Client not found." }`

---

![Dashboard screenshot](docs/images/screenshot.png)

## License
- [LICENSE](LICENSE)

## Author
- [LorenBll](https://github.com/LorenBll)
