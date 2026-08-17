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
