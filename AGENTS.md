# AGENTS.md

Guidelines for working on this codebase.

## Architecture: logic layer vs endpoints

- All functionality — validation, persistence, business rules and side effects (e.g. audio playback) — must live in the **logic layer**: internal, program-only functions (underscore-prefixed) that raise clear, exhaustive errors.
- **Endpoints** are only a filter between the outside world and the logic layer: they parse the incoming request, call a logic-layer function and map the result (or exception) to an HTTP response. No business logic belongs in endpoint handlers.

## Authorization

- An endpoint called by an entity that lacks the required authorization must return **404**, byte-identical to a non-existent endpoint, so the endpoint's existence and its auth scheme are never revealed.
- Exception: `api_key_authenticated` endpoints return **401** `{"error": "API key required."}` (the legacy message), because they are plugin-facing and need a clear error.

## Audio

- Every `.env` variable change — including one where the saved value does not actually change — plays the **acknowledge** sound. The corresponding logic-layer functions are marked with the `@play_audio("acknowledge")` decorator.
- Exception: changing `PLAY_AUDIOS` or `DISPLAY_PROMOTION` produces no sound.

## Workers

- A worker is a class whose two public methods are named `start()` and `stop()`, following the audio-worker naming standard (see `AudioOrchestrator` in `src/audio.py` and `ExternalAccessWorker` in `src/network.py`).
- `start()` begins the worker and `stop()` shuts it down; both are idempotent.
- For the audio worker, `AudioOrchestrator.start()`/`stop()` only toggle its enabled state (driven by `PLAY_AUDIOS`); the actual playback sub-workers are started on demand via `play()`. For the network worker, `ExternalAccessWorker.start()`/`stop()` drive its own lifecycle directly (it has no sub-workers).
- Logic-layer setters toggle a worker by calling its `start()`/`stop()` when the associated `.env` flag changes (e.g. `_set_external_access` → `_start_external_access_worker` / `_stop_external_access_worker`).
- The endpoints and methods the network worker may run are marked with `@network.network_worker_callable`; together they are the only ones the network worker serves — any other request is answered with a 404 byte-identical to a non-existent endpoint.
- The network worker also gates requests by remote IP (`_network_worker_ip_policy` in `src/main.py`): each IP in `NETWORK_ACCESS_IPS` carries one of the string values `"allow"`, `"unknown"` or `"block"`. Requests from `"allow"` IPs pass through and `"block"` IPs are refused. New IPs are always recorded in the list with `"unknown"`, regardless of the policy for new IPs; requests from `"unknown"` IPs and from IPs not yet in the list are decided by `NETWORK_ACCESS_ALLOW_NEW`. These automatic recordings play no acknowledge sound.
