# Security Policy

## Supported versions

Only the latest released version receives security updates.

| Version | Supported |
| ------- | --------- |
| Latest  | Yes       |

## Reporting a vulnerability

Report security issues privately. Do not open a public issue, pull request, discussion or any other public channel, and do not disclose the issue publicly before a fix has been released.

Email **lorenbll@outlook.com** and signal the email as urgent, so that it is handled ahead of ordinary correspondence.

Akupara is a local-first web service. Its attack surface includes the HTTP API under `/api/*`, the browser UI, the session and API-key authentication, the external-interactions worker, the plugin bridge, and the signed project and plugin-library hashes.

Include as much detail as possible, such as:

- A clear description of the issue and the part of the project it affects.
- The steps needed to reproduce the problem, when they are known.
- Any relevant logs, screenshots or proof-of-concept code.
- The impact you believe the issue has.

If the report involves API keys, hashes, tokens or other secrets, do not include them in the email; redact them and state where they were found.

## What to expect

After a report is received:

1. The issue is reviewed and triaged.
2. You may be contacted for clarification or additional details.
3. A fix is developed and validated before public disclosure.
4. The reporter is credited unless they prefer to remain anonymous.

## Security guidelines

This project is intended to follow basic security hygiene:

- **Localhost binding** — the service binds to `127.0.0.1` by default. The before-request hook rejects non-local traffic before any endpoint-specific logic runs. Do not change the bind address to `0.0.0.0` without additional network-layer protections.
- **Review third-party dependencies** before adding them. Akupara currently depends on Flask and python-dotenv — vet any new libraries for known vulnerabilities.
- **Headless mode** (`guiEnabled: false`) disables UI endpoints for a reduced attack surface when the dashboard is not needed.
- **Treat all externally supplied input as untrusted** and validate it before use.

## Disclosure notes

Do not publicly disclose an unpatched vulnerability until the maintainer has had reasonable time to investigate and respond. If a coordinated disclosure timeline is needed, it can be discussed during the report process.
