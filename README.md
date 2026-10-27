# Akupara

Akupara is a local-first web service with a browser UI, user, session and API-key authentication, a signed and hash-verified plugin library, internal and external plugin interactions, audio feedback, and update and integrity checks.

Developer documentation: [README-DEV.md](README-DEV.md).

## Table of Contents

- [Features](#features)
- [Setup](#setup)
- [Run](#run)
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
