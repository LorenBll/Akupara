# Security Policy

## Supported Versions

Only the latest released version receives security updates.

| Version | Supported |
| ------- | --------- |
| Latest  | Yes       |

## Reporting a Vulnerability

If you believe you have found a security issue in Akupara, please report it privately to the maintainers rather than opening a public issue.

Akupara is a local web service template that involves:
- **HTTP API endpoints** under `/api/*` for health checks
- **A web UI** for displaying service information
- **ServiceHandler integration** — optional registration for service discovery and endpoint registration

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

- **Localhost binding** — The service binds to `127.0.0.1` by default. The before-request hook rejects non-local traffic before any endpoint-specific logic runs. Do not change the bind address to `0.0.0.0` without additional network-layer protections.
- **Review third-party dependencies** before adding them. Akupara currently depends on Flask and python-dotenv — vet any new libraries for known vulnerabilities.
- **Headless mode** (`guiEnabled: false`) disables UI endpoints for a reduced attack surface when the dashboard is not needed.
- **ServiceHandler integration is optional.** When enabled, the service registers its API endpoints and performs periodic keepalive pings. The ServiceHandler itself is a separate local service bound to `127.0.0.1`.
- **Treat all externally supplied input as untrusted** and validate it before use.

## Disclosure Notes

Do not publicly disclose an unpatched vulnerability until maintainers have had reasonable time to investigate and respond. If a coordinated disclosure timeline is needed, it can be discussed during the report process.
