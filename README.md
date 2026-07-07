# PortHandler

PortHandler is a local web service registry with a web UI. It solves the problem of registering named services on the local machine so clients can look up each other by name, port, and metadata through a small HTTP API.

## About
PortHandler is scoped to service registration and discovery on the local device. The service binds to `127.0.0.1` on port `49155` and rejects API calls that do not come from the local device. Registered clients are kept in memory only — each service must re-register every time PortHandler starts. A background health-check thread pings registered clients every 30 seconds and removes unreachable ones.

**Removed features**: The `password` field has been removed from `configuration.json` — it was never consumed by the application.

The web UI (`web/index.html`) displays a dashboard with a status pill, a searchable and sortable grid of registered service cards, a sidebar for tweaking the sort order and group-by of columns, and a health-check button. Features include:

- **Search** — real-time filtering with fuzzy matching (Levenshtein distance ≤ 2 or 30%).
- **Sort & Group-by** — drag-to-reorder sort columns; group by a selected key; sort order persisted across restarts.
- **Status grouping** — services are automatically tagged as Operational or Broken; can be sorted/grouped by status.
- **Expand/Collapse** — click a card to expand it with a smooth FLIP animation showing full metadata; collapse returns the card to its grid position.
- **Column anchoring** — expanded cards align left/middle/right based on their grid column position.
- **Health check** — click the health button to re-check all services; shows a spinner during the check.
- **Broken service management** — broken services are shown with red styling immediately; "Forget All" and "Restart All" buttons handle bulk actions.
- **Animations** — first-load animation (FLA) staggers elements in; subsequent updates use fade-in (FI); sort menu items stagger open/close.
- **Keyboard-friendly** — search is auto-focused on load; search query is cleared on page load.

> **Safety notice**: PortHandler is intended only for environments where safety is not a major risk — the chances of malevolent actors are low, and the consequences of an eventual mishap are low.

## Setup
1. Install the Python dependencies with `pip install -r requirements.txt`.
2. Review `resources/configuration.json` if you want to change the port.
3. Leave the project structure intact so the service can find `resources/` and `src/`.

## Run
1. Windows: run `scripts\run.bat`.
2. Unix-like systems: run `bash scripts/run.sh`.
3. Manual: run `python src/main.py` from the project root.

## Access Control

All endpoints are local-device only. Requests from non-local addresses are rejected with:
- `403` -> `{ "error": "Local device access only." }`
- All endpoints also support `HEAD` and `OPTIONS`.
- API responses use `Connection: close` (non-persistent connections).

## API Endpoints

### `POST /api/register` (also `HEAD`, `OPTIONS`)
Registers a new client service and returns a SHA-256 hash. Before registering, PortHandler probes the new client's health endpoint (`/api/health`) to confirm it is reachable.

- Body (JSON object):
	- `name` (string, required): name for the client service. If a client with the same name is already registered, PortHandler checks whether that existing client is still alive. If it is, registration is rejected. If it is not, the stale registration is replaced.
	- `port` (number, required): port number the client listens on (1-65535).
	- `starting_script` (string, optional): path to the client's startup script. The recommended value is the OS-appropriate run script — `scripts/run.bat` on Windows or `scripts/run.sh` on Unix — not the `main.py` file directly.
	- `pid` (number, optional): process ID of the running client.
- Returns:
	- `201` -> `{ "hash": "<sha256-hash>" }`
	- `400` -> `{ "error": "A non-empty name is required." }`
	- `400` -> `{ "error": "A port number is required." }`
	- `400` -> `{ "error": "Port must be a number between 1 and 65535." }`
	- `400` -> `{ "error": "Client health endpoint is not reachable." }`
	- `409` -> `{ "error": "A client with name '...' is already registered." }` (only returned when the existing client is still alive)

### `POST /api/question` (also `HEAD`, `OPTIONS`)
Looks up a registered client's port by name. No registration is required to ask.

- Body (JSON object):
	- `name` (string, required): name of the target client to look up.
- Returns:
	- `200` -> `{ "name": "<target-name>", "port": <target-port> }`
	- `400` -> `{ "error": "The name of the target client is required." }`
	- `404` -> `{ "error": "No client found with name '...'." }`

### `DELETE /api/unregister` (also `HEAD`, `OPTIONS`)
Unregisters a client by its hash.

- Body (JSON object):
	- `hash` (string, required): SHA-256 hash of the client to unregister.
- Returns:
	- `200` -> `{ "status": "unregistered", "hash": "<hash>" }`
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
	- `200` -> `{ "clients": [ { "hash": "...", "name": "...", "port": 1234, "pid": 5678, "ip": "127.0.0.1", "timestamp": "..." }, ... ] }`

### `GET /api/sort-settings` / `PUT /api/sort-settings` (also `HEAD`, `OPTIONS`)
Gets or sets the column sort order, group-by key, and original sort order used by the web UI.

- `GET`: returns current settings:
	- `200` -> `{ "sort_order": ["name", "port", "pid", "bind_address", "hostname", "status"], "group_by": "name", "original_sort_order": ["name", "port", "pid", "bind_address", "hostname", "status"] }`
- `PUT`: updates settings (body: `{ "sort_order": ["port", "name", "pid"], "group_by": null, "original_sort_order": ["port", "name", "pid"] }`)
	- `200` -> `{ "sort_order": ["port", "name", "pid"] }`

### `POST /api/terminate` (also `HEAD`, `OPTIONS`)
Terminates a registered client process and unregisters it.

- Body (JSON object):
	- `hash` (string, required): SHA-256 hash of the client to terminate.
	- `pid` (number, required): process ID to kill.
- Returns:
	- `200` -> `{ "status": "terminated", ... }`
	- `403` -> `{ "error": "...", "details": "..." }`

### `POST /api/restart` (also `HEAD`, `OPTIONS`)
Restarts a registered client process via its start script.

- Body (JSON object):
	- `hash` (string, required): SHA-256 hash of the client.
	- `pid` (number, required): current process ID (killed before restart).
	- `starting_script` (string, required): path to the startup script.
- Returns:
	- `200` -> `{ "status": "restarted", ... }`

### `POST /api/broken/forget` (also `HEAD`, `OPTIONS`)
Removes a client from the broken list without terminating it.

- Body (JSON object):
	- `hash` (string, required): SHA-256 hash of the broken client.
- Returns:
	- `200` -> `{ "status": "forgotten", "hash": "<hash>" }`

### `POST /api/broken/restart` (also `HEAD`, `OPTIONS`)
Forgets a client from the broken list and then restarts it.

- Body (JSON object):
	- `hash` (string, required): SHA-256 hash of the broken client.
	- `starting_script` (string, required): path to the startup script.
- Returns:
	- `200` -> `{ "status": "restarted", ... }`

### `POST /api/health/check` (also `HEAD`, `OPTIONS`)
Checks the health of all registered clients and returns unhealthy ones.

- Body: none (or empty JSON)
- Returns:
	- `200` -> `{ "unhealthy": [ { "hash": "...", "name": "...", ... }, ... ] }`

### `POST /api/health/check/<hash>` (also `HEAD`, `OPTIONS`)
Checks the health of a specific client by hash.

- Body: none
- Returns:
	- `200` -> `{ "healthy": true }` or `{ "healthy": false }`

### `GET /` (root)
Serves the web UI dashboard (`web/index.html`).

---

![Dashboard screenshot](docs/images/screenshot.png)

## License
- [LICENSE](LICENSE)

## Author
- [LorenBll](https://github.com/LorenBll)