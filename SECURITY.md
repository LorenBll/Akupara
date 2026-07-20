# Security Policy

## Supported Versions

Only the latest released version receives security updates.

| Version | Supported |
| ------- | --------- |
| Latest  | Yes       |

## Reporting a Vulnerability

If you believe you have found a security issue in ServiceHandler, please report it privately to the maintainers rather than opening a public issue.

ServiceHandler is a local web service registry that involves:
- **HTTP API endpoints** under `/api/*` for registering and unregistering services, managing endpoints, performing health checks, and granting API keys
- **A web UI dashboard** with sort, filter, and batch operation controls
- **API key authentication** using keys stored as plain text in the `.env` file
- **SHA-256 hash-based identity** for self-service authentication
- **A background health-check thread** that pings registered clients every 15 seconds

Include as much detail as possible, such as:
- A clear description of the issue and the affected endpoint or component
- Steps to reproduce the problem
- Any relevant logs, screenshots, or proof of concept code
- The potential impact and how severe you believe it is

If the report involves API keys, hashes, tokens, or other secrets, do not post them publicly. Redact sensitive values before sharing.

## What To Expect

After a report is received:

1. The issue will be reviewed and triaged.
2. You may be contacted for clarification or additional details.
3. A fix may be developed and validated before public disclosure.
4. The reporter may be credited unless they prefer to remain anonymous.

## Security Guidelines

This project is intended to follow basic security hygiene:

- **API keys are stored as plain text** in the `SH_API_KEYS` environment variable inside `.env`. There is no encryption layer. Treat `.env` as a sensitive file and exclude it from version control.
- **ServiceHandler binds to `127.0.0.1`** by default (port 49155). The before-request hook rejects non-local traffic before any endpoint-specific auth runs. Do not change the bind address to `0.0.0.0` without additional network-layer protections.
- **SHA-256 hashes serve as proof of identity** for self-service authentication. If a hash is leaked, any party that knows it can act on behalf of that service. Treat hashes as credentials.
- **Access control is layered** — localhost-only check runs first, then endpoint-specific logic. Review the access control table in the README before deploying.
- **Sensitive endpoints** (terminate, restart, protect, shutdown) require a valid API key or localhost access.
- **Review third-party dependencies** before adding them. ServiceHandler currently depends on Flask and jsonschema — vet any new libraries for known vulnerabilities.
- **Headless mode** (`SH_NO_GUI=true`) disables UI endpoints for a reduced attack surface when the dashboard is not needed.
- **Protected services** flagged via the protect endpoint cannot be terminated, restarted, or forgotten by anyone.
- **Treat all externally supplied input as untrusted** and validate it before use. The API validates port ranges, JSON schemas, and input types across all endpoints.

## Disclosure Notes

Do not publicly disclose an unpatched vulnerability until maintainers have had reasonable time to investigate and respond. If a coordinated disclosure timeline is needed, it can be discussed during the report process.
